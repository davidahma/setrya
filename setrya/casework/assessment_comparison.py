"""Attach a signed client release comparison to an existing approval process."""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from setrya.core import signing
from setrya.core.inputs import publish_new_bytes, read_regular_bytes, require

from . import assessment
from .profiles import decode, fields, new_bundle, sha, snapshot, stamp
from .receipt_summary import _markdown

LEGACY_FORMAT = "setrya.assessment-comparison.v1"
FORMAT = "setrya.assessment-comparison.v2"
MANIFEST = "comparison.json"
SIGNATURE = "comparison.sig"
SUMMARY = "approval.md"
CUSTOMER = "customer.md"
_MAX_BYTES = 4 * 1024 * 1024


def _surfaces(rows):
    result = {}
    for row in rows:
        for surface in row["details"]["observed"]:
            key, samples = (tuple(surface[:-1]), surface[-1]) if surface[0] == "query" else (tuple(surface), 1)
            result[key] = max(result.get(key, 0), samples)
    return result


def _label_surface(surface):
    kind, *parts = surface
    if kind == "query":
        route, caller, question, *role = parts
        label = f"Endpoint {route}, caller {caller}, question {question[:12]}"
        if role:
            label += ", retained control" if role[0] == "control" else ", erased target"
        return label
    if kind == "scan":
        return f"Scanner {parts[0]}"
    if kind == "worker":
        return f"Delayed worker on {parts[0]} in {parts[1]}"
    if kind == "observed-copy":
        return f"Store {parts[0]}, key digest {parts[1][:12]}"
    if kind == "maintenance":
        route, conversation, step, store, key, action = parts
        return f"Maintenance step {step} on {route} in {conversation}, {store}/{key} ({action})"
    return f"Copy {parts[0]}/{parts[1]} ({parts[2]})"


def _coverage_changes(before, after):
    for surface in sorted(before.keys() | after.keys()):
        former, current = before.get(surface, 0), after.get(surface, 0)
        if former != current:
            label = _label_surface(surface)
            if surface[0] == "query":
                label += f" · {former} → {current} samples"
            yield current > former, label


def _deployment_rows(rows, *, include_behavior):
    instances = set()
    for row in rows:
        binding = row["details"]["deployment"]
        if binding is not None:
            record = binding["record"]
            instance = (record["environment"], record["origin"], record["build_id"],
                        f"TLS SHA-256 {record['tls_sha256']}", binding["status"])
            if include_behavior:
                instance += (f"Behavior SHA-256 {binding['behavior_sha256']}",)
            instances.add(instance)
    return instances


def _check_coverage(prior, current):
    old = [check for check in prior if check["kind"] != "setrya.copy-scan.v1"]
    new = [check for check in current if check["kind"] != "setrya.copy-scan.v1"]
    missing = Counter(check["kind"] for check in old) - Counter(check["kind"] for check in new)
    lost, unbound = {}, {}
    for kind in sorted({check["kind"] for check in old + new}):
        baseline = [check for check in old if check["kind"] == kind]
        candidate = [check for check in new if check["kind"] == kind]
        former = {identity for check in baseline for identity in check["scope_ids"]}
        remaining = {identity for check in candidate for identity in check["scope_ids"]}
        difference = sum(check["entries"] for check in baseline) - sum(check["entries"] for check in candidate)
        lost[kind] = max(len(former - remaining), difference)
        unbound[kind] = sum(check["entries"] - len(check["scope_ids"]) for check in baseline + candidate)
    lines = [f"- {check['kind']} · {check['gaps']} of {check['entries']} need review." for check in new]
    lines.extend(f"- {kind} · {count} missing from the candidate assessment."
                 for kind, count in sorted(missing.items()))
    lines.extend(f"- {kind} · {count} checked entries lost." for kind, count in lost.items() if count > 0)
    lines.extend(f"- {kind} · {count} entries lacked stable scope identifiers."
                 for kind, count in unbound.items() if count)
    empty_journal = any(check["kind"] == "journal-coverage" and not check["entries"] for check in new)
    if empty_journal:
        lines.append("- journal-coverage · No journal boundaries were observed.")
    prior_languages, current_languages = (
        {language for check in checks if check["kind"] == "setrya.copy-review.v1"
         for language in check.get("languages", ("python",))}
        for checks in (old, new)
    )
    lost_languages = prior_languages - current_languages
    lines.extend(f"- setrya.copy-review.v1 · Candidate did not scan {language}."
                 for language in sorted(lost_languages))
    ready = not (missing or empty_journal or lost_languages or any(lost.values()) or any(unbound.values())
                 or any(check["gaps"] for check in new))
    return lines, ready


def _outputs(result):
    role_aware = result["format"] == FORMAT
    client = result["client"]
    old = {r["name"]: r for r in result["previous"]["assessment"]["repositories"]}
    new = {r["name"]: r for r in result["candidate"]["assessment"]["repositories"]}
    engineering = [f"# Release comparison for {client}", "",
                   f"Approved assessment SHA-256 `{result['previous']['sha256']}`", "",
                   f"Candidate assessment SHA-256 `{result['candidate']['sha256']}`", ""]
    customer = [f"# Deletion results for {client}", ""]
    ready = True
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name, {}), new.get(name, {})
        prior, current = before.get("receipts", []), after.get("receipts", [])
        old_surfaces, new_surfaces = _surfaces(prior), _surfaces(current)
        unknown_roles = role_aware and any(surface[0] == "query" and len(surface) == 4
                                           for surface in old_surfaces.keys() | new_surfaces.keys())
        gained, lost = [], []
        for increased, label in _coverage_changes(old_surfaces, new_surfaces):
            (gained if increased else lost).append(label)
        issues = [item for row in current for item in row["details"]["open_items"]]
        coverage_lines, coverage_ready = _check_coverage(before.get("checks", []), after.get("checks", []))
        before_deployments = _deployment_rows(prior, include_behavior=role_aware)
        after_deployments = _deployment_rows(current, include_behavior=role_aware)
        before_scope = {row[:2] for row in before_deployments}
        after_scope = {row[:2] for row in after_deployments}
        changed_scope = before_scope != after_scope
        shared_scope = before_scope & after_scope
        changed_behavior = role_aware and (
            {(*row[:2], row[-1]) for row in before_deployments if row[:2] in shared_scope}
            != {(*row[:2], row[-1]) for row in after_deployments if row[:2] in shared_scope}
        )
        old_commit, new_commit = before.get("commit", "unavailable"), after.get("commit", "unavailable")
        changed_deployment = bool(after_deployments) and (
            {row[:5] for row in before_deployments} != {row[:5] for row in after_deployments}
        )
        changed_checkout = old_commit != new_commit and any(row["commit_binding"] == "unbound" for row in current)
        unbound = not result.get("deployment_pinned") and (changed_checkout or changed_deployment)
        ready = ready and coverage_ready and bool(new_surfaces) and not (
            lost or issues or changed_scope or changed_behavior or unbound or unknown_roles
        ) and all(row["verdict"] == "PASS" and row["review"] == "READY_FOR_REVIEW" for row in current)
        engineering.extend((f"## {name}", "", f"Commit `{old_commit}` → `{new_commit}`", ""))
        for label, rows in (("New observed coverage", gained), ("Lost observed coverage", lost)):
            engineering.append(f"### {label}")
            engineering.append("")
            engineering.extend(f"- {_markdown(row)}" for row in rows)
            if not rows:
                engineering.append("None recorded.")
            engineering.append("")
        if unknown_roles:
            engineering.extend(("A supplied assessment lacks query roles. Rebuild it from the recorded receipts before approval.", ""))
        departed = before_deployments - after_deployments
        arrived = after_deployments - before_deployments
        if departed or arrived:
            engineering.extend(("### Changed deployment observations", ""))
            for label, rows in (("Previous", departed), ("Candidate", arrived)):
                engineering.extend(f"- {label} · {_markdown(' · '.join(row))}" for row in sorted(rows))
            if changed_scope:
                engineering.append("- The observed environment or origin changed. Review the destination separately.")
            if changed_behavior:
                engineering.append("- The HTTP behavior SHA-256 changed. Review the changed test before approval.")
            engineering.append("")
        if unbound:
            engineering.extend(("Candidate evidence lacks an independent binding to the changed release.", ""))
        if coverage_lines:
            engineering.extend(("### Coverage checks", ""))
            engineering.extend(coverage_lines)
            engineering.append("")
        engineering.extend(("### Candidate verdicts and follow-ups", ""))
        if not current:
            engineering.append("No candidate receipts supplied for this repository.")
        else:
            for row in current:
                detail = row["details"]
                marker = " · Erased data found" if detail["residual"] else ""
                engineering.append(f"- {row['verdict']} · {row['review']}{marker} · `{row['receipt_sha256']}`")
                engineering.extend(f"  - {_markdown(line)}" for line in detail["failures"])
            for item in issues:
                description = f"{item['surface']} · {item['kind']} · Owner {item['owner'] or 'unassigned'}"
                engineering.append(f"- {_markdown(description)} · Due {item['due'] or 'unrecorded'}")
            customer.extend((f"## {name}", ""))
            for row in current:
                customer.extend(_markdown(line) for line in row["details"]["customer_summary"].split("\n"))
                customer.append("")
        engineering.append("")
    disposition = "READY FOR APPROVAL" if ready else "SEND BACK FOR REVIEW"
    engineering[2:2] = [f"**{disposition}**", ""]
    return {SUMMARY: ("\n".join(engineering) + "\n").encode(), CUSTOMER: ("\n".join(customer) + "\n").encode()}


def _validate(result):
    fields(result, "format client issued_at signature_type previous candidate files", ("deployment_pinned",))
    require(result["format"] in (LEGACY_FORMAT, FORMAT) and result["signature_type"] in ("cosign-detached", "sigstore-bundle"),
            "Unsupported comparison format.")
    assessment._label(result["client"])
    stamp(result["issued_at"])
    for name in ("previous", "candidate"):
        entry = result[name]
        fields(entry, "sha256 assessment")
        sha(entry["sha256"])
        assessment._validate(entry["assessment"])
        require(entry["assessment"].get("client") == result["client"], "Assessment client differs from comparison scope.")
    require(result["previous"]["sha256"] != result["candidate"]["sha256"], "Choose two distinct assessments.")
    if "deployment_pinned" in result:
        from setrya.core.deployment import check_pin, validate_record

        validate_record(result["deployment_pinned"])
        rows = [row for repo in result["candidate"]["assessment"]["repositories"] for row in repo["receipts"]]
        require(rows, "Candidate receipt is required for deployment pinning.")
        for row in rows:
            check_pin(row["details"]["deployment"], result["deployment_pinned"])
    require(stamp(result["previous"]["assessment"]["issued_at"]) < stamp(result["candidate"]["assessment"]["issued_at"]),
            "Candidate assessment must follow the approved assessment.")
    fields(result["files"], f"{SUMMARY} {CUSTOMER}")
    for digest in result["files"].values():
        sha(digest)


def compare(previous, candidate, *, client, approved_sha256, output, key=None, trust, expected_deployment=None):
    sha(approved_sha256)
    old, old_digest, _ = assessment.read_signed(previous, **trust)
    require(old_digest == approved_sha256, "Previous assessment differs from the independently approved digest.")
    new, new_digest, _ = assessment.read_signed(candidate, **trust, expected_deployment=expected_deployment)
    result = {"format": FORMAT, "client": client, "issued_at": datetime.now(UTC).isoformat(),
              "signature_type": "cosign-detached" if key else "sigstore-bundle",
              "previous": {"sha256": old_digest, "assessment": old},
              "candidate": {"sha256": new_digest, "assessment": new},
              "files": {SUMMARY: "0" * 64, CUSTOMER: "0" * 64}}
    if expected_deployment is not None:
        result["deployment_pinned"] = expected_deployment
    _validate(result)
    files = _outputs(result)
    result["files"] = {name: assessment._digest(contents) for name, contents in files.items()}
    raw = (json.dumps(result, indent=2) + "\n").encode()
    require(len(raw) <= _MAX_BYTES and all(len(data) <= _MAX_BYTES for data in files.values()),
            "Comparison packaging exceeds the evidence limit.")
    with new_bundle(output) as folder:
        publish_new_bytes(raw, folder / MANIFEST, max_bytes=_MAX_BYTES)
        for name, data in files.items():
            publish_new_bytes(data, folder / name, max_bytes=_MAX_BYTES)
        signing.sign_bytes(raw, folder / SIGNATURE, key=key)
    return files[SUMMARY].decode()


def verify(directory, *, key=None, identity=None, issuer=None, trusted_root=None):
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), "Supply a comparison directory.")
    require(not key or not any((identity, issuer, trusted_root)), "Choose one signer policy.")
    require(key or (identity and issuer and trusted_root), "Supply independently trusted signer information.")
    raw = snapshot(directory / MANIFEST, _MAX_BYTES)
    signature = snapshot(directory / SIGNATURE, signing.MAX_SIGSTORE_BYTES)
    root = read_regular_bytes(trusted_root, "trusted root", signing.MAX_SIGSTORE_BYTES) if trusted_root else None
    signing.verify_bytes(raw, signature, key=key, identity=identity, issuer=issuer, trusted_root=root)
    result = decode(raw)
    _validate(result)
    require(result["signature_type"] == ("cosign-detached" if key else "sigstore-bundle"), "Wrong comparison signer policy.")
    files = _outputs(result)
    for name, data in files.items():
        require(result["files"][name] == assessment._digest(data) and snapshot(directory / name, _MAX_BYTES) == data,
                "An attachment differs from the signed comparison.")
    return files[SUMMARY].decode()


def add_commands(sub):
    compare_parser = sub.add_parser("compare", help="sign a client release comparison")
    compare_parser.add_argument("previous", type=Path)
    compare_parser.add_argument("candidate", type=Path)
    compare_parser.add_argument("--client", required=True)
    compare_parser.add_argument("--approved-sha256", required=True)
    compare_parser.add_argument("--output", required=True, type=Path)
    compare_parser.add_argument("--sign-key", type=Path)
    compare_parser.add_argument("--expect-deployment", type=Path)
    verify_parser = sub.add_parser("verify-comparison", help="verify a signed comparison and both attachments")
    verify_parser.add_argument("directory", type=Path)
    for parser in (compare_parser, verify_parser):
        parser.add_argument("--key", type=Path)
        parser.add_argument("--identity")
        parser.add_argument("--issuer")
        parser.add_argument("--trusted-root", type=Path)


def dispatch(args):
    trust = {"key": args.key, "identity": args.identity, "issuer": args.issuer, "trusted_root": args.trusted_root}
    if args.operation == "verify-comparison":
        answer = verify(args.directory, **trust)
    else:
        expected = None
        if args.expect_deployment is not None:
            from setrya.core.deployment import read_pin

            expected = read_pin(args.expect_deployment)
        answer = compare(args.previous, args.candidate, client=args.client, approved_sha256=args.approved_sha256,
                         output=args.output, key=args.sign_key, trust=trust, expected_deployment=expected)
    print(answer, end="")
    return 0
