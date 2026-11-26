"""Verify completed deletions using journal pointers and independently reopened store snapshots."""
from pathlib import Path
import re

from setrya.core.provenance import validate as validate_provenance
from setrya.provenance import producer_only
from setrya.erasure.journal import identifier
from setrya.offboarding.inputs import require
from setrya.control_references import validate_controls
from .observe import check_request
from .profiles import OBSERVED, day, fields, issue, new_bundle, sha, stamp
from .sampling import load_population
from .stores import observer_config

LEGACY_FORMAT = "setrya.observed-deletion.v1"
FORMAT = "setrya.observed-deletion.v1.1"

GAPS = {"journal-unavailable", "journal-producer-coverage-incomplete", "key-limit", "unmapped-journal-store", "store-or-control-unavailable",
        "external-or-shared-key", "key-read-unavailable", "control-is-subject-key", "expected-store-missing-from-journal"}


class JournalObserver:
    """Read-only SQLite/PostgreSQL observer. Only built-in, validated store configurations are accepted."""

    def __init__(self, *, journal, inventory_file):
        self._journal = Path(journal)
        self._stores, self._roster = observer_config(inventory_file)

    def _read(self, request):
        return check_request(request, self._journal, self._stores, identify_keys=True, roster=self._roster)

    def find(self, request):
        """Read the direct and derived keys journalled for this completed request."""
        return self._read(request)

    def verify_absent(self, request):
        """Re-read the journal, visibility controls and keys in fresh read-only transactions."""
        return self._read(request)


def observe_deletion(*, frame, request, journal, inventory_file, output, controls=None):
    """Save and return a v3 observed-deletion receipt for a completed request in the supplied frame."""
    rows, digest = load_population(frame)
    require(request in rows, "Request was not in the supplied frame.")
    observer = JournalObserver(journal=journal, inventory_file=inventory_file)
    row = rows[request]
    policies = {name: {k: spec[k] for k in ("owner", "follow_up") if k in spec}
                for name, spec in observer._stores.items() if name in row["stores"]}
    evidence = {"format": FORMAT, "request": row, "frame_sha256": digest,
                "policy": policies, "provenance": producer_only()}
    if controls is not None:
        evidence["controls"] = validate_controls(controls)
    with new_bundle(output) as folder:
        evidence["find"] = observer.find(row)
        evidence["verify_absent"] = observer.verify_absent(row)
        return issue(OBSERVED, evidence, folder)


def _readings(stage, request):
    fields(stage, "id completed_at checked_at journal_read_at status checks gaps")
    require(stage["id"] == request["id"] and stage["completed_at"] == request["completed_at"],
            "Observation is for a different deletion request.")
    checked = stamp(stage["checked_at"])
    completed = stamp(request["completed_at"])
    require(completed <= checked, "Observation precedes the completed request.")
    require(isinstance(stage["gaps"], list) and len(stage["gaps"]) <= len(GAPS)
            and set(stage["gaps"]) <= GAPS, "Invalid store observation gaps.")
    require(isinstance(stage["checks"], list) and len(stage["checks"]) <= 512, "Too many journalled key reads.")
    if stage["journal_read_at"] is None:
        require(not stage["checks"] and "journal-unavailable" in stage["gaps"], "Missing journal snapshot.")
    else:
        require(completed <= stamp(stage["journal_read_at"]) <= checked, "Invalid journal observation time.")
    readings = {}
    for check in stage["checks"]:
        fields(check, "store key_sha256 state", ("journal_event", "lineage_version", "collector_sha256"))
        identifier(check["store"], "store")
        key = (check["store"], sha(check["key_sha256"]))
        refs = [check[k] for k in ("journal_event", "lineage_version") if k in check]
        collected = sha(check["collector_sha256"]) if "collector_sha256" in check else None
        require((refs or collected) and all(type(n) is int and n > 0 for n in refs) and key not in readings
                and check["state"] in ("PRESENT", "ABSENT", "UNKNOWN"), "Invalid direct key evidence.")
        readings[key] = check
    states = {r["state"] for r in readings.values()}
    status = "PRESENT" if "PRESENT" in states else "UNKNOWN" if stage["gaps"] or states != {"ABSENT"} else "ABSENT"
    require(stage["status"] == status, "Read summary disagrees with its journalled observations.")
    return readings


def evidence_ids(row):
    return {k: row[k] for k in ("journal_event", "lineage_version", "collector_sha256") if row and k in row}


def derive_observed(e):
    fmt = e.get("format") if isinstance(e, dict) else None
    provenanced = fmt == FORMAT
    fields(e, "format request frame_sha256 policy find verify_absent" + (" provenance" if provenanced else ""),
           ("controls",))
    require(fmt in (FORMAT, LEGACY_FORMAT), "Invalid read-only observation format.")
    producer = validate_provenance(e["provenance"]) if provenanced else None
    require(producer is None or producer["application"]["execution"] == "unavailable",
            "Direct observation cannot establish an application checkout or executed test files.")
    req = e["request"]
    fields(req, "id subject_ref completed_at stores")
    sha(e["frame_sha256"])
    sha(req["subject_ref"])
    require(isinstance(req["id"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", req["id"]),
            "Invalid request identifier.")
    expected = req["stores"]
    require(isinstance(expected, list) and 0 < len(expected) <= 64 and len(set(expected)) == len(expected)
            and all(isinstance(s, str) and re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", s) for s in expected),
            "Invalid expected stores.")
    policy = e["policy"]
    require(isinstance(policy, dict) and set(policy) <= set(expected), "Invalid observer store policy.")
    for setting in policy.values():
        fields(setting, "", ("owner", "follow_up"))
        if "owner" in setting:
            identifier(setting["owner"], "owner")
        if "follow_up" in setting:
            day(setting["follow_up"])
    first, last = (_readings(e[stage], req) for stage in ("find", "verify_absent"))
    first_completed = stamp(e["find"]["checked_at"])
    require(first_completed <= stamp(e["verify_absent"]["checked_at"]), "Reads are out of order.")
    second_snapshot = e["verify_absent"]["journal_read_at"]
    require(second_snapshot is None or first_completed <= stamp(second_snapshot),
            "The second journal snapshot must follow the first completed observation.")
    gaps = set(e["find"]["gaps"] + e["verify_absent"]["gaps"])
    for reads in (first, last):
        if set(expected) - {s for s, _ in reads}:
            gaps.add("expected-store-missing-from-journal")
    checks, open_items = [], []
    for store, key in sorted(first.keys() | last.keys()):
        before = first.get((store, key))
        after = last.get((store, key))
        states = [r["state"] if r else "UNKNOWN" for r in (before, after)]
        status = "UNKNOWN"
        if "PRESENT" in states:
            status = "PRESENT"
        elif states == ["ABSENT", "ABSENT"] and evidence_ids(before) == evidence_ids(after) and store in policy:
            status = "ABSENT"
        if not before or not after or evidence_ids(before) != evidence_ids(after):
            gaps.add("journal-scope-changed")
        checks.append({"store": store, "key_sha256": key, "status": status,
                       "find": states[0], "verify_absent": states[1], **evidence_ids(after or before)})
        if status != "ABSENT":
            open_items.append({"store": store, "key_sha256": key, "kind": "verification",
                               "evidence": f"erasure-receipt.json#/checks/{len(checks)-1}",
                               **policy.get(store, {})})
    for store in sorted(set(expected) - ({s for s, _ in first} & {s for s, _ in last})):
        open_items.append({"store": store, "kind": "coverage", "evidence": "erasure-receipt.json#/gaps",
                           **policy.get(store, {})})
    states = {c["status"] for c in checks}
    verdict = "FAIL" if "PRESENT" in states else "NOT TESTED" if gaps or states != {"ABSENT"} else "PASS"
    return {**({"provenance": producer, "evidence_format": fmt} if provenanced else {}),
            "scope": "person", "request_id": req["id"], "subject": {"ref": req["subject_ref"]},
            "frame_sha256": e["frame_sha256"], "observed_at": e["verify_absent"]["checked_at"],
            "technical_verdict": verdict, "review_status": {"FAIL": "RESIDUAL_FOUND", "PASS": "READY_FOR_REVIEW",
                                                           "NOT TESTED": "OPEN_ITEMS"}[verdict],
            "checks": checks, "gaps": sorted(gaps), "open_items": open_items,
            **({"controls": validate_controls(e["controls"])} if "controls" in e else {})}
