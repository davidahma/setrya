"""Replay erasure receipts using the evidence rules under which each was issued."""
import json
import os
from pathlib import Path

from setrya.drill import InputError
from setrya.evidence import bundle_manifest
from setrya.erasure import receipt as v2
from setrya.erasure import v3_receipt as v3
from setrya.offboarding.inputs import MAX_JSON_BYTES, read_regular_bytes, require, unique_fields


def replay_bytes(directory, raw, *, expected_request=None, expected_job=None):
    """Recompute the decision for an exact receipt byte snapshot and its companion evidence."""
    from setrya.discovery import case as discovery
    folder = Path(directory)
    name = bundle_manifest(folder)
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
    require(isinstance(document, dict), "Expected a receipt object.")
    if name == v2.RECEIPT and document.get("format") == v3.RECEIPT_FORMAT and document.get("scope") == "tenant":
        from setrya.offboarding import tenant_v3 as tenant
        return tenant.replay_bytes(folder, raw, expected_request=expected_request, expected_job=expected_job)
    require(expected_request is None and expected_job is None, "Request and job pins apply only to tenant receipts.")
    if name == discovery.RESULT:
        return discovery.replay_bytes(folder, raw)
    if name != v2.RECEIPT:
        raise InputError("Supply an erasure or discovery receipt with its evidence directory.")
    if document.get("format") == v3.RECEIPT_FORMAT:
        return v3.replay_bytes(folder, raw)
    return v2.replay_bytes(folder, raw)


def replay(directory, *, expected_request=None, expected_job=None, records=None):
    """Offline replay. Tenant callers may independently pin the request digest and job ID."""
    folder = Path(directory)
    if folder.is_file():
        path, folder = folder, folder.parent
        if path.name not in (v2.RECEIPT, "case-result.json"):
            return _replay_legacy(path, expected_request, expected_job, records)
    name = bundle_manifest(folder)
    if name is None and (folder / "receipt.json").is_file():
        return _replay_legacy(folder / "receipt.json", expected_request, expected_job, records)
    require(records is None, "Explicit --record inputs apply only to archived v1 receipts.")
    require(name in (v2.RECEIPT, "case-result.json"), "Expected one erasure receipt with its evidence.")
    raw = read_regular_bytes(folder / name, "receipt", v2.MAX_BUNDLE_FILE_BYTES)
    return replay_bytes(folder, raw, expected_request=expected_request, expected_job=expected_job)


def _replay_legacy(path, expected_request, expected_job, records):
    from setrya.offboarding import receipt as legacy
    from setrya.offboarding.inputs import read_json
    from setrya.offboarding.receipt_cli import replay_legacy

    folder = path.parent
    require(bundle_manifest(folder) is None, "Keep each evidence bundle in its own directory.")
    require(expected_request is not None, "Supply the original ticket digest with --expect-request for v1 replay.")
    request, digest = read_json(folder / "request.json", "request")
    require(expected_request == digest, "Request digest does not match the expected digest.")
    legacy.validate_request(request)
    if records is None:
        # Original demo exports use a token ID for each descriptor and exhibit.
        records = [(folder / f"{item['id']}.json", folder / f"{item['id']}.txt")
                   for item in request["items"] if (folder / f"{item['id']}.json").exists()]
    return replay_legacy(request_path=folder / "request.json", policy_path=folder / "policy.json",
                         evidence_path=folder / "evidence.json", expect_request=expected_request,
                         expect_job=expected_job, receipt_path=path, records=records)


def check_scope(receipt, expected_scope):
    """Apply an explicit reviewer scope to an already validated historical or v3 receipt."""
    if expected_scope is None:
        return
    legacy_tenant = receipt.get("version") == 1 and "format" not in receipt
    scope = receipt.get("scope", "tenant" if legacy_tenant else "person")
    require(scope == expected_scope, f"Expected a {expected_scope} receipt.")


def summary(receipt):
    """Render an evaluated receipt, including its engineering verdict and any case obligations."""
    if receipt.get("profile") == "discovery" or receipt.get("format") == "setrya.discovery.result":
        from setrya.discovery.case import summary as discovery_summary
        return discovery_summary(receipt)
    if receipt.get("scope") == "tenant" or receipt.get("version") == 1 and "format" not in receipt:
        from setrya.offboarding.tenant_v3 import summary as tenant_summary
        return tenant_summary(receipt)
    if receipt.get("version") == 3:
        return v3.summary(receipt)
    lines = [f"{receipt['verdict']}  {receipt['unknown_copies']} unknown copies  "
             f"drill duration {receipt['time_to_first_verdict_ms']} ms"]
    lines.extend(f"{copy['status']}  {copy['store']}  {copy['key']}" for copy in receipt["copies"])
    lines.extend(receipt["failures"] + receipt["gaps"])
    lines.extend(f"{error['operation']} {error['error']}" for error in receipt["errors"])
    return "\n".join(lines)


def exit_code(receipt, *, case_review=False):
    if receipt.get("version") == 1 and "format" not in receipt:
        from setrya.offboarding.receipt import EXIT_CODES
        return EXIT_CODES[receipt["review_status"]]
    if "technical_verdict" in receipt:
        return v3.exit_code(receipt, case_review=case_review)
    return v2.EXIT[receipt["verdict"]]


def report(receipt, *, case_review=False):
    print(summary(receipt))
    return exit_code(receipt, case_review=case_review)


def signature_command(args):
    """Verify unchanged bytes with an independent Cosign trust policy after replay."""
    from setrya.offboarding import signature
    from setrya.offboarding.receipt_schema import validate_receipt

    def validate(raw):
        header = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
        if isinstance(header, dict) and header.get("version") == 1 and "format" not in header:
            # A v1 signature authenticates only its receipt. Historical replay takes
            # the original ticket digest, policy, evidence and exhibits separately.
            require(len(raw) <= MAX_JSON_BYTES, "The v1 receipt exceeds the 1 MiB limit.")
            validate_receipt(header)
            check_scope(header, args.scope)
            return
        require(not os.path.lexists(args.receipt.parent / "receipt.json")
                and args.receipt.name == bundle_manifest(args.receipt.parent),
                "Choose the manifest from the original evidence bundle.")
        receipt = replay_bytes(args.receipt.parent, raw)
        check_scope(receipt, args.scope)

    return signature.run(args, receipt_limit=v2.MAX_BUNDLE_FILE_BYTES, validate=validate)
