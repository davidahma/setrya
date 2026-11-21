"""Bundle and replay request-bound tenant observations using the offboarding policy evaluator."""
import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from setrya.offboarding import receipt as offboarding
from setrya.offboarding.inputs import (
    MAX_JSON_BYTES, keys_match, publish_new, publish_new_bytes,
    read_regular_bytes, require, unique_fields,
)
from setrya.offboarding.receipt_cli import MAX_CLOCK_SKEW, MAX_DOCUMENT_BYTES, MAX_TOTAL_DOCUMENT_BYTES
from setrya.receipt_format import FORMAT_V3 as RECEIPT_FORMAT, MAX_BUNDLE_FILE_BYTES, RECEIPT

MANIFEST = "tenant-input.json"


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)


def _input(folder, name, limit=MAX_JSON_BYTES):
    path = folder / name
    require(not path.is_symlink(), "Evidence files must be regular files in the tenant bundle.")
    return read_regular_bytes(path, name, limit)


def derive(directory, *, expected_request=None, expected_job=None):
    """Recompute both decisions from the original request, policy, job reads and exhibits."""
    folder = Path(directory)
    packet = _input(folder, MANIFEST)
    manifest = _json(packet)
    require(keys_match(manifest, {"format", "version", "request_sha256", "records"})
            and manifest["format"] == "setrya.tenant.input" and type(manifest["version"]) is int
            and manifest["version"] == 1 and type(manifest["records"]) is int
            and 0 <= manifest["records"] <= offboarding.MAX_ITEMS, "Invalid tenant input manifest.")
    request, policy, evidence = (_input(folder, name) for name in ("request.json", "policy.json", "evidence.json"))
    digest = sha256(request).hexdigest()
    require(digest == manifest["request_sha256"] and expected_request in (None, digest),
            "Request digest does not match the expected digest.")

    def records():
        remaining = MAX_TOTAL_DOCUMENT_BYTES
        for i in range(manifest["records"]):
            descriptor = _input(folder, f"record-{i}.json")
            document = _input(folder, f"record-{i}.bin", min(MAX_DOCUMENT_BYTES, remaining))
            require(document, "Record documents must be nonempty.")
            remaining -= len(document)
            yield _json(descriptor), sha256(descriptor).hexdigest(), sha256(document).hexdigest()

    result = offboarding.assess(_json(request), digest, digest, _json(policy), sha256(policy).hexdigest(),
                                 _json(evidence), sha256(evidence).hexdigest(), records())
    require(datetime.fromisoformat(result["observed_at"]) <= datetime.now(UTC) + MAX_CLOCK_SKEW,
            "Observation time is more than five minutes ahead of the verifier clock.")
    require(expected_job in (None, result["job_id"]), "Job identifier does not match the expected job.")
    status = result["technical_verdict"]
    open_items = [{"id": item["id"], "kind": item["kind"], "status": item["status"],
                   "follow_up": item.get("expires_on"), "evidence": f"{RECEIPT}#/items/{i}"}
                  for i, item in enumerate(result["items"]) if item["status"] != "PROVIDER_REPORTED"]
    return {"format": RECEIPT_FORMAT, **result, "version": 3, "scope": "tenant", "run": result["run_id"],
            "technical_verdict": "NOT TESTED" if status == "INCONCLUSIVE" else status, "open_items": open_items,
            "evidence": {"file": MANIFEST, "sha256": sha256(packet).hexdigest()}}


def issue(*, request, expect_request, policy, evidence, output, records=(), expect_job=None):
    """Write a new v3 bundle, requiring the original request digest from the case system."""
    records = tuple(records)
    require(len(records) <= offboarding.MAX_ITEMS, "Too many tenant exhibits.")
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), "Choose a fresh output directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as scratch:
        folder = Path(scratch) / "bundle"
        folder.mkdir()
        for source, name in ((request, "request.json"), (policy, "policy.json"), (evidence, "evidence.json")):
            publish_new_bytes(read_regular_bytes(source, name), folder / name)
        remaining = MAX_TOTAL_DOCUMENT_BYTES
        for i, (descriptor, document) in enumerate(records):
            publish_new_bytes(read_regular_bytes(descriptor, "descriptor"), folder / f"record-{i}.json")
            raw = read_regular_bytes(document, "record document", min(MAX_DOCUMENT_BYTES, remaining))
            publish_new_bytes(raw, folder / f"record-{i}.bin", max_bytes=MAX_DOCUMENT_BYTES)
            remaining -= len(raw)
        publish_new({"format": "setrya.tenant.input", "version": 1,
                     "request_sha256": expect_request, "records": len(records)}, folder / MANIFEST)
        result = derive(folder, expected_request=expect_request, expected_job=expect_job)
        publish_new(result, folder / RECEIPT, max_bytes=MAX_BUNDLE_FILE_BYTES)
        # Claim the destination first. Never replace a concurrently created directory.
        if os.name == "nt":
            folder.rename(output)  # Windows rename refuses an existing destination.
        else:
            output.mkdir()
            try:
                folder.replace(output)
            except OSError:
                output.rmdir()
                raise
    return result


def replay_bytes(directory, raw, **expected):
    result = derive(directory, **expected)
    require(json.dumps(_json(raw), sort_keys=True) == json.dumps(result, sort_keys=True),
            "Receipt does not match the replayed tenant evidence.")
    return result


def summary(result):
    lines = [f"Technical verdict {result['technical_verdict']} · Review status {result['review_status']}",
             f"Tenant {result['target']} · request {result['request_id']} · job {result['job_id']}",
             f"{result['context']} · revision {result['revision']} · observed {result['observed_at']}"]
    lines.extend(f"{p['verdict']}  {p['id']} · {p['scope']} · {p['checkpoint']}" for p in result["probes"])
    for item in result["items"]:
        date = f" · due {item['expires_on']}" if "expires_on" in item else ""
        lines.append(f"{item['status']}  {item['id']} · {item['kind']}{date}")
    return "\n".join(lines)


def command(argv, *, prog):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    for name in ("request", "policy", "evidence", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expect-request", required=True, help="SHA-256 from the independent request ticket")
    parser.add_argument("--expect-job", help="expected ID from the dispatch log")
    parser.add_argument("--record", action="append", nargs=2, type=Path, default=[], metavar=("DESCRIPTOR", "DOCUMENT"))
    parser.add_argument("--case-review", action="store_true")
    opts = parser.parse_args(argv)
    result = issue(request=opts.request, expect_request=opts.expect_request, policy=opts.policy,
                   evidence=opts.evidence, output=opts.output, records=opts.record, expect_job=opts.expect_job)
    return report(result, case_review=opts.case_review)


def report(result, *, case_review=False):
    print(summary(result))
    return exit_code(result, case_review=case_review)


def exit_code(result, *, case_review=False):
    return (offboarding.EXIT_CODES[result["review_status"]] if case_review
            else {"PASS": 0, "FAIL": 1, "NOT TESTED": 3}[result["technical_verdict"]])


def replay(directory, *, expected_request=None, expected_job=None):
    from setrya.evidence import bundle_manifest
    require(bundle_manifest(Path(directory)) == RECEIPT, "Expected one tenant receipt.")
    return replay_bytes(directory, _input(Path(directory), RECEIPT, MAX_BUNDLE_FILE_BYTES),
                        expected_request=expected_request, expected_job=expected_job)
