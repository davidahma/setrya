"""Export authenticated Setrya observations for an OSCAL assessment review."""

from collections import Counter
from datetime import UTC, datetime
from itertools import repeat
import os
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from uuid import uuid4

from setrya import __version__
from setrya.core.control_references import REFERENCE
from setrya.core.inputs import InvalidReport, publish_new, read_json, require
from setrya.core.receipt_v3 import Findings, answer_outcome, answer_records, answer_slots, stored_outcome
from setrya.core.scan_scope import same_scope
from setrya.receipt_format import MAX_BUNDLE_FILE_BYTES, RECEIPT

from .exchange import MANIFEST
from .handoff import verify_handoff
from .profiles import fields, sha, stamp


VERSION = "1.2.0"
MAPPING_FORMAT = "setrya.control-mapping.v1"
NAMESPACE = "https://github.com/davidahma/setrya"
CONTROL_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,95}")
PLAN_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.I)
CHECKS = frozenset((
    "synthetic-erasure", "artifact-delete", "artifact-preserve", "artifact-rewrite",
    "endpoint-read", "store-scan", "write-back", "read-only-observation", "vendor-conformance", "provider-readback",
))


def _label(value, pattern=REFERENCE):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _read_mapping(path):
    record, digest = read_json(path, "control mapping")
    fields(record, "format version supports")
    require(record["format"] == MAPPING_FORMAT and _label(record["version"], r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"),
            "Expected a versioned Setrya control mapping.")
    rows = record["supports"]
    require(isinstance(rows, list) and 1 <= len(rows) <= 128, "Declare one to 128 supporting mapping rows.")
    seen, labels = set(), {}
    for row in rows:
        fields(row, "check framework edition reference control_id applicability source")
        check, control_id = row["check"], row["control_id"]
        require(isinstance(check, str) and check in CHECKS and _label(control_id, CONTROL_ID),
                "Expected a supported Setrya check and an OSCAL control ID.")
        labels_for_id = tuple(row[k] for k in ("framework", "edition", "reference"))
        require(all(_label(label) for label in labels_for_id), "Expected bounded framework, edition and control labels.")
        applicability = row["applicability"]
        require(isinstance(applicability, str) and 0 < len(applicability) <= 700
                and applicability == applicability.strip() and applicability.isprintable(),
                "Expected a short mapping applicability statement without surrounding whitespace.")
        source = row["source"]
        require(isinstance(source, str) and 0 < len(source) <= 1024 and source.isascii()
                and all(33 <= ord(character) <= 126 for character in source), "Expected an HTTPS source citation.")
        try:
            parsed = urlsplit(source)
        except ValueError:
            raise InvalidReport("Expected an HTTPS source citation without credentials.") from None
        require(parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None,
                "Expected an HTTPS source citation without credentials.")
        key = check, control_id
        require(key not in seen, "Duplicate check and control mapping.")
        require(labels.setdefault(control_id, labels_for_id) == labels_for_id,
                "One OSCAL control ID refers to conflicting framework references.")
        seen.add(key)
    return record, digest


def _control_ids(rows):
    require(isinstance(rows, list) and 0 < len(rows) <= 4096, "Expected explicit Assessment Plan control IDs.")
    selections = set()
    for row in rows:
        fields(row, "control-id")
        require(_label(row["control-id"], CONTROL_ID), "Expected whole-control IDs in the Assessment Plan selection.")
        selections.add(row["control-id"])
    return selections


def _read_plan(path):
    document, digest = read_json(path, "OSCAL Assessment Plan")
    fields(document, "assessment-plan")
    plan = document["assessment-plan"]
    require(isinstance(plan, dict), "Expected an OSCAL Assessment Plan.")
    uuid, metadata, ssp, reviewed = (plan.get(name) for name in ("uuid", "metadata", "import-ssp", "reviewed-controls"))
    require(_label(uuid, PLAN_UUID) and isinstance(metadata, dict) and metadata.get("oscal-version") == VERSION
            and isinstance(ssp, dict) and isinstance(ssp.get("href"), str) and ssp["href"].strip()
            and isinstance(reviewed, dict), f"Expected an OSCAL {VERSION} Assessment Plan with an SSP reference.")
    require(all(isinstance(metadata.get(field), str) and metadata[field].strip() for field in ("title", "version")),
            "The Assessment Plan metadata needs a title and version.")
    try:
        stamp(metadata.get("last-modified"))
    except ValueError:
        raise InvalidReport("The Assessment Plan last-modified time must include a timezone.") from None
    selections = reviewed.get("control-selections")
    require(isinstance(selections, list) and 0 < len(selections) <= 128,
            "The Assessment Plan must explicitly select the controls in scope.")
    include, exclude = set(), set()
    for selection in selections:
        require(isinstance(selection, dict) and "include-controls" in selection and "include-all" not in selection,
                "Use explicit Assessment Plan control IDs instead of include-all.")
        include.update(_control_ids(selection["include-controls"]))
        if "exclude-controls" in selection:
            exclude.update(_control_ids(selection["exclude-controls"]))
    return include - exclude, digest


def _local_reference(path, output):
    try:
        return quote(os.path.relpath(path, output.parent.resolve()), safe="/")
    except ValueError:
        raise InvalidReport("Put the Assessment Plan, exchange and output on the same filesystem volume.") from None


def _props(values):
    return [{"name": name, "value": str(value), "ns": NAMESPACE} for name, value in values.items()]


def _observation(description, *, stamp, evidence, attributes):
    return {
        "uuid": str(uuid4()), "description": description, "methods": ["EXAMINE"], "collected": stamp,
        "relevant-evidence": [{"href": href, "description": "Reference inside the authenticated Setrya exchange."}
                              for href in evidence],
        "props": _props(attributes),
    }


def _outcome(failures, gaps):
    return "FAIL" if failures else "NOT TESTED" if gaps else "PASS"


def _totals(statuses, unit):
    counts = Counter(statuses)
    return {"check-outcome": _outcome(counts["FAIL"], counts["NOT TESTED"] or not counts),
            "check-unit": unit, "passing-checks": counts["PASS"],
            "failing-checks": counts["FAIL"], "untested-checks": counts["NOT TESTED"]}


def _receipt_status(receipt):
    verdict = receipt["technical_verdict"]
    return "NOT TESTED" if verdict == "ERROR" else verdict


def _artifact_outcomes(data, declared):
    indexes = {"stored": 0, "discovered": 0}
    for artifact in declared:
        slot = "discovered" if artifact["discovered_by"] == "canary" else "stored"
        index = indexes[slot]
        indexes[slot] += 1
        before = data["baseline"][slot][index]
        for phase in ("immediate", "final"):
            stage = data[phase]
            reading = stage[slot][index] if stage is not None else None
            failures, gaps = Findings(), set()
            stored_outcome(reading, artifact, before, phase, failures, gaps)
            yield artifact["action"], _outcome(failures, gaps)


def _artifact_checks(receipt, data):
    artifacts = receipt["artifact_checks"]
    results = list(_artifact_outcomes(data, artifacts))
    for action in ("delete", "preserve", "rewrite"):
        declared = [item for item in artifacts if item["action"] == action]
        if not declared:
            continue
        observed = [item for item in declared if any("#/immediate/" in ref or "#/final/" in ref
                                                       for ref in item["evidence"])]
        maintenance = receipt.get("maintenance_checks", ())
        maintenance_reads = [row for phase in maintenance for row in phase["checks"] if row["action"] == action]
        recorded = sum(row["evidence"] is not None for row in maintenance_reads)
        references = [RECEIPT + "#/artifact_checks"]
        counts = {"declared-artifacts": len(declared), "observed-artifacts": len(observed)}
        statuses = [status for rule, status in results if rule == action]
        if maintenance and any(row["action"] == action for row in data["case"]["artifacts"]):
            references.append(RECEIPT + "#/maintenance_checks")
            counts["recorded-maintenance-reads"] = recorded
            steps = sum(not phase["checks"] for phase in maintenance)
            slots = sum(row["action"] == action for row in data["case"]["artifacts"])
            maintenance_statuses = [row["status"] for row in maintenance_reads] + ["NOT TESTED"] * (steps * slots)
            statuses.extend(maintenance_statuses)
            for status, name in (("PASS", "passing"), ("FAIL", "failing"), ("NOT TESTED", "untested")):
                counts[f"{name}-maintenance-checks"] = maintenance_statuses.count(status)
        counts.update(_totals(statuses, "artifact-phase"))
        yield f"artifact-{action}", (
            f"Direct post-erasure checks for artifacts with the {action} rule.",
            references, counts,
        )


def _endpoint_outcomes(data, phase):
    expected = list(answer_slots(data["case"]))
    readings = answer_records(data, phase) if data[phase] is not None else repeat(None, len(expected))
    for (caller, _, role, _), reading in zip(expected, readings, strict=True):
        failures, gaps = Findings(), set()
        answer_outcome(reading, caller, role == "control", phase, failures, gaps)
        yield _outcome(failures, gaps)


def _scan_outcomes(data, phase):
    baseline, stage = data["baseline"], data[phase]
    for store in data["case"]["scan_stores"]:
        hits = stage["scans"][store] if stage is not None else None
        if hits:
            yield "FAIL"
        elif hits is None or (any(store in row.get("scan_details", {}) for row in (baseline, stage))
                              and not same_scope(baseline, stage, store)):
            yield "NOT TESTED"
        else:
            yield "PASS"


def _phase_checks(receipt, data):
    evidence_file = receipt["evidence"]["file"]
    for check, field, description, outcomes, unit in (
        ("endpoint-read", "answers", "Post-erasure endpoint checks.", _endpoint_outcomes, "answer-sample"),
        ("store-scan", "scans", "Post-erasure canary scan checks.", _scan_outcomes, "scan-phase"),
    ):
        traces, slots, statuses = [], [], []
        for phase in ("immediate", "final"):
            statuses.extend(outcomes(data, phase))
            if current := data[phase]:
                pointer = f"{evidence_file}#/{phase}/{field}"
                slots.append(pointer)
                readings = current[field].values() if field == "scans" else answer_records(data, phase)
                if any(row is not None for row in readings):
                    traces.append(pointer)
        if statuses:
            yield check, (description, traces or slots, {"recorded-phases": len(traces), **_totals(statuses, unit)})


def _has_recorded_check(details):
    if "observed-artifacts" in details:
        return bool(details["observed-artifacts"] or details.get("recorded-maintenance-reads"))
    return details.get("recorded-phases", 1) > 0


def _checks(verified):
    """Project per-check outcomes from authenticated, replayed evidence."""
    manifest, receipt, data = verified.manifest, verified.receipt, verified.observations
    kind = manifest["evidence_kind"]
    verdict_ref = [RECEIPT + "#/technical_verdict"]
    if kind == "read-only-observation":
        statuses = [row["status"] for row in receipt["checks"]]
        outcomes = {"ABSENT": "PASS", "PRESENT": "FAIL", "UNKNOWN": "NOT TESTED"}
        return {"read-only-observation": (
            "Read-only checks of journalled locations for a completed deletion request.",
            [RECEIPT + "#/checks"] if statuses else verdict_ref,
            {"absent-reads": statuses.count("ABSENT"), "present-reads": statuses.count("PRESENT"),
             "unknown-reads": statuses.count("UNKNOWN"),
             **_totals((outcomes[status] for status in statuses), "journalled-key")},
        )}
    if kind == "provider-readback" or (kind == "vendor-attestation" and "readback" in receipt):
        measured, *uninspected = receipt["checks"]
        return {"provider-readback": (
            "Declared provider API readback after an operator-reported deletion.",
            [RECEIPT + "#/checks/0", RECEIPT + "#/readback"],
            {"provider-target": receipt["target"], "readback-surface": measured["surface"],
             "scope-sha256": receipt["readback"]["scope_sha256"], "readback-baseline-at": receipt["readback"]["baseline_at"],
             "uninspected-surfaces": len(uninspected),
             **_totals([measured["status"]], "api-surface")},
        )}
    if kind == "vendor-attestation":
        statuses = [row["status"] for row in receipt["checks"]]
        return {"vendor-conformance": (
            "Vendor conformance outcomes reported by the authenticated exchange issuer.",
            [RECEIPT + "#/checks"],
            {"check-kind": manifest["vendor"]["kind"], "passing-surfaces": statuses.count("PASS"),
             "failing-surfaces": statuses.count("FAIL"), "untested-surfaces": statuses.count("NOT TESTED"),
             **_totals(statuses, "vendor-surface")},
        )}
    checks = {"synthetic-erasure": ("Recorded result of a synthetic erasure drill.", verdict_ref,
                                   _totals([_receipt_status(receipt)], "replayed-drill"))}
    if data["erased"] is not True:
        return checks
    checks.update(_artifact_checks(receipt, data))
    checks.update(_phase_checks(receipt, data))
    if receipt["race"] == "exercised":
        checks["write-back"] = (
            "Replay result for a drill that released and drained the same deferred writer across erasure.",
            [f"{receipt['evidence']['file']}#/worker", *verdict_ref],
            {"race": "exercised", **_totals([_receipt_status(receipt)], "replayed-drill")},
        )
    return checks

def export_results(directory, *, assessment_plan, mapping, output, ticket=None, expect_plan_sha256=None,
                   expect_mapping_sha256=None, **trust):
    """Write a local OSCAL result that points to the verified signed exchange."""
    directory = Path(directory).absolute()
    plan_path = Path(assessment_plan).resolve()
    output = Path(output).absolute()
    require(not output.resolve().is_relative_to(directory.resolve()), "Write the OSCAL result outside the signed exchange.")
    selected, plan_hash = _read_plan(plan_path)
    registry, mapping_hash = _read_mapping(mapping)
    for expected, actual, label in ((expect_plan_sha256, plan_hash, "Assessment Plan"),
                                    (expect_mapping_sha256, mapping_hash, "Control mapping")):
        require(expected is None or sha(expected) == actual, f"{label} differs from the independently supplied digest.")
    require(ticket is None or _label(ticket), "Expected an opaque governance ticket reference.")
    verified = verify_handoff(directory, **trust)
    manifest, receipt = verified.manifest, verified.receipt
    checks = _checks(verified)
    in_scope = [row for row in registry["supports"] if row["control_id"] in selected and row["check"] in checks]
    reviewed_controls = {row["control_id"] for row in in_scope if _has_recorded_check(checks[row["check"]][2])}
    require(reviewed_controls,
            "No mapping supports a recorded check within the Assessment Plan's explicit control selection.")
    original_controls = {(row["framework"], row["reference"]) for row in receipt.get("controls", [])}
    bundle_ref = _local_reference(directory.resolve(), output)
    collected = manifest["observation"]["last_checked_at"]
    evidence_file = receipt["evidence"]["file"]
    attributes = {
        "technical-verdict": manifest["technical_verdict"], "review-status": manifest["review_status"],
        "evidence-kind": manifest["evidence_kind"], "system": manifest["system"]["name"],
        "system-version": manifest["system"]["version"], "exchange-verified-at": verified.verified_at,
        "receipt-sha256": manifest["files"][RECEIPT], "evidence-sha256": manifest["files"][evidence_file],
        "mapping-version": registry["version"], "mapping-sha256": mapping_hash, "assessment-plan-sha256": plan_hash,
        **{f"exchange-signer-{key}": value for key, value in verified.signer.items()},
    }
    if ticket:
        attributes["governance-ticket"] = ticket
    if manifest["ticket"]:
        attributes.update({"completed-request-id": manifest["ticket"]["id"],
                           "completed-request-frame-sha256": manifest["ticket"]["frame_sha256"]})
    verdict = {key: attributes[key] for key in ("technical-verdict", "review-status")}
    observations = []
    for row in in_scope:
        description, refs, details = checks[row["check"]]
        source = "receipt-bound" if (row["framework"], row["reference"]) in original_controls else "reviewer-supplied"
        link = "supports" if _has_recorded_check(details) else "mapped-control"
        properties = {
            "check": row["check"], link: row["control_id"],
            "framework": row["framework"], "edition": row["edition"],
            "control-reference": row["reference"], "control-reference-source": source,
            "mapping-applicability": row["applicability"], "mapping-source": row["source"],
            **verdict, **details,
        }
        observations.append(_observation(
            f"{details['check-outcome']} · {description} Receipt verdict {verdict['technical-verdict']}.",
            stamp=collected, evidence=[f"{bundle_ref}/{ref}" for ref in refs], attributes=properties,
        ))
    for item in manifest["open_items"]:
        properties = {f"open-item-{key.replace('_', '-')}": value for key, value in item.items() if key != "evidence"}
        observations.append(_observation(
            f"Open item recorded in the signed exchange. Receipt verdict {verdict['technical-verdict']}.",
            stamp=collected, evidence=[f'{bundle_ref}/{item["evidence"]}'], attributes={**verdict, **properties},
        ))
    controls = [{"control-id": value} for value in sorted(reviewed_controls)]
    result = {
        "assessment-results": {
            "uuid": str(uuid4()),
            "metadata": {"title": "Setrya evidence for control review", "last-modified": datetime.now(UTC).isoformat(),
                         "version": __version__, "oscal-version": VERSION},
            "import-ap": {"href": _local_reference(plan_path, output)},
            "results": [{
                "uuid": str(uuid4()), "title": "Verified Setrya evidence",
                "description": "Recorded evidence supports the selected controls within the observed scope. Control conclusions are left to the assessor.",
                "start": manifest["observation"].get("first_checked_at", collected), "end": collected,
                "reviewed-controls": {"control-selections": [{"include-controls": controls}]},
                "props": _props(attributes), "links": [{"rel": "evidence", "href": f"{bundle_ref}/{MANIFEST}"}],
                "observations": observations,
            }],
        },
    }
    publish_new(result, output, max_bytes=MAX_BUNDLE_FILE_BYTES)
    return result
