"""Draft portable manifest for replaying original person and vendor receipts."""

import json
import re
from datetime import UTC
from hashlib import sha256
from itertools import islice
from pathlib import Path

from setrya.core import signing
from setrya.core.inputs import (
    MAX_JSON_BYTES,
    InvalidReport,
    publish_new_bytes,
    read_regular_bytes,
    require,
    unique_fields,
)
from setrya.core.provenance import verify_pins
from setrya.core.receipt_v3 import derive as derive_drill
from setrya.evidence import bundle_manifest
from setrya.receipt_format import EVIDENCE as DRILL_EVIDENCE
from setrya.receipt_format import FORMAT_V3, MAX_BUNDLE_FILE_BYTES, RECEIPT

from .profiles import EVIDENCE as OBSERVED_EVIDENCE
from .profiles import OBSERVED, VENDOR, derive, new_bundle, snapshot, stamp

FORMAT = "setrya.deletion-receipt-exchange.v1"
MANIFEST = "exchange.json"
SIGNATURE = "exchange.sig"
_SYSTEM_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+:-]{0,95}")
_OBSERVED = "read-only-observation"
_SYNTHETIC = "synthetic-drill"
_VENDOR = "vendor-attestation"
_PROVIDER = "provider-readback"
_VENDOR_KINDS = (_VENDOR, _PROVIDER)
_COMPANIONS = {
    _OBSERVED: OBSERVED_EVIDENCE, _SYNTHETIC: DRILL_EVIDENCE,
    _VENDOR: OBSERVED_EVIDENCE, _PROVIDER: OBSERVED_EVIDENCE,
}
_DIGEST = re.compile(r"[a-f0-9]{64}")
_MANIFEST_FIELDS = {
    "format", "status", "evidence_kind", "scope", "system", "signature_type", "observation", "ticket", "surfaces",
    "technical_verdict", "review_status", "open_items", "files",
}
_VERDICTS = ("PASS", "FAIL", "NOT TESTED", "ERROR")
_REVIEWS = ("READY_FOR_REVIEW", "RESIDUAL_FOUND", "OPEN_ITEMS")


class ExchangeError(InvalidReport):
    """A verification failure with a stable code shared by interoperable verifiers."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f"[{code}] {message}")


def _check(condition, code, message):
    if not condition:
        raise ExchangeError(code, message)


def _decode(raw, code):
    def reject_constant(_):
        raise ValueError("JSON does not support non-finite numbers.")

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields, parse_constant=reject_constant)
    except InvalidReport:
        raise ExchangeError("DUPLICATE_JSON_KEY", "Use unique keys in every exchange JSON file.") from None
    except (ValueError, RecursionError):
        raise ExchangeError(code, "Expected a UTF-8 JSON object.") from None
    _check(isinstance(data, dict), code, "Expected a JSON object.")
    return data


def _bundle_bytes(path, limit=MAX_BUNDLE_FILE_BYTES):
    try:
        return snapshot(path, limit)
    except (InvalidReport, OSError):
        raise ExchangeError("UNEXPECTED_FILES", "Expected exactly the regular files for this exchange.") from None


def _digest(raw):
    return sha256(raw).hexdigest()


def _system(name, version):
    require(all(isinstance(item, str) and _SYSTEM_LABEL.fullmatch(item) for item in (name, version)),
            "System and version must be short ASCII labels without spaces.")
    return {"name": name, "version": version}


def _signature_type(key):
    return "cosign-detached" if key is not None else "sigstore-bundle"


def _receipt_kind(header):
    message = "The exchange needs an original v3 person drill, read-only observation, vendor fixture or provider readback."
    _check(header.get("format") == FORMAT_V3 and header.get("version") == 3, "EVIDENCE_KIND_MISMATCH", message)
    scope, profile = header.get("scope"), header.get("profile")
    if scope == "vendor" and profile == VENDOR:
        return _PROVIDER if "readback" in header else _VENDOR
    _check(scope == "person" and (profile == OBSERVED or "profile" not in header), "EVIDENCE_KIND_MISMATCH", message)
    return _OBSERVED if profile == OBSERVED else _SYNTHETIC


def _source(directory):
    require(directory.is_dir() and not directory.is_symlink() and bundle_manifest(directory) == RECEIPT,
            "Supply a v3 receipt directory.")
    raw = _bundle_bytes(directory / RECEIPT)
    evidence_name = _COMPANIONS[_receipt_kind(_decode(raw, "CLAIMS_MISMATCH"))]
    return raw, evidence_name, _bundle_bytes(directory / evidence_name)


def _open_items(receipt):
    return [
        {"kind": item["kind"], "surface": item["store"], "evidence": f"{RECEIPT}#/open_items/{index}",
         **{name: item[name] for name in ("owner", "follow_up") if item.get(name) is not None}}
        for index, item in enumerate(receipt["open_items"])
    ]


def _file_digests(source):
    raw, evidence_name, evidence = source
    return {RECEIPT: _digest(raw), evidence_name: _digest(evidence)}


def _utc(value):
    return stamp(value).astimezone(UTC).isoformat()


def _replay(source, *, declared_kind=None):
    raw, evidence_name, evidence = source
    header = _decode(raw, "CLAIMS_MISMATCH")
    data = _decode(evidence, "CLAIMS_MISMATCH")
    kind = _receipt_kind(header)
    if kind in _VENDOR_KINDS:
        format_matches = "format" not in data
    else:
        prefix = "setrya.observed-deletion." if kind == _OBSERVED else "setrya.erasure.evidence.v3"
        evidence_format = data.get("format")
        format_matches = isinstance(evidence_format, str) and evidence_format.startswith(prefix)
    legacy_readback = kind == _PROVIDER and declared_kind == _VENDOR
    _check(format_matches and evidence_name == _COMPANIONS[kind] and (declared_kind in (None, kind) or legacy_readback),
           "EVIDENCE_KIND_MISMATCH", "The original receipt, evidence and declared kind must describe the same observation.")
    bound = header.get("evidence")
    _check(isinstance(bound, dict) and bound.get("file") == evidence_name,
           "EVIDENCE_KIND_MISMATCH", "The original receipt names a different kind of evidence.")
    digest = _digest(evidence)
    _check(bound.get("sha256") == digest, "EVIDENCE_DIGEST_MISMATCH", "The evidence differs from the original receipt digest.")
    try:
        if kind == _OBSERVED:
            first_end = stamp(data["find"]["checked_at"])
            next_snapshot = data["verify_absent"]["journal_read_at"]
            _check(next_snapshot is None or first_end <= stamp(next_snapshot), "SNAPSHOT_ORDER",
                   "The second journal snapshot must follow the first completed observation.")
            receipt = derive(OBSERVED, data, digest, None)
        elif kind in _VENDOR_KINDS:
            receipt = derive(VENDOR, data, digest, None)
        else:
            receipt = derive_drill(data, digest)
    except ExchangeError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        raise ExchangeError("CLAIMS_MISMATCH", "The original evidence could not be replayed.") from None
    _verdict_matches(header, receipt)
    _check(_equal(header, receipt), "CLAIMS_MISMATCH", "The original receipt differs from the replayed evidence.")
    return receipt, data


def _verdict_matches(left, right):
    _check(all(left.get(key) == right[key] for key in ("technical_verdict", "review_status")),
           "VERDICT_MISMATCH", "The claimed verdict or review status differs from the evidence.")


def _equal(left, right):
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _project(source, system, signature_type, receipt, data, *, declared_kind=None):
    kind = declared_kind or _receipt_kind(receipt)
    ticket = None
    vendor = {}
    observation = {"last_checked_at": _utc(receipt["observed_at"])}
    if kind == _OBSERVED:
        observation["first_checked_at"] = _utc(data["find"]["checked_at"])
        ticket = {"id": receipt["request_id"], "frame_sha256": receipt["frame_sha256"]}
        stores, routes = sorted(data["request"]["stores"]), []
    elif kind in _VENDOR_KINDS:
        stores, routes = [], []
        vendor["vendor"] = {
            "kind": "api-readback" if "readback" in receipt else "fixture",
            "target": receipt["target"],
            "versions": receipt["versions"],
            "checks": [{"surface": row["surface"], "status": row["status"]} for row in receipt["checks"]],
            **({"execution": receipt["execution"]} if "execution" in receipt else {}),
        }
        if kind == _PROVIDER:
            observation["first_checked_at"] = _utc(receipt["readback"]["baseline_at"])
            vendor["vendor"]["scope_sha256"] = receipt["readback"]["scope_sha256"]
    else:
        case = receipt["case"]
        observation["drill_duration_ms"] = receipt["drill_duration_ms"]
        stores = sorted(set(case["scan_stores"]) | {row["store"] for row in case["artifacts"]})
        routes = [case["route"]]
    return {
        "format": FORMAT, "status": "draft", "evidence_kind": kind, "scope": receipt["scope"], "system": system,
        "signature_type": signature_type, "observation": observation, "ticket": ticket,
        "surfaces": {"stores": stores, "routes": routes},
        "technical_verdict": receipt["technical_verdict"], "review_status": receipt["review_status"],
        "open_items": _open_items(receipt),
        "files": _file_digests(source),
        **vendor,
        **({"deployment": receipt["deployment"]} if "deployment" in receipt else {}),
    }


def pack(directory, *, output, system, version, key=None, synthetic=False):
    """Validate, copy and sign the original evidence into a fresh exchange directory."""
    source = _source(Path(directory))
    require(synthetic == (source[1] == DRILL_EVIDENCE),
            "Use --synthetic only for a person drill with synthetic data.")
    receipt, data = _replay(source)
    manifest = _project(source, _system(system, version), _signature_type(key), receipt, data)
    encoded = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    raw, evidence_name, evidence = source
    with new_bundle(output) as folder:
        publish_new_bytes(raw, folder / RECEIPT, max_bytes=MAX_BUNDLE_FILE_BYTES)
        publish_new_bytes(evidence, folder / evidence_name, max_bytes=MAX_BUNDLE_FILE_BYTES)
        publish_new_bytes(encoded, folder / MANIFEST)
        signing.sign_bytes(encoded, folder / SIGNATURE, key=key)
    return manifest


def _manifest(raw):
    manifest = _decode(raw, "INVALID_MANIFEST")
    kind = manifest.get("evidence_kind")
    is_vendor = kind in _VENDOR_KINDS
    required = _MANIFEST_FIELDS | ({"vendor"} if is_vendor else set())
    optional = {"deployment"} if kind == _SYNTHETIC else set()
    _check(required <= manifest.keys() <= required | optional
           and manifest["format"] == FORMAT and manifest["status"] == "draft"
           and manifest["scope"] == ("vendor" if is_vendor else "person")
           and kind in (_OBSERVED, _SYNTHETIC, *_VENDOR_KINDS)
           and manifest["signature_type"] in ("cosign-detached", "sigstore-bundle")
           and manifest["technical_verdict"] in _VERDICTS and manifest["review_status"] in _REVIEWS,
           "INVALID_MANIFEST", "Unsupported receipt exchange manifest.")
    observation, ticket = manifest["observation"], manifest["ticket"]
    is_observed = kind == _OBSERVED
    timing = {"last_checked_at"}
    if is_observed or kind == _PROVIDER:
        timing.add("first_checked_at")
    elif kind == _SYNTHETIC:
        timing.add("drill_duration_ms")
    evidence_name = _COMPANIONS[kind]
    _check(isinstance(observation, dict) and set(observation) == timing
           and ((isinstance(ticket, dict) and set(ticket) == {"id", "frame_sha256"})
                if is_observed else ticket is None), "INVALID_MANIFEST", "The evidence kind and claim fields disagree.")
    _check(isinstance(manifest["files"], dict) and set(manifest["files"]) == {RECEIPT, evidence_name}
           and all(isinstance(value, str) and _DIGEST.fullmatch(value) for value in manifest["files"].values()),
           "INVALID_MANIFEST", "The evidence kind and signed file digests disagree.")
    _check(isinstance(manifest["system"], dict) and set(manifest["system"]) == {"name", "version"}
           and all(isinstance(label, str) and _SYSTEM_LABEL.fullmatch(label) for label in manifest["system"].values()),
           "INVALID_MANIFEST", "Expected short ASCII system and version labels.")
    return manifest, evidence_name


def _authenticate(folder, raw, *, key, identity, issuer, trusted_root):
    _check((key and not any((identity, issuer, trusted_root))) or (not key and identity and issuer and trusted_root),
           "WRONG_SIGNER", "Supply one independently trusted key or exact Sigstore identity, issuer and root.")
    signature = _bundle_bytes(folder / SIGNATURE, MAX_JSON_BYTES if key else signing.MAX_SIGSTORE_BYTES)
    try:
        root = read_regular_bytes(trusted_root, "trusted root", signing.MAX_SIGSTORE_BYTES) if trusted_root else None
        signing.verify_bytes(raw, signature, key=key, identity=identity, issuer=issuer, trusted_root=root)
    except (InvalidReport, OSError, ValueError):
        raise ExchangeError("WRONG_SIGNER", "The exchange signature could not be authenticated with the supplied trust.") from None


def _assert_pins(receipt, manifest, *, expect_ticket, expect_frame, expected_provenance, expected_app_commit,
                 expected_deployment):
    ticket = manifest["ticket"] or {}
    _check(expect_ticket is None or ticket.get("id") == expect_ticket,
           "TICKET_ID_MISMATCH", "The completed ticket ID differs from the independently supplied ticket.")
    _check(expect_frame is None or ticket.get("frame_sha256") == expect_frame,
           "EXPORT_DIGEST_MISMATCH", "The completed export fingerprint differs from the independently supplied value.")
    if expected_deployment is not None:
        from setrya.core.deployment import verify_pin

        try:
            verify_pin(receipt, expected_deployment)
        except InvalidReport:
            raise ExchangeError("DEPLOYMENT_MISMATCH", "The receipt does not verify the independently supplied deployment.") from None
    try:
        verify_pins(receipt, expected_app_commit=expected_app_commit)
    except InvalidReport:
        raise ExchangeError("APP_COMMIT_MISMATCH", "The receipt does not bind the expected clean application checkout.") from None
    try:
        verify_pins(receipt, expected_provenance=expected_provenance)
    except InvalidReport:
        raise ExchangeError("PROVENANCE_MISMATCH", "The receipt differs from the independently supplied provenance.") from None


def verify(directory, *, key=None, identity=None, issuer=None, trusted_root=None, expect_ticket=None, expect_frame=None,
           expected_provenance=None, expected_app_commit=None, expected_deployment=None):
    """Authenticate and independently replay one exchange with caller-owned trust."""
    manifest, _, _ = verify_source(
        directory, key=key, identity=identity, issuer=issuer, trusted_root=trusted_root,
        expect_ticket=expect_ticket, expect_frame=expect_frame, expected_provenance=expected_provenance,
        expected_app_commit=expected_app_commit, expected_deployment=expected_deployment,
    )
    return manifest


def verify_source(directory, *, key=None, identity=None, issuer=None, trusted_root=None, expect_ticket=None,
                  expect_frame=None, expected_provenance=None, expected_app_commit=None, expected_deployment=None):
    """Return the manifest, receipt and observations from the same verified snapshot."""
    folder = Path(directory)
    _check(folder.is_dir() and not folder.is_symlink(), "UNEXPECTED_FILES", "Supply an exchange bundle directory.")
    raw = _bundle_bytes(folder / MANIFEST, MAX_JSON_BYTES)
    manifest, evidence_name = _manifest(raw)
    signature_type = _signature_type(key)
    _check(manifest["signature_type"] == signature_type, "WRONG_SIGNER", "The exchange uses a different signer policy.")
    _authenticate(folder, raw, key=key, identity=identity, issuer=issuer, trusted_root=trusted_root)
    expected_files = {MANIFEST, SIGNATURE, RECEIPT, evidence_name}
    actual_files = {path.name for path in islice(folder.iterdir(), len(expected_files) + 1)}
    other_evidence = OBSERVED_EVIDENCE if evidence_name == DRILL_EVIDENCE else DRILL_EVIDENCE
    _check(actual_files != (expected_files - {evidence_name} | {other_evidence}),
           "EVIDENCE_KIND_MISMATCH", "The supplied evidence file belongs to a different evidence kind.")
    _check(actual_files == expected_files, "UNEXPECTED_FILES", "Expected exactly the manifest, signature, original receipt and original evidence.")
    source = _bundle_bytes(folder / RECEIPT), evidence_name, _bundle_bytes(folder / evidence_name)
    _check(manifest["files"] == _file_digests(source), "EVIDENCE_DIGEST_MISMATCH", "Exchange file digests differ from the signed manifest.")
    receipt, data = _replay(source, declared_kind=manifest["evidence_kind"])
    result = _project(source, manifest["system"], signature_type, receipt, data,
                      declared_kind=manifest["evidence_kind"])
    _verdict_matches(manifest, result)
    _check(_equal(manifest, result), "CLAIMS_MISMATCH", "Exchange claims differ from the original evidence.")
    _assert_pins(receipt, result, expect_ticket=expect_ticket, expect_frame=expect_frame,
                 expected_provenance=expected_provenance, expected_app_commit=expected_app_commit,
                 expected_deployment=expected_deployment)
    return result, receipt, data


def summary(manifest):
    observation = manifest["observation"]
    system = manifest["system"]
    verdict, review = manifest["technical_verdict"], manifest["review_status"]
    lines = [
        f"Verified exchange · {verdict} · {review}",
        f"{system['name']} {system['version']} · {manifest['evidence_kind']} · draft v1",
        f"Last observation {observation['last_checked_at']}",
    ]
    if manifest["ticket"]:
        lines.append(f"Completed ticket {manifest['ticket']['id']}")
    vendor = manifest.get("vendor")
    if vendor:
        origin = "Provider API readback" if vendor["kind"] == "api-readback" else "Conformance fixture"
        operator = (vendor.get("execution") or {}).get("operator") or "unrecorded"
        lines.extend((f"{origin} · {vendor['target']} · recorded operator {operator}",
                      "Authentication applies to the exchange issuer."))
        versions = ", ".join(f"{name} {version}" for name, version in sorted(vendor["versions"].items()))
        if versions:
            lines.append(f"Recorded versions {versions}")
        if manifest["evidence_kind"] == _PROVIDER:
            lines.extend((f"Baseline {observation['first_checked_at']}",
                          f"Observed API scope SHA-256 {vendor['scope_sha256']}"))
        lines.extend(f"{row['status']} · {row['surface']}" for row in vendor["checks"])
    else:
        lines.append("Declared stores " + ", ".join(manifest["surfaces"]["stores"]))
    lines.extend(f"Declared route {route}" for route in manifest["surfaces"]["routes"])
    for item in manifest["open_items"]:
        owner = f" · owner {item['owner']}" if "owner" in item else ""
        due = f" · due {item['follow_up']}" if "follow_up" in item else ""
        lines.append(f"Open item {item['surface']} · {item['kind']}{owner}{due} · {item['evidence']}")
    return "\n".join(lines)
