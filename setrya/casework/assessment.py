"""Combine scoped copy coverage and replayed deletion receipts into a signed report."""

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from setrya import __version__
from setrya.core import signing
from setrya.core.inputs import (
    MAX_JSON_BYTES,
    publish_new_bytes,
    read_regular_bytes,
    require,
)
from setrya.evidence import bundle_manifest, git_revision
from setrya.receipt_format import MAX_BUNDLE_FILE_BYTES
from setrya.receipts import replay_bytes

from .profiles import decode, fields, new_bundle, snapshot, stamp

FORMAT = "setrya.assessment.v1"
CLIENT_FORMAT = "setrya.assessment.v2"
PLAN = "setrya.assessment-input.v1"
REPORT = "assessment.json"
SIGNATURE = "assessment.sig"
SUMMARY = "summary.md"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_VERDICTS = {"PASS", "FAIL", "NOT TESTED", "ERROR"}
_REVIEWS = {"READY_FOR_REVIEW", "RESIDUAL_FOUND", "OPEN_ITEMS"}

# These input checks report different kinds of gaps. Keep their counts separate.
_CHECKS = {
    "setrya.copy-scan.v1": ("sites", None, None),
    "setrya.copy-review.v1": ("sites", "candidate_commit", "needs-review"),
    "setrya.route-coverage.v1": ("surfaces", "candidate_commit", "gap"),
    "journal-coverage": ("boundaries", None, "unmapped"),
}
_COPY_CHECKS = {"setrya.copy-scan.v1", "setrya.copy-review.v1"}


def _check_languages(kind, languages):
    require(kind in _COPY_CHECKS and languages in (["typescript"], ["python", "typescript"]),
            "Invalid copy-check language scope.")


def _digest(raw):
    return sha256(raw).hexdigest()


def _label(value):
    require(isinstance(value, str) and _LABEL.fullmatch(value), "Use an opaque repository or system label.")
    return value


def _listed(items, limit, label):
    require(isinstance(items, list) and len(items) <= limit, f"Supply at most {limit} {label}.")
    return items


def _source(directory, value):
    require(isinstance(value, str) and 0 < len(value) <= 2048, "Supply a local input path.")
    return directory / value


def _check(path, commit, *, detailed=False):
    raw = snapshot(path, MAX_JSON_BYTES)
    data = decode(raw)
    require(isinstance(data, dict), "Expected a coverage or scan object.")
    kind = data.get("format", "journal-coverage")
    require(isinstance(kind, str) and kind in _CHECKS, "Unsupported coverage or scan format.")
    entries, commit_field, policy = _CHECKS[kind]
    if commit_field:
        require(data.get(commit_field) == commit, "Coverage candidate differs from the assessment commit.")
    rows = _listed(data.get(entries), 4096, "coverage rows")
    require(all(isinstance(row, dict) for row in rows), "Invalid coverage row.")
    if policy == "needs-review":
        require(all(row.get("status") in ("needs-review", "evidence-replayed") for row in rows),
                "Invalid copy-site coverage status.")
        gap_count = sum(row["status"] == policy for row in rows)
        require(data.get("review_required") == gap_count, "Copy review count differs from its rows.")
    elif policy == "gap":
        require(all(row.get("status") in _VERDICTS for row in rows), "Invalid route coverage status.")
        gap_count = sum(row["status"] != "PASS" for row in rows)
    elif policy == "unmapped":
        require(set(data) == {"status", "boundaries"} and all(type(row.get("mapped")) is bool for row in rows),
                "Invalid journal coverage status.")
        gap_count = sum(not row["mapped"] for row in rows)
        require(data["status"] == ("MAPPED" if rows and not gap_count else "NOT TESTED"),
                "Journal coverage status differs from its rows.")
    else:
        gap_count = len(rows)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    result = {"kind": kind, "input_sha256": _digest(raw), "report_sha256": _digest(canonical),
              "entries": len(rows), "gaps": gap_count,
              "commit_binding": "reported" if commit_field else "unbound"}
    if "languages" in data:
        _check_languages(kind, data["languages"])
        result["languages"] = data["languages"]
    if detailed:
        result["scope_ids"] = sorted({identity for row in rows if (identity := _check_scope(kind, row)) is not None})
    return result


def _check_scope(kind, row):
    if kind == "setrya.copy-review.v1":
        site = row.get("site_id")
        return site if isinstance(site, str) and _SHA256.fullmatch(site) else None
    if kind == "setrya.route-coverage.v1" and "surface" in row:
        identity = [row.get("component"), row.get("path"), row["surface"]]
        if all(isinstance(value, str) and value for value in identity[:2]) and (
                identity[2] is None or isinstance(identity[2], str)):
            return _digest(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode())
    if kind == "journal-coverage":
        identity = [row.get(field) for field in ("store", "kind", "name")]
        if all(isinstance(value, str) and value for value in identity):
            return _digest(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode())
    return None


def _receipt(path, commit, *, detailed=False, expected_deployment=None):
    require(path.is_dir() and not path.is_symlink(), "Supply a receipt directory.")
    name = bundle_manifest(path)
    require(name in ("erasure-receipt.json", "case-result.json"), "Expected an erasure or discovery receipt.")
    raw = snapshot(path / name, MAX_BUNDLE_FILE_BYTES)
    result = replay_bytes(path, raw)
    if expected_deployment is not None:
        _check_deployment(result, expected_deployment)
    verdict, review = result.get("technical_verdict"), result.get("review_status")
    require(verdict in _VERDICTS and review in _REVIEWS, "Expected a technical verdict and review status.")
    captured = result.get("provenance", {})
    app = captured.get("application", {})
    revision = app.get("checkout_commit")
    files = captured.get("test_files", ())
    clean_starter = (app.get("execution") == "local-starter" and app.get("checkout_state") == "clean"
                     and app.get("asserted_deploy_commit") is None
                     and files and all(row.get("commit_bound") is True for row in files))
    if revision is not None and clean_starter:
        require(revision == commit, "Replayed receipt checkout differs from the assessment commit.")
        binding = "receipt-checkout"
    else:
        binding = "unbound"
    evidence = result.get("evidence", result.get("erasure_receipt", {}))
    row = {"receipt_sha256": _digest(raw), "evidence_sha256": evidence.get("sha256"),
            "verdict": verdict, "review": review, "commit_binding": binding,
            "profile": result.get("profile", result.get("scope", "discovery"))}
    if detailed:
        from .assessment_details import details

        row["details"] = details(result, path)
    return row


def _check_deployment(receipt, expected):
    from setrya.core.deployment import check_pin

    check_pin(receipt.get("deployment"), expected)



def _hash(value):
    require(isinstance(value, str) and _SHA256.fullmatch(value), "Invalid assessment digest.")


def _validate(result):
    fields(result, "format setrya_version issued_at signature_type repositories files", ("client",))
    require(result["format"] in (FORMAT, CLIENT_FORMAT) and result["signature_type"] in ("cosign-detached", "sigstore-bundle"),
            "Unsupported assessment format.")
    detailed = result["format"] == CLIENT_FORMAT
    require(("client" in result) == detailed, "Assessment format and client scope disagree.")
    if detailed:
        _label(result["client"])
    _label(result["setrya_version"])
    stamp(result["issued_at"])
    fields(result["files"], SUMMARY)
    _hash(result["files"][SUMMARY])
    names = set()
    repositories = _listed(result["repositories"], 32, "repositories")
    require(repositories, "Supply at least one repository.")
    for repo in repositories:
        fields(repo, "name commit checks receipts")
        name = _label(repo["name"])
        require(name not in names, "Duplicate repository label.")
        names.add(name)
        git_revision(repo["commit"])
        check_digests = set()
        for row in _listed(repo["checks"], 8, "checks per repository"):
            fields(row, "kind input_sha256 report_sha256 entries gaps commit_binding" + (" scope_ids" if detailed else ""),
                   ("languages",))
            require(row["kind"] in _CHECKS and row["commit_binding"] in ("reported", "unbound")
                    and type(row["entries"]) is int and type(row["gaps"]) is int
                    and 0 <= row["gaps"] <= row["entries"] <= 4096, "Invalid assessment coverage record.")
            if "languages" in row:
                _check_languages(row["kind"], row["languages"])
            _hash(row["input_sha256"])
            _hash(row["report_sha256"])
            if detailed:
                scope = _listed(row["scope_ids"], row["entries"], "coverage scope identifiers")
                for identity in scope:
                    _hash(identity)
                require(scope == sorted(set(scope)), "Coverage scope identifiers must be distinct and ordered.")
            require(row["report_sha256"] not in check_digests, "Duplicate coverage report digest.")
            check_digests.add(row["report_sha256"])
        receipt_digests = set()
        for receipt in _listed(repo["receipts"], 24, "receipts per repository"):
            fields(receipt, "receipt_sha256 evidence_sha256 verdict review commit_binding profile" + (" details" if detailed else ""))
            if detailed:
                from .assessment_details import validate_details

                validate_details(receipt["details"])
                require(not receipt["details"]["residual"] or receipt["verdict"] == "FAIL",
                        "Assessment residual conflicts with its verdict.")
                require(not receipt["details"]["open_items"] or receipt["review"] != "READY_FOR_REVIEW",
                        "Assessment follow-ups conflict with its review status.")
                deployment = receipt["details"]["deployment"]
                require(deployment is None or deployment["status"] == "verified" or receipt["verdict"] != "PASS",
                        "An unverified deployment cannot produce a passing assessment observation.")
            require(receipt["verdict"] in _VERDICTS and receipt["review"] in _REVIEWS
                    and receipt["commit_binding"] in ("receipt-checkout", "unbound"),
                    "Invalid assessment receipt result.")
            _label(receipt["profile"])
            _hash(receipt["receipt_sha256"])
            source_digest = receipt["evidence_sha256"]
            if source_digest is not None:
                _hash(source_digest)
            identity = source_digest or receipt["receipt_sha256"]
            require(identity not in receipt_digests, "Duplicate receipt evidence digest.")
            receipt_digests.add(identity)


def _summary(result):
    lines = ["# Deletion assessment", "", f"Issued {result['issued_at']} · Setrya {result['setrya_version']}", "",
             "Repository checks below describe the supplied evidence. A coverage map identifies where to test.",
             ""]
    if "client" in result:
        lines.extend((f"Client `{result['client']}`", ""))
    for repo in result["repositories"]:
        label = repo["name"]
        receipts = repo["receipts"]
        counts = Counter(row["verdict"] for row in receipts)
        lines.extend([f"## {label}", "", f"Commit `{repo['commit']}`", ""])
        verdicts = ", ".join(f"{counts[state]} {state}" for state in ("PASS", "FAIL", "NOT TESTED", "ERROR"))
        unbound = sum(row["commit_binding"] == "unbound" for row in receipts)
        open_items = sum(row["review"] == "OPEN_ITEMS" for row in receipts)
        if receipts:
            lines.append(f"- Replayed receipts — {verdicts}. Without a clean checkout binding — {unbound}. "
                         f"With open review items — {open_items}.")
        else:
            lines.append("- Replayed receipts — none supplied.")
        for check in repo["checks"]:
            label = {"setrya.copy-scan.v1": "Copy inventory", "setrya.copy-review.v1": "Copy review",
                     "setrya.route-coverage.v1": "Route coverage", "journal-coverage": "Journal coverage"}[check["kind"]]
            if "languages" in check:
                language = "Python and TypeScript" if "python" in check["languages"] else "TypeScript"
                label += f" ({language})"
            if check["entries"]:
                lines.append(f"- {label} — {check['gaps']} of {check['entries']} supplied "
                             "entries need review or evidence.")
            else:
                lines.append(f"- {label} — no entries supplied by this input.")
        if not repo["checks"]:
            lines.append("- Coverage — no scans or coverage checks supplied.")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def build(plan, output, *, key=None, expected_deployment=None):
    plan = Path(plan)
    source = decode(snapshot(plan, MAX_JSON_BYTES))
    fields(source, "format repositories", ("client",))
    require(source["format"] == PLAN, "Expected a Setrya assessment input file.")
    client = _label(source["client"]) if "client" in source else None
    require(expected_deployment is None or client is not None, "Supply a client label for a deployment-pinned assessment.")
    repos = []
    seen = set()
    for item in _listed(source["repositories"], 32, "repositories"):
        fields(item, "name commit checks receipts")
        name, commit = _label(item["name"]), git_revision(item["commit"])
        require(name not in seen, "Duplicate repository label.")
        seen.add(name)
        checks = _listed(item["checks"], 8, "checks per repository")
        receipts = _listed(item["receipts"], 24, "receipts per repository")
        repos.append({"name": name, "commit": commit,
                      "checks": [_check(_source(plan.parent, path), commit, detailed=client is not None) for path in checks],
                      "receipts": [_receipt(_source(plan.parent, path), commit, detailed=client is not None,
                                            expected_deployment=expected_deployment) for path in receipts]})
    require(repos, "Supply at least one repository.")
    if expected_deployment is not None:
        require(any(repo["receipts"] for repo in repos), "No receipt was supplied for deployment review.")
    result = {"format": CLIENT_FORMAT if client is not None else FORMAT,
              **({"client": client} if client is not None else {}),
              "setrya_version": __version__, "issued_at": datetime.now(UTC).isoformat(),
              "signature_type": "cosign-detached" if key else "sigstore-bundle", "repositories": repos}
    summary = _summary(result)
    result["files"] = {SUMMARY: _digest(summary)}
    _validate(result)
    raw = (json.dumps(result, indent=2) + "\n").encode()
    with new_bundle(output) as directory:
        publish_new_bytes(raw, directory / REPORT)
        publish_new_bytes(summary, directory / SUMMARY)
        signing.sign_bytes(raw, directory / SIGNATURE, key=key)
    return result


def read_signed(directory, *, key=None, identity=None, issuer=None, trusted_root=None, expected_deployment=None):
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), "Supply an assessment directory.")
    require(not key or not any((identity, issuer, trusted_root)), "Choose one signer policy.")
    require(key or (identity and issuer and trusted_root), "Supply independently trusted signer information.")
    raw = snapshot(directory / REPORT, MAX_JSON_BYTES)
    signature = snapshot(directory / SIGNATURE, MAX_JSON_BYTES if key else signing.MAX_SIGSTORE_BYTES)
    root = read_regular_bytes(trusted_root, "trusted root", signing.MAX_SIGSTORE_BYTES) if trusted_root else None
    signing.verify_bytes(raw, signature, key=key, identity=identity, issuer=issuer, trusted_root=root)
    manifest = decode(raw)
    _validate(manifest)
    require(manifest["signature_type"] == ("cosign-detached" if key else "sigstore-bundle"),
            "Unsupported assessment signer policy.")
    summary = snapshot(directory / SUMMARY, MAX_JSON_BYTES)
    require(manifest["files"] == {SUMMARY: _digest(summary)} and summary == _summary(manifest),
            "Summary differs from the signed assessment.")
    if expected_deployment is not None:
        rows = [row["details"] for repo in manifest["repositories"] for row in repo["receipts"]
                if "details" in row]
        require(rows, "No receipts with deployment evidence were supplied.")
        for details in rows:
            _check_deployment(details, expected_deployment)
    return manifest, _digest(raw), summary


def verify(directory, **options):
    return read_signed(directory, **options)[2].decode()


def command(argv):
    parser = argparse.ArgumentParser(prog="setrya assess", description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    issue = sub.add_parser("build", help="replay inputs and sign a repository assessment")
    issue.add_argument("--plan", required=True, type=Path)
    issue.add_argument("--output", required=True, type=Path)
    issue.add_argument("--key", type=Path, help="local Cosign signing key (default: Sigstore keyless)")
    issue.add_argument("--expect-deployment", type=Path)
    inspect = sub.add_parser("verify", help="verify the signer and summary")
    inspect.add_argument("directory", type=Path)
    inspect.add_argument("--key", type=Path)
    inspect.add_argument("--identity")
    inspect.add_argument("--issuer")
    inspect.add_argument("--trusted-root", type=Path)
    inspect.add_argument("--expect-deployment", type=Path)
    from .assessment_comparison import add_commands, dispatch

    add_commands(sub)
    args = parser.parse_args(argv)
    if args.operation in ("compare", "verify-comparison"):
        return dispatch(args)
    expected = None
    if args.expect_deployment is not None:
        from setrya.core.deployment import read_pin

        expected = read_pin(args.expect_deployment)
    if args.operation == "build":
        build(args.plan, args.output, key=args.key, expected_deployment=expected)
        print(f"Signed assessment saved in {args.output}")
    else:
        print(verify(args.directory, key=args.key, identity=args.identity, issuer=args.issuer,
                     trusted_root=args.trusted_root, expected_deployment=expected), end="")
    return 0
