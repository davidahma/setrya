"""Export authenticated Setrya observations for an OSCAL assessment review."""

from datetime import UTC, datetime
import os
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from uuid import uuid4

from setrya import __version__
from setrya.core.control_references import REFERENCE
from setrya.core.inputs import InvalidReport, publish_new, read_json, require
from setrya.core.receipt_v3 import answer_records
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
    "endpoint-read", "store-scan", "write-back", "read-only-observation", "vendor-conformance",
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


def _checks(verified):
    """Describe recorded check families without deriving any new verdicts."""
    manifest, receipt, data = verified.manifest, verified.receipt, verified.observations
    kind = manifest["evidence_kind"]
    verdict_ref = [RECEIPT + "#/technical_verdict"]
    if kind == "read-only-observation":
        statuses = [row["status"] for row in receipt["checks"]]
        return {"read-only-observation": (
            "Read-only checks of journalled locations for a completed deletion request.",
            [RECEIPT + "#/checks"] if statuses else verdict_ref,
            {"absent-reads": statuses.count("ABSENT"), "present-reads": statuses.count("PRESENT"),
             "unknown-reads": statuses.count("UNKNOWN")},
        )}
    if kind == "vendor-attestation":
        statuses = [row["status"] for row in receipt["checks"]]
        return {"vendor-conformance": (
            "Vendor conformance outcomes reported by the authenticated exchange issuer.",
            [RECEIPT + "#/checks"],
            {"check-kind": manifest["vendor"]["kind"], "passing-surfaces": statuses.count("PASS"),
             "failing-surfaces": statuses.count("FAIL"), "untested-surfaces": statuses.count("NOT TESTED")},
        )}
    checks = {"synthetic-erasure": ("Recorded result of a synthetic erasure drill.", verdict_ref, {})}
    if data["erased"] is not True:
        return checks
    artifacts = receipt["artifact_checks"]
    for action in ("delete", "preserve", "rewrite"):
        declared = [item for item in artifacts if item["action"] == action]
        observed = [item for item in declared if any("#/immediate/" in ref or "#/final/" in ref
                                                       for ref in item["evidence"])]
        if observed:
            references = [RECEIPT + "#/artifact_checks"]
            counts = {"declared-artifacts": len(declared), "observed-artifacts": len(observed)}
            maintenance_reads = sum(
                row["action"] == action and row["evidence"] is not None
                for phase in receipt.get("maintenance_checks", ()) for row in phase["checks"]
            )
            if maintenance_reads:
                references.append(RECEIPT + "#/maintenance_checks")
                counts["recorded-maintenance-reads"] = maintenance_reads
            checks[f"artifact-{action}"] = (
                f"Recorded direct post-erasure reads for artifacts with the {action} rule.",
                references, counts,
            )
    evidence_file = receipt["evidence"]["file"]
    for check, field, description in (
        ("endpoint-read", "answers", "Recorded post-erasure endpoint observations."),
        ("store-scan", "scans", "Recorded post-erasure canary scan observations."),
    ):
        traces = []
        for stage in ("immediate", "final"):
            if current := data[stage]:
                readings = current[field].values() if field == "scans" else answer_records(data, stage)
                if any(row is not None for row in readings):
                    traces.append(f"{evidence_file}#/{stage}/{field}")
        if traces:
            checks[check] = (description, traces, {"recorded-phases": len(traces)})
    if receipt["race"] == "exercised":
        checks["write-back"] = (
            "Recorded release and drain of the same deferred writer across erasure.",
            [f"{evidence_file}#/worker"], {"race": "exercised"},
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
    require(in_scope, "No mapping supports a recorded check within the Assessment Plan's explicit control selection.")
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
    observations = []
    for row in in_scope:
        description, refs, details = checks[row["check"]]
        source = "receipt-bound" if (row["framework"], row["reference"]) in original_controls else "reviewer-supplied"
        observations.append(_observation(description, stamp=collected, evidence=[f"{bundle_ref}/{ref}" for ref in refs],
                                         attributes={
                                             "check": row["check"], "supports": row["control_id"],
                                             "framework": row["framework"], "edition": row["edition"],
                                             "control-reference": row["reference"], "control-reference-source": source,
                                             "mapping-applicability": row["applicability"], "mapping-source": row["source"],
                                             **details,
                                         }))
    for item in manifest["open_items"]:
        observations.append(_observation("Open item recorded in the signed exchange.", stamp=collected,
                                         evidence=[f'{bundle_ref}/{item["evidence"]}'],
                                         attributes={f"open-item-{key.replace('_', '-')}": value for key, value in item.items()
                                                     if key != "evidence"}))
    controls = [{"control-id": value} for value in sorted({row["control_id"] for row in in_scope})]
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
