"""Turn two recorded tests and their process exits into a CI decision."""

from dataclasses import dataclass
from pathlib import Path

from .checker import Status, evaluate
from .status import exit_agrees
from .comparison import compare
from .evidence import Report, bundle_manifest, git_revision
from .review_endpoint import EndpointEvidence, review_endpoints
from .drill import InputError
from .scenarios import ScenarioEvidence, inspect_scenario


EXIT_CONTRACTS = ("pytest", "setrya")


def review(before: Report, after: Report, *, base_commit: str, candidate_commit: str,
           base_exit: int, candidate_exit: int, exit_contract: str = "pytest") -> tuple[int, str]:
    base = git_revision(base_commit)
    candidate = git_revision(candidate_commit)
    if exit_contract not in EXIT_CONTRACTS:
        raise InputError("unknown native exit contract")
    if base == candidate:
        raise InputError("review requires two different Git commits")
    header = f"Base commit {base} · test exit {base_exit}\nCandidate commit {candidate} · test exit {candidate_exit}"
    for lane, report, revision, test_exit in (
        ("base", before, base, base_exit), ("candidate", after, candidate, candidate_exit)
    ):
        verdict = evaluate(report)
        if (report.inputs or {}).get("revision") != revision:
            return 3, f"INCOMPARABLE  The {lane} report does not identify its expected commit.\n{header}"
        if not exit_agrees(test_exit, verdict.status, exit_contract) or verdict.status is Status.ERROR:
            return 2, (f"ERROR  The {lane} test exit and evidence do not establish a completed result.\n"
                       f"{header}\n{lane.capitalize()} evidence {verdict.status.label} · {verdict.detail}")
    result, comparison = compare(before, after, allow_passing_pair=True)
    first, _, detail = comparison.partition("\n")
    return result, "\n".join(line for line in (first, header, detail) if line)


@dataclass(frozen=True)
class ReviewInput:
    """One CI lane, including the evidence available when setup or pytest failed."""

    report: Report | ScenarioEvidence | EndpointEvidence | None
    commit: str | None
    test_exit: int | None
    report_error: str | None

    @classmethod
    def load(cls, path: str, commit: str | None, test_exit: str | None) -> "ReviewInput":
        try:
            revision = git_revision(commit)
        except InputError:
            revision = None
        try:
            exit_code = int(test_exit)
        except (TypeError, ValueError):
            exit_code = None
        try:
            if Path(path).is_dir() and bundle_manifest(Path(path)) == "answer-cache.json":
                artifact = EndpointEvidence.load(path)
            else:
                artifact = inspect_scenario(path) if Path(path).is_dir() else Report.load(path)
            return cls(artifact, revision, exit_code, None)
        except (OSError, TypeError, ValueError):
            # Report.load also rejects malformed JSON integers. Error strings and
            # paths can contain private input, so keep the artifact diagnostic fixed.
            problem = "is unreadable or invalid"
            try:
                Path(path).stat()
            except FileNotFoundError:
                problem = "is missing"
            except OSError:
                pass
            return cls(None, revision, exit_code, problem)

    def describe(self, name: str) -> str:
        context = f"{name} commit {self.commit or '(invalid)'} · test exit "
        if self.test_exit is not None:
            context += str(self.test_exit)
        elif self.report is not None and self.report_error:
            context += f"(unavailable, {self.report_error})"
        else:
            context += "(invalid)"
        if self.report is None:
            return f"{context}\n{name} report {self.report_error}."
        if isinstance(self.report, EndpointEvidence):
            return f"{context}\n{self.report.describe()}"
        if isinstance(self.report, ScenarioEvidence):
            verdict = self.report.verdict
            return (f"{context}\n{name} recorded {verdict.status.label} · {self.report.kind} / "
                    f"{self.report.schedule} · {verdict.detail}")
        verdict = evaluate(self.report)
        revision = (self.report.inputs or {}).get("revision", "(unrecorded)")
        return (f"{context}\n{name} recorded {verdict.status.label} · {verdict.detail}\n"
                f"  Run {self.report.run_id} · recorded commit {revision}")


def review_inputs(base: ReviewInput, candidate: ReviewInput, *,
                  named_directories: frozenset[str] = frozenset(), exit_contract: str | None = None) -> tuple[int, str]:
    """Produce a reviewer-facing outcome even if one lane has no valid report."""
    if base.report is None or candidate.report is None:
        problem = "Both lanes need a readable, valid report."
    elif base.commit is None or candidate.commit is None:
        problem = "Both lanes need a full lowercase Git commit ID."
    elif base.commit == candidate.commit:
        problem = "Review requires two different Git commits."
    elif base.test_exit is None or candidate.test_exit is None:
        problem = "Both lanes need a numeric test exit."
    elif isinstance(base.report, EndpointEvidence) and isinstance(candidate.report, EndpointEvidence):
        code, decision, weakest = review_endpoints(base, candidate, exit_contract or "pytest")
        return code, "\n".join((decision, f"Weakest evidence · {weakest}", base.describe("Base"), candidate.describe("Candidate")))
    elif isinstance(base.report, Report) and isinstance(candidate.report, Report):
        return review(base.report, candidate.report, base_commit=base.commit, candidate_commit=candidate.commit,
                      base_exit=base.test_exit, candidate_exit=candidate.test_exit, exit_contract=exit_contract or "pytest")
    elif isinstance(base.report, ScenarioEvidence) and isinstance(candidate.report, ScenarioEvidence):
        if exit_contract not in (None, "setrya"):
            return 2, "ERROR  Scenario directories require the Setrya exit contract."
        from .review_scenarios import compare_scenarios

        code, text = compare_scenarios(base.report, candidate.report, base.commit, candidate.commit,
                                       base.test_exit, candidate.test_exit, named_directories=named_directories)
        return code, f"{text}\n{base.describe('Base')}\n{candidate.describe('Candidate')}"
    else:
        return 3, (f"INCOMPARABLE  Review needs two report files, two endpoint bundles, or two scenario directories.\n"
                   f"{base.describe('Base')}\n{candidate.describe('Candidate')}")
    return 2, f"ERROR  {problem}\n{base.describe('Base')}\n{candidate.describe('Candidate')}"
