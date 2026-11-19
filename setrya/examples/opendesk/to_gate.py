"""Translate a pinned opendesk trial report into portable gate observations."""

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from .verify_report import MAX_INPUT_BYTES, parse_report, read_raw

# Each scope identifies the reader and lookup implemented by this specific runner.
# Keep it independent of the release policy that will judge these observations.
PROBES = (
    ("source", "sql-observer.message-id.v1", "source_message_row", "after_offboarding"),
    (
        "summary",
        "sql-observer.ticket-summary-id.v1",
        "stored_ticket_summary",
        "after_offboarding",
    ),
    (
        "agent-read",
        "support-agent.ticket-summary.v1",
        "agent_summary",
        "after_offboarding",
    ),
    (
        "worker-summary",
        "sql-observer.ticket-summary-id.v1",
        "stored_ticket_summary",
        "after_worker",
    ),
)
STATES = {
    "FOUND": "present",
    "ABSENT_BY_ID": "absent",
    "RETURNED": "present",
    "NOT_RETURNED": "absent",
    "INCONCLUSIVE": "unavailable",
    "NOT_TESTED": "not_tested",
}
V2_SCOPES = {
    "summary": "sql-observer.ticket-summary-marker.v2",
    "worker-summary": "sql-observer.ticket-summary-marker.v2",
    "agent-read": "support-agent.ticket-summary-marker.v2",
}


def read_pair(phases, phase, field):
    if phase not in phases:
        return {"subject": "not_tested", "control": "not_tested"}
    rows = phases[phase]
    return {
        "subject": STATES[rows["Acme"][field]],
        "control": STATES[rows["Birch"][field]],
    }


def observations(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Oversized trial report")
    return convert(parse_report(raw), hashlib.sha256(raw).hexdigest())


def convert(trial, digest):
    phases = {row["phase"]: row["workspaces"] for row in trial["observations"]}
    probes = []
    for name, scope, field, checkpoint in PROBES:
        if trial["report_version"] == 2:
            scope = V2_SCOPES.get(name, scope)
        probe = {
            "id": name,
            "scope": scope,
            "checkpoint": checkpoint,
            "before": read_pair(phases, "before_offboarding", field),
            "after": read_pair(phases, checkpoint, field),
        }
        if checkpoint == "after_worker":
            # The pinned runner finishes both summary requests before offboarding.
            probe["worker"] = {
                "captured_before_offboarding": False,
                "finished_after_offboarding": False,
            }
        probes.append(probe)
    return {
        "version": 1,
        "target": "opendesk",
        "revision": trial["target"]["commit"],
        "run_id": "sha256-" + digest,
        "offboarding": "completed"
        if trial["offboarding_http_status"] == 204
        else "unconfirmed",
        "probes": probes,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() and args.report.samefile(args.output):
            raise ValueError("Output must differ from input")
        converted = observations(read_raw(args.report))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=args.output.parent, delete=False, encoding="utf-8"
        ) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(converted, stream, indent=2)
                stream.write("\n")
                stream.close()
                temporary.replace(args.output)
            finally:
                temporary.unlink(missing_ok=True)
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        print(
            "Cannot convert trial report. Check the pinned input and output paths.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
