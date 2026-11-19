"""Compare the same scheduled deliveries across two reviewed revisions."""

from dataclasses import dataclass

from .checker import Status, Verdict, evaluate
from .comparison import compare
from .scenarios import SCHEDULES, ScenarioEvidence


def replay_scenario_command(lane: str, named_directories: frozenset[str], delivery: str = "") -> str:
    suffix = f"/{delivery}" if delivery else ""
    target = lane + suffix if lane in named_directories else f'"${lane.upper()}_REPORT{suffix}"'
    return f"setrya replay {target}"


@dataclass(frozen=True)
class _Pair:
    path: str
    before: Verdict
    after: Verdict
    code: int
    comparison: str

    def describe(self, named_directories: frozenset[str]) -> str:
        _, _, observations = self.comparison.partition("\n")
        return "\n".join(line for line in (
            f"Delivery {self.path}", observations,
            f"Replay base delivery  {replay_scenario_command('base', named_directories, self.path)}",
            f"Replay candidate delivery  {replay_scenario_command('candidate', named_directories, self.path)}",
        ) if line)


def compare_scenarios(base: ScenarioEvidence, candidate: ScenarioEvidence, base_commit: str,
                      candidate_commit: str, base_exit: int, candidate_exit: int, *,
                      named_directories: frozenset[str] = frozenset()) -> tuple[int, str]:
    for name, lane, revision, exit_code in (("base", base, base_commit, base_exit),
                                           ("candidate", candidate, candidate_commit, candidate_exit)):
        for schedule, member in lane.members.items():
            for index, report in member.reports.items():
                if (report.inputs or {}).get("revision") != revision:
                    return 3, f"INCOMPARABLE  The {name} {schedule} delivery-{index} does not identify its expected commit."
        if exit_code != lane.verdict.status.exit_code:
            return 2, f"ERROR  The {name} test exit disagrees with the replayed scenario verdict."

    if (base.kind, base.schedule, base.members.keys()) != (candidate.kind, candidate.schedule, candidate.members.keys()):
        return 3, "INCOMPARABLE  The scenario kind or delivery schedule differs."

    pairs = []
    gaps = []
    shared = []
    coverage = []
    for schedule, old in base.members.items():
        new = candidate.members[schedule]
        expected = set(range(len(SCHEDULES[schedule][0])))
        coverage.append(f"Schedule {schedule} · base {old.verdict.status.label} · candidate {new.verdict.status.label}")
        for name, member in (("base", old), ("candidate", new)):
            missing = expected - member.reports.keys()
            if missing:
                files = ", ".join(f"delivery-{i}.json" for i in sorted(missing))
                code = 3 if member.verdict.status is Status.NOT_TESTED else 2
                gaps.append((code, f"{name} {schedule} is missing valid evidence for {files}."))
            elif not member.complete:
                code = 2 if member.verdict.status is Status.ERROR else 3
                gaps.append((code, f"{name} {schedule} has incomplete schedule evidence. {member.verdict.detail}"))
            if member.shared_failure:
                shared.append((name, f"{name} {schedule} · {member.shared_failure.detail}"))
        if not old.reports or not new.reports:
            continue
        if old.comparison_id is None or new.comparison_id is None:
            return 3, f"INCOMPARABLE  {schedule} lacks a cross-revision scenario identity. Rerun both revisions."
        if old.comparison_id != new.comparison_id:
            return 3, f"INCOMPARABLE  The {schedule} adapter, options, or drill differs."
        for i in sorted(old.reports.keys() & new.reports.keys()):
            before, after = old.reports[i], new.reports[i]
            code, comparison = compare(before, after, allow_passing_pair=True)
            path = (f"{schedule}/" if base.schedule == "all" else "") + f"delivery-{i}.json"
            pairs.append(_Pair(path, evaluate(before), evaluate(after), code, comparison))

    # FAIL can outlive a damaged sibling. Keep it visible without claiming the
    # base passed the same complete scenario or all failing phases were fixed.
    selected = None
    if candidate.verdict.status is Status.FAIL:
        selected = next((pair for pair in pairs if pair.code == 1), None)
        if selected and not gaps and not shared and all(pair.code in (0, 1) for pair in pairs):
            code, headline = 1, selected.comparison.splitlines()[0]
        else:
            code, headline = 1, "FAILURE OBSERVED  Candidate scenario evidence records a failure."
    elif gaps:
        code, detail = next((gap for gap in gaps if gap[0] == 2), gaps[0])
        headline = ("ERROR" if code == 2 else "INCONCLUSIVE") + "  " + detail
    elif shared or (base.verdict.status is Status.FAIL and not any(pair.before.status is Status.FAIL for pair in pairs)):
        code, headline = 3, "INCONCLUSIVE  The base failed across workers. A delivery comparison cannot verify that fix."
    else:
        selected = next((pair for pair in pairs if pair.code == 2), None)
        selected = selected or next((pair for pair in pairs if pair.code != 0), None)
        selected = selected or next((pair for pair in pairs if pair.before.status is Status.FAIL), None)
        selected = selected or pairs[0]
        code, headline = selected.code, selected.comparison.splitlines()[0]

    tier = None
    for pair in pairs:
        for verdict in (pair.before, pair.after):
            if verdict.evidence_tier == "observed-after-refresh":
                tier = "observed-after-refresh"
            elif verdict.evidence_tier and tier is None:
                tier = verdict.evidence_tier
    if code == 0 and tier == "observed-after-refresh":
        headline = "PASS  The matching observations passed after refresh. Write visibility is unverified."

    lines = [headline, *coverage, *(detail for _, detail in gaps if detail not in headline),
             *(detail for _, detail in shared)]
    if tier:
        lines.append(f"Weakest passing evidence tier across paired deliveries · {tier}")
    if selected:
        lines.append(selected.describe(named_directories))
    return code, "\n".join(lines)
