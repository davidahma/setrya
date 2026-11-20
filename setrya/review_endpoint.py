"""Review the recorded HTTP responses and worker outcome for two application revisions."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .checker import NS_PER_MS, Status, Verdict, _write_resolved
from .comparison import _difference, compare_with_tier
from .drill import InputError
from .endpoint_evidence import APPLICATION_VERSIONS, JOURNAL, MANIFEST, _read_bundle, contract_for, matches_question
from .evidence import Report, matches_search
from .offboarding.inputs import save
from .status import exit_agrees


PARTIAL = "response observed (write attribution or two-user control incomplete)"


@dataclass(frozen=True)
class EndpointEvidence:
    verdict: Verdict
    report: Report
    manifest: dict

    @classmethod
    def load(cls, path):
        verdict, _, report, manifest = _read_bundle(path)
        if manifest["version"] not in APPLICATION_VERSIONS:
            raise InputError("select an application endpoint bundle")
        return cls(verdict, report, manifest)

    def save(self, path: Path):
        path.mkdir(exist_ok=True)
        self.report.save(path / JOURNAL)
        save({**self.manifest, "report_sha256": sha256((path / JOURNAL).read_bytes()).hexdigest()}, path / MANIFEST)

    def describe(self):
        case = contract_for(self.manifest)
        observations = self.manifest["observations"]
        probes = [e for e in self.report.events if e["kind"] == "probe"]
        decisive = self.report.events[self.verdict.event - 1] if self.verdict.event else {}
        phase = decisive.get("stage") or (observations[-1]["stage"] if observations else "baseline")
        lines = [self.verdict.status.label + " · " + self.verdict.detail]
        for role, who in (("revoked", case.revoked), ("retained", case.retained)):
            samples = [(e, r) for e, r in zip(probes, observations, strict=True)
                       if r["stage"] == phase and r["probe"] == role + "-answer"]
            if not samples:
                lines.append(f"  {role} {who} · {phase} · no response recorded")
                continue
            event, row = next((p for p in samples if p[0]["seq"] == decisive.get("seq")), samples[-1])
            sources = ", ".join(s or "unknown" for s in row["sources"]) or "(none)"
            state = {True: "hit", False: "miss"}.get(row["cache_hit"], "unknown")
            identity = f"served as {row['caller'] or 'unknown'} on {row['route'] or 'unknown'}"
            question = "" if matches_question(row, case) else " · question unverified"
            lines.append(f"  {role} {who} · {phase} event {event['seq']} · {identity}{question} · "
                         f"HTTP {row['status'] if row['status'] is not None else 'unknown'} · {row['body']} · sources {sources} · cache {state}")
        return "\n".join(lines)

    def tier(self):
        if self.verdict.status is not Status.FAIL:
            return self.verdict.evidence_tier or "incomplete"
        if self.verdict.evidence_tier != "write-confirmed":
            return PARTIAL
        # The revoked user's read is already attributed by endpoint replay.
        # Require a timely, visible final control for the retained user too.
        events = {e["kind"]: e for e in self.report.events if e["kind"] != "probe"}
        held = {k: events["held"][k] for k in ("run_id", "attempt", "document", "revision", "name")}
        if self.manifest["held_after_reads"] != held:
            return PARTIAL
        control = next(p for p in self.report.rules.probes if p.id == "retained-answer")
        final = next((e for e in reversed(self.report.events) if e.get("stage") == "final"
                      and e["probe_id"] == control.id), None)
        deadline = events["released"]["ns"] + self.report.rules.settle_ms * NS_PER_MS
        receipt = events["receipt"]
        if (final and receipt["ns"] <= final["started_ns"] <= final["ns"] <= deadline
                and matches_search(final, control) and _write_resolved(final, receipt, control, {})
                and None not in final["passages"] and set(control.require) <= set(final["passages"])):
            return "write-confirmed"
        return PARTIAL


def review_endpoints(base, candidate, exit_contract):
    old, new = base.report, candidate.report
    for name, lane in (("base", base), ("candidate", candidate)):
        evidence = lane.report
        if (evidence.report.inputs or {}).get("revision") != lane.commit:
            return 3, f"NOT TESTED  The {name} endpoint evidence does not identify its expected commit.", "incomplete"
        if not exit_agrees(lane.test_exit, evidence.verdict.status, exit_contract):
            return 2, f"ERROR  The {name} test exit and endpoint evidence disagree.", "incomplete"
    changed = [key for key, value in old.manifest["case"].items() if new.manifest["case"][key] != value]
    mismatch = "Changed case fields: " + ", ".join(changed) + "." if changed else _difference(old.report, new.report)
    if mismatch:
        return 3, "NOT TESTED  The two endpoint cases are incomparable. " + mismatch, "incomplete"
    a, b = old.verdict.status, new.verdict.status
    tiers = (old.tier(), new.tier())
    weakest = next((t for t in ("incomplete", PARTIAL, "observed-after-refresh") if t in tiers), "write-confirmed")
    if Status.ERROR in (a, b):
        return 2, "ERROR  An endpoint test did not complete.", weakest
    if b is Status.FAIL:
        return 1, "FAIL  " + new.verdict.detail, weakest
    if Status.NOT_TESTED in (a, b):
        incomplete = old if a is Status.NOT_TESTED else new
        return 3, "NOT TESTED  " + incomplete.verdict.detail, weakest
    code, _, pair_tier = compare_with_tier(old.report, new.report, allow_passing_pair=True)
    if code:
        return 3, "NOT TESTED  A matching observation at the affected phase is missing.", weakest
    if a is Status.FAIL and pair_tier == weakest == "write-confirmed":
        return 0, "FIX VERIFIED  The resumed worker exposed the private answer on base. Both candidate callers passed.", weakest
    if a is Status.FAIL:
        return 0, "PASS  Both candidate callers passed. The base evidence could not verify a late-write fix.", weakest
    return 0, "PASS  Both endpoint revisions passed for the revoked user and the retained colleague.", weakest
