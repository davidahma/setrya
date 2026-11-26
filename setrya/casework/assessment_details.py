"""Project replay-derived scope and customer evidence for release comparisons."""

from collections import Counter

from setrya.core.inputs import require
from setrya.core.receipt_v3 import (
    GROUPED_EVIDENCE_FORMATS,
    answer_records,
    answer_slots,
    post_erasure_findings,
)

from .customer import customer_text
from .profiles import day, fields, sha
from .receipt_summary import _read_drill_evidence

_SURFACES = {"copy": (4,), "query": (5, 6), "scan": (2,), "observed-copy": (3,), "worker": (3,),
             "maintenance": (7,)}


def details(receipt, path):
    require(receipt.get("scope") == "person" and receipt.get("profile") in (None, "observed-deletion"),
            "Client comparisons accept person drills and read-only deletion observations.")
    if receipt.get("profile") == "observed-deletion":
        surfaces = {("observed-copy", row["store"], row["key_sha256"]) for row in receipt["checks"]
                    if row["status"] in ("ABSENT", "PRESENT")}
        kinds = {"residual"} if any(row["status"] == "PRESENT" for row in receipt["checks"]) else set()
    else:
        evidence = _read_drill_evidence(path, receipt)
        surfaces = _drill_surfaces(receipt, evidence)
        kinds = post_erasure_findings(evidence, grouped=evidence["format"] in GROUPED_EVIDENCE_FORMATS)[0].kinds

    return {
        "observed": [list(row) for row in sorted(surfaces)],
        "failures": list(receipt.get("failures", ())),
        "residual": "residual" in kinds,
        "open_items": [{"surface": row["store"], "kind": row["kind"], "owner": row.get("owner"),
                        "due": row.get("follow_up")} for row in receipt.get("open_items", ())],
        "customer_summary": customer_text(receipt, failure_kinds=kinds),
        "deployment": receipt.get("deployment"),
    }


def _drill_surfaces(receipt, evidence):
    phases = ("baseline", "immediate", "final")
    if any(evidence[phase] is None for phase in phases):
        return set()
    case = evidence["case"]
    surfaces = {("copy", row["store"], row["key"], row["action"]) for row in receipt.get("artifact_checks", ())
                if len(row["evidence"]) == 3}
    for index, result in enumerate(receipt.get("maintenance_checks", ()), start=1):
        step = f"{index} {result['step']}"
        surfaces.update(("maintenance", case["route"], case["conversation"], step, row["store"], row["key"], row["action"])
                        for row in result["checks"] if row["status"] in ("PASS", "FAIL"))
    surfaces.update(("scan", store) for store in case["scan_stores"] if evidence["baseline"]["scans"][store]
                    and all(evidence[phase]["scans"][store] is not None for phase in phases[1:]))
    if receipt.get("race") == "exercised":
        surfaces.add(("worker", case["route"], case["conversation"]))

    seen = Counter()
    all_reads = [answer_records(evidence, phase) for phase in phases]
    for (caller, question, role, _), baseline, *after in zip(answer_slots(case), *all_reads, strict=True):
        surface = ("query", case["route"], caller, question, role)
        if (baseline is not None and baseline[role + "_present"] is True and
                all(row is not None and row["target_present"] is not None
                    and (role == "target" or row["control_present"] is not None) for row in after)):
            seen[surface] += 1
    return surfaces | {(*surface, count) for surface, count in seen.items()}


def _text(value, limit):
    require(isinstance(value, str) and 0 < len(value) <= limit and value.isprintable(),
            "Invalid assessment comparison text.")


def _rows(value, limit):
    require(isinstance(value, list) and len(value) <= limit, "Too many assessment comparison entries.")
    return value


def validate_details(value):
    fields(value, "observed failures residual open_items customer_summary deployment")
    require(type(value["residual"]) is bool, "Invalid comparison residual state.")
    surfaces = _rows(value["observed"], 2048)
    query_widths = set()
    for row in surfaces:
        require(isinstance(row, list) and row and isinstance(row[0], str)
                and len(row) in _SURFACES.get(row[0], ()), "Invalid observed surface.")
        parts = row[1:]
        if row[0] == "query":
            query_widths.add(len(row))
            *parts, count = parts
            require(type(count) is int and 1 <= count <= 32, "Invalid observed query sample count.")
            sha(parts[2])
            if len(parts) == 4:
                require(parts[3] in ("target", "control"), "Invalid observed query role.")
        for part in parts:
            _text(part, 1024)
        if row[0] == "observed-copy":
            sha(row[2])
    require(len(query_widths) <= 1, "Inconsistent observed query roles.")
    require(list(map(tuple, surfaces)) == sorted({tuple(row) for row in surfaces}),
            "Duplicate or unordered observed surface.")
    for line in _rows(value["failures"], 640):
        _text(line, 4096)
    for row in _rows(value["open_items"], 640):
        fields(row, "surface kind owner due")
        _text(row["surface"], 1024)
        _text(row["kind"], 96)
        if row["owner"] is not None:
            _text(row["owner"], 1024)
        if row["due"] is not None:
            day(row["due"])
    text = value["customer_summary"]
    require(isinstance(text, str) and 0 < len(text) <= 128 * 1024
            and all(not line or line.isprintable() for line in text.split("\n")), "Invalid customer summary.")
    if value["deployment"] is not None:
        from setrya.core.deployment import validate_binding

        validate_binding(value["deployment"])
