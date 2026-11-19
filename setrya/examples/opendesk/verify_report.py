"""Validate a trial report resubmitted for publication, including its outcome."""

import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

TARGET_V0413 = {
    "version": "v0.4.13",
    "commit": "e0012447924c5ac8a13a51d2cd41a6496bc26fb3",
    "image": "sha256:e0fca3427eec675426c6c96766267d7980c861b52f630afc906fcf0c7dca0fe1",
}
TARGET = {
    "version": "v0.4.14",
    "commit": "779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5",
    "image": "sha256:cb60703a043dc2afa9fb3266e93079813be3e171ed70177e2d4ac47cb266f4d8",
}
METADATA = {
    "report_version": 1,
    "case": "upstream_opendesk_workspace_offboarding",
    "target": TARGET,
    "roles": {
        "offboarding": "workspace owner with MFA",
        "application_read": "agent in both workspaces at baseline",
        "storage_read": "axiom_observer (column-level SELECT)",
    },
    "worker_overlap": {
        "result": "NOT_TESTED",
        "reason": "This trial invokes the agent summary endpoint synchronously.",
    },
    "exclusions": [
        "physical media",
        "backups",
        "other stored copies",
        "other API paths",
        "future writes",
    ],
}
HEALTHY = {
    "source_message_row": "FOUND",
    "stored_ticket_summary": "FOUND",
    "agent_summary": "RETURNED",
    "agent_http_status": 200,
}
PHASES = ("before_offboarding", "after_offboarding")
STAGES = ("startup", "seeding", "baseline", "offboarding", "observation", "cleanup")
MAX_INPUT_BYTES = 65_536
LATEST_VERSION = 2
HEALTHY_V2 = {**HEALTHY, "agent_read_reason": "none"}
UNTESTED = {
    "source_message_row": "NOT_TESTED",
    "stored_ticket_summary": "NOT_TESTED",
    "agent_summary": "NOT_TESTED",
    "agent_http_status": None,
    "agent_read_reason": "not_tested",
}


def require(condition):
    if not condition:
        raise ValueError("Invalid or inconsistent upstream evidence")


def outcome(observations, version=1):
    healthy = HEALTHY if version == 1 else HEALTHY_V2
    if not observations:
        return "INCONCLUSIVE"
    if any(row != healthy for row in observations[0]["workspaces"].values()):
        return "INCONCLUSIVE"
    if len(observations) != 2:
        return "INCONCLUSIVE"
    after = observations[1]["workspaces"]
    acme = after["Acme"]
    if (
        "FOUND" in (acme["source_message_row"], acme["stored_ticket_summary"])
        or acme["agent_summary"] == "RETURNED"
    ):
        return "FAIL"
    if after["Birch"] != healthy:
        return "INCONCLUSIVE"
    if version == 2 and (
        acme["source_message_row"] != "ABSENT_BY_ID"
        or acme["stored_ticket_summary"] != "ABSENT_BY_ID"
        or acme["agent_read_reason"] not in ("none", "workspace_required")
    ):
        return "INCONCLUSIVE"
    return "COMPLETED"


def validate_v2_row(row):
    require(set(row) == set(UNTESTED))
    for surface in ("source_message_row", "stored_ticket_summary"):
        require(row[surface] in ("FOUND", "ABSENT_BY_ID", "INCONCLUSIVE", "NOT_TESTED"))
    status, result, reason = (
        row["agent_http_status"],
        row["agent_summary"],
        row["agent_read_reason"],
    )
    require(status is None or (type(status) is int and 100 <= status <= 599))
    if reason == "none":
        require(status == 200 and result in ("RETURNED", "NOT_RETURNED"))
    elif reason == "not_tested":
        require(status is None and result == "NOT_TESTED")
    else:
        require(result == "INCONCLUSIVE")
        if reason == "read_error":
            require(status is None)
        elif reason == "unexpected_ticket" or reason == "unexpected_summary":
            require(status == 200)
        elif reason == "workspace_required":
            require(status == 400)
        else:
            require(reason == "http_error" and status is not None and status != 200)


def validate(report):
    require(
        set(report)
        == set(METADATA)
        | {
            "outcome",
            "interrupted_at",
            "offboarding_http_status",
            "observations",
        }
    )
    version = report["report_version"]
    require(type(version) is int and version in (1, LATEST_VERSION))
    healthy = HEALTHY if version == 1 else HEALTHY_V2
    supported_targets = (TARGET_V0413,) if version == 1 else (TARGET_V0413, TARGET)
    require(report["target"] in supported_targets)
    for key, value in METADATA.items():
        if key not in ("report_version", "target"):
            require(report[key] == value)
    observations = report["observations"]
    require(isinstance(observations, list) and len(observations) <= 2)
    for index, item in enumerate(observations):
        require(
            set(item) == {"phase", "observed_at", "workspaces"}
            and item["phase"] == PHASES[index]
        )
        date = datetime.fromisoformat(item["observed_at"])
        require(
            date.tzinfo == UTC
            and date.isoformat(timespec="seconds") == item["observed_at"]
        )
        require(
            isinstance(item["workspaces"], dict)
            and set(item["workspaces"]) == {"Acme", "Birch"}
        )
        for row in item["workspaces"].values():
            if version == 2:
                validate_v2_row(row)
                continue
            require(set(row) == set(HEALTHY))
            for surface in ("source_message_row", "stored_ticket_summary"):
                require(row[surface] in ("FOUND", "ABSENT_BY_ID"))
            require(
                type(row["agent_http_status"]) is int
                and row["agent_http_status"] in (200, 400, 401, 403, 404)
            )
            if row["agent_http_status"] == 200:
                require(row["agent_summary"] in ("RETURNED", "NOT_RETURNED"))
            else:
                require(row["agent_summary"] == "INCONCLUSIVE")
    interrupted = report["interrupted_at"]
    require(interrupted is None or interrupted in STAGES)
    status = report["offboarding_http_status"]
    require(status is None or (type(status) is int and status == 204))
    if len(observations) == 2:
        allowed = (
            (None, "cleanup") if version == 1 else (None, "observation", "cleanup")
        )
        require(status == 204 and interrupted in allowed)
        require(observations[0]["observed_at"] <= observations[1]["observed_at"])
    if status == 204:
        require(
            observations
            and all(row == healthy for row in observations[0]["workspaces"].values())
        )
        if len(observations) == 1:
            require(interrupted in ("observation", "cleanup"))
    elif observations:
        require(interrupted in ("baseline", "offboarding", "cleanup"))
        if interrupted == "offboarding":
            require(
                all(row == healthy for row in observations[0]["workspaces"].values())
            )
    else:
        require(interrupted in ("startup", "seeding", "baseline", "cleanup"))
    result = outcome(observations, version)
    if interrupted is not None and result != "FAIL":
        result = "INCONCLUSIVE"
    require(result == report["outcome"])


def unique_keys(pairs):
    output = {}
    for key, value in pairs:
        require(key not in output)
        output[key] = value
    return output


def parse_report(raw):
    report = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)
    validate(report)
    return report


def read_raw(path):
    def regular_file(name, flags):
        fd = os.open(name, flags | getattr(os, "O_NONBLOCK", 0))
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode))
        except BaseException:
            os.close(fd)
            raise
        return fd

    with open(path, "rb", opener=regular_file) as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    require(len(raw) <= MAX_INPUT_BYTES)
    return raw


if __name__ == "__main__":
    try:
        parse_report(read_raw(Path(sys.argv[1])))
    except (IndexError, KeyError, TypeError, ValueError, OSError, RecursionError):
        sys.exit("Invalid upstream report")
    print("Upstream report matches its pinned, content-free contract.")
