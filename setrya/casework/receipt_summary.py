"""Audience-specific summaries of decisions reconstructed from a v3 evidence bundle."""
from collections import Counter
from hashlib import sha256
import html
import json
from pathlib import Path
import re

from setrya.core.receipt_v3 import (
    GROUPED_EVIDENCE_FORMATS, answer_slots, attribution_lines, human_issue, maintenance_lines, post_erasure_findings,
    sample_rows,
)
from setrya.offboarding.inputs import read_regular_bytes, require, unique_fields
from setrya.receipt_format import FORMAT_V3, MAX_BUNDLE_FILE_BYTES
from setrya.receipts import replay, replay_bytes, summary as technical_summary, write_back_lead
from .customer import customer_text
from .profiles import BRANCHES, CRYPTO, VENDOR


def _markdown(text):
    # Keep ordinary punctuation readable. Prevent receipt values from creating
    # active links, mentions, raw HTML or Markdown formatting.
    escaped = re.sub(r"([\\`*_{}\[\]<>|#~!])", r"\\\1", html.escape(text)).replace("@", "&#64;")
    escaped = re.sub(r"(?i)\b(https?|ftp|mailto):(?=\S)", r"\1&#58;", escaped)
    return re.sub(r"(?i)\bwww\.(?=\S)", "www&#46;", escaped)


def _format(title, sections, markdown):
    escaped = _markdown if markdown else str
    lines = [("# " if markdown else "") + escaped(title)]
    for heading, content in sections:
        if not content:
            continue
        if heading:
            lines.extend(("", ("## " if markdown else "") + escaped(heading)))
        bullet_run = False
        for line in content:
            bullet = line.startswith("- ")
            if markdown and not (bullet and bullet_run):
                lines.append("")
            lines.append(("- " if bullet else "") + escaped(line[2:] if bullet else line))
            bullet_run = bullet
    return "\n".join(lines)


def _count(number, noun, plural=None):
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def _stage_outcome(observed, key, label):
    stage = observed[key]
    if stage is None:
        return [f"{label}, no observations were recorded."]
    case = observed["case"]
    answers = [(role, row) for (_, _, role, _), (_, row) in
               zip(answer_slots(case), sample_rows(stage, case, grouped=observed["format"] in GROUPED_EVIDENCE_FORMATS),
                   strict=True)]
    stored = stage["stored"] + stage["discovered"]
    if key == "final":
        stored += observed.get("late_residuals", [])
    checks = [(stored, "stored record"), ([row for _, row in answers], "answer")]
    lines = []
    for planned, noun in checks:
        present = [row for row in planned if row is not None]
        target = Counter(row["target_present"] for row in present)
        lines.append(f"{label}, read {len(present)} of {_count(len(planned), noun)}. "
                     f"The erased fact appeared in {target[True]} of those reads."
                     + (f" Its presence was uncertain in {target[None]}." if target[None] else ""))
    retained_stores = [row for rule, row in zip(case["artifacts"], stage["stored"], strict=True)
                       if rule["action"] in ("rewrite", "preserve")]
    retained_answers = [row for role, row in answers if role == "control"]
    for planned, noun in ((retained_stores, "preservation check"), (retained_answers, "control answer")):
        if not planned:
            continue
        observed_controls = sum(row is not None and row["control_present"] is True for row in planned)
        lines.append(f"The retained fact was confirmed in {observed_controls} of "
                     f"{_count(len(planned), noun)}.")
    scans = [hits for hits in stage["scans"].values() if hits is not None]
    if scans:
        locations = sum(len(hits) for hits in scans)
        lines.append(f"{label}, scanners found the erased fact in {_count(locations, 'location')}.")
    return lines


def _linked(folder, filename, digest):
    raw = read_regular_bytes(folder / filename, "linked receipt", MAX_BUNDLE_FILE_BYTES)
    require(sha256(raw).hexdigest() == digest, "Linked receipt changed after replay.")
    return replay_bytes(folder, raw)


def _read_drill_evidence(folder, receipt):
    evidence = receipt["evidence"]
    raw = read_regular_bytes(folder / evidence["file"], "summary evidence", MAX_BUNDLE_FILE_BYTES)
    require(sha256(raw).hexdigest() == evidence["sha256"], "Summary evidence changed after replay.")
    return json.loads(raw.decode(), object_pairs_hook=unique_fields)


def _outcome(bundle, receipt, *, audience):
    folder = Path(bundle)
    if folder.is_file():
        folder = folder.parent
    profile = receipt.get("profile")
    if profile == BRANCHES:
        from ..branches.receipt import observations
        residuals = sum(branch["status"] == "RESIDUAL" for branch in receipt["branches"])
        customer = [f"The observer found {_count(residuals, 'live branch', 'live branches')} from before erasure."]
        return ({"residual"} if residuals else set()), customer if audience == "customer" else observations(receipt)
    if profile == CRYPTO:
        counts = Counter(copy["status"] for copy in receipt["copies"])
        return ({"residual"} if counts["recoverable"] else set()), [
            f"Ciphertext copies: {counts['crypto-erased']} crypto-erased, {counts['absent']} absent, "
            f"{counts['recoverable']} recoverable, {counts['unknown']} unknown.",
        ]
    if profile == VENDOR:
        counts = Counter(check["status"] for check in receipt["checks"])
        return set(), [f"Vendor checks: {counts['PASS']} passed, {counts['FAIL']} failed, {counts['NOT TESTED']} not tested."]
    if profile == "observed-deletion":
        counts = Counter(c["status"] for c in receipt["checks"])
        return ({"residual"} if counts["PRESENT"] else set()), [
            f"Recorded copies: {counts['PRESENT']} present, {counts['ABSENT']} absent, {counts['UNKNOWN']} unknown.",
        ]
    if profile == "follow-up":
        verified = sum(item["status"] == "verified" for item in receipt["items"])
        kinds = {"residual"} if any(item["reason"] == "residual-found" for item in receipt["items"]) else set()
        if receipt["original_verdict"] == "FAIL":
            from .recheck import PARENT
            from setrya.evidence import bundle_manifest

            parent = folder / PARENT
            original = _linked(parent, bundle_manifest(parent), receipt["parent_sha256"])
            prior, _ = _outcome(parent, original, audience=audience)
            kinds.update(prior)
        return kinds, [f"Follow-up observations verified {verified} of {len(receipt['items'])} recorded items."]
    if profile == "discovery":
        counts = Counter(row["status"] for row in receipt["services"])
        kinds = set()
        lines = ["Recorded results: " + ", ".join(f"{n} {label}" for label, n in sorted(counts.items())) + "."]
        if receipt.get("erasure_receipt"):
            from setrya.discovery.case import DRILL
            from setrya.receipt_format import RECEIPT

            linked = _linked(folder / DRILL, RECEIPT, receipt["erasure_receipt"]["sha256"])
            kinds, _ = _outcome(folder / DRILL, linked, audience=audience)
            if audience != "customer":
                lines.extend("- " + line for line in attribution_lines(linked, prefix=f"{DRILL}/"))
        if audience != "customer":
            for row in receipt["services"]:
                if row["role"] == "provider" and "invocation" in row:
                    invocation = row["invocation"]["status"].lower().replace("_", " ")
                    retention = ("A configuration exhibit is recorded." if "exhibit" in row
                                 else "Provider retention evidence is unavailable.")
                    lines.append(f"- {row['id']} · Invocation {invocation}. {retention}")
        return kinds, lines
    if receipt["scope"] == "tenant":
        counts = Counter(row["verdict"] for row in receipt["probes"])
        reasons = {reason for row in receipt["probes"] for reason in row["reasons"]}
        kinds = {"residual"} if "after_subject_present" in reasons else set()
        if "after_control_absent" in reasons:
            kinds.add("control-loss")
        return kinds, ["Recorded results: " + ", ".join(f"{n} {label}" for label, n in sorted(counts.items())) + "."]
    observed = _read_drill_evidence(folder, receipt)
    findings, _ = post_erasure_findings(observed, grouped=observed["format"] in GROUPED_EVIDENCE_FORMATS)
    return findings.kinds, [line for key, label in (("immediate", "After erasure"), ("final", "At the final read"))
                            for line in _stage_outcome(observed, key, label)]


def _scan_coverage(receipt):
    lines = []
    for store, row in sorted(receipt.get("scan_coverage", {}).items()):
        scope = row["scope"]
        if row["kind"] == "git":
            lines.append(f"- {store} · {scope} · Local Git blobs, including unreachable objects.")
            if row.get("locations_incomplete"):
                lines.append(f"- {store} · Git path lookup was incomplete. Recorded blob matches still apply.")
        else:
            lines.append(f"- {store} · {scope} · Include patterns: {', '.join(row['patterns'])}.")
            if row["skipped"]:
                excluded = ", ".join(f"{count} {suffix}" for suffix, count in sorted(row["skipped"].items()))
                lines.append(f"- {store} · Files outside those patterns: {excluded}.")
    return lines


def receipt_summary(bundle, *, audience="reviewer", output_format="text", case_id=None):
    require(audience in ("engineer", "reviewer", "customer") and output_format in ("text", "md", "markdown"),
            "Choose an audience and output format.")
    require(not case_id or audience == "customer", "A display case number applies only to the customer copy.")
    receipt = replay(bundle)
    require(receipt.get("format") == FORMAT_V3, "Summaries require a replayable v3 evidence bundle.")
    failure_kinds, outcomes = _outcome(bundle, receipt, audience=audience)
    controls = [f"- {row['framework']} / {row['reference']}" for row in receipt.get("controls", ())]
    if audience == "customer":
        title, *body = customer_text(receipt, case_id=case_id, failure_kinds=failure_kinds).splitlines()
        sections = [(None, body), ("Recorded observations", outcomes)]
    else:
        title = f"Deletion review for {receipt.get('request_id') or receipt.get('case_id') or receipt.get('run') or receipt.get('target') or receipt.get('capture_manifest_sha256') or receipt.get('parent_sha256') or 'recorded copies'}"
        sections = [("Decision", [f"Technical verdict {receipt['technical_verdict']}",
                                  f"Review status {receipt['review_status']}"])]
        evidence = receipt.get("evidence", {})
        anchors = [f"{evidence['file']}  SHA-256 {evidence['sha256']}"] if "file" in evidence else []
        if audience == "engineer":
            sections.append(("Observations", technical_summary(receipt).splitlines()))
            anchors.extend(f"- {item['store']}/{item['key']}  {', '.join(item['evidence'])}"
                           for item in receipt.get("artifact_checks", ()))
        else:
            profile = receipt.get("profile") or receipt["scope"]
            shared = any(row["action"] == "rewrite" for row in receipt.get("artifact_checks", ()))
            scope = {"person": "Shared-record deletion drill" if shared else "Person deletion drill",
                     "tenant": "Tenant offboarding", "discovery": "Discovery case",
                     "observed-deletion": "Read-only verification", "follow-up": "Open-item follow-up",
                     BRANCHES: "Branch and restore lineage", CRYPTO: "Read-only key-destruction evidence",
                     VENDOR: "Provider API readback" if "readback" in receipt else "Vendor conformance fixture"}.get(profile, profile)
            sections.append(("Scope", [scope, *(["Write-back check " + receipt["race"]] if receipt.get("race") else [])]))
            issues = [*receipt.get("failures", ()), *receipt.get("gaps", ())]
            issues.extend(f"{e['operation']} ({e['error']})" for e in receipt.get("errors", ()))
            sections.append(("Findings", ["- " + human_issue(issue) for issue in issues]))
            sections.append(("Write attribution", ["- " + row for row in attribution_lines(receipt)]))
        sections.append(("Recorded observations", outcomes))
        sections.append(("Scan coverage", _scan_coverage(receipt)))
        sections.append(("Evidence", anchors))
        items = []
        for item in receipt["open_items"]:
            label = item.get("store") or item.get("service") or item.get("id") or "Follow-up"
            kind = item.get("kind") or item.get("status") or "review"
            owner = item.get("owner") or "unassigned"
            date = item.get("follow_up") or "unscheduled"
            if item.get("evidence") == "remote-copies-uninspected":
                items.append(f"- {label} · Remote copies were not inspected. Owner {owner}. Follow-up {date}.")
            else:
                items.append(f"- {label} ({kind})  owner {owner}  follow-up {date}")
        sections.append(("Open items", items or ["No open items recorded."]))
    if receipt.get("maintenance_checks") and audience == "reviewer":
        sections.append(("Maintenance checks", list(maintenance_lines(receipt))))
    if receipt.get("profile") == VENDOR and audience != "engineer":
        from ..vendor.report import execution_lines

        sections.append(("Execution and review", execution_lines(receipt)))
    if controls:
        sections.append(("Customer control references", ["The case owner supplied these references.", *controls]))
    markdown = output_format in ("md", "markdown")
    formatted = _format(title, sections, markdown)
    if lead := write_back_lead(receipt, bundle):
        return lead + "\n\n" + formatted
    return formatted
