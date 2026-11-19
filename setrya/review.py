"""Turn two recorded tests and their process exits into a CI decision."""

from dataclasses import dataclass
from pathlib import Path

from .checker import Status, evaluate
from .comparison import compare
from .evidence import Report, git_revision
from .drill import InputError


def review(before: Report, after: Report, *, base_commit: str, candidate_commit: str,
           base_exit: int, candidate_exit: int) -> tuple[int, str]:
    base = git_revision(base_commit)
    candidate = git_revision(candidate_commit)
    if base == candidate:
        raise InputError("review requires two different Git commits")
    header = f"Base commit {base} · test exit {base_exit}\nCandidate commit {candidate} · test exit {candidate_exit}"
    for lane, report, revision, test_exit in (
        ("base", before, base, base_exit), ("candidate", after, candidate, candidate_exit)
    ):
        verdict = evaluate(report)
        if (report.inputs or {}).get("revision") != revision:
            return 3, f"INCOMPARABLE  The {lane} report does not identify its expected commit.\n{header}"
        if (test_exit not in {0, 1} or verdict.status is Status.ERROR
                or (verdict.status is Status.PASS and test_exit != 0)
                or (verdict.status is Status.FAIL and test_exit != 1)):
            return 2, (f"ERROR  The {lane} test exit and evidence do not establish a completed result.\n"
                       f"{header}\n{lane.capitalize()} evidence {verdict.status.label} · {verdict.detail}")
    result, comparison = compare(before, after, allow_passing_pair=True)
    first, _, detail = comparison.partition("\n")
    return result, "\n".join(line for line in (first, header, detail) if line)


@dataclass(frozen=True)
class ReviewInput:
    """One CI lane, including the evidence available when setup or pytest failed."""

    report: Report | None
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
            return cls(Report.load(path), revision, exit_code, None)
        except ValueError:
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
        context += str(self.test_exit) if self.test_exit is not None else "(invalid)"
        if self.report is None:
            return f"{context}\n{name} report {self.report_error}."
        verdict = evaluate(self.report)
        revision = (self.report.inputs or {}).get("revision", "(unrecorded)")
        return (f"{context}\n{name} recorded {verdict.status.label} · {verdict.detail}\n"
                f"  Run {self.report.run_id} · recorded commit {revision}")


def review_inputs(base: ReviewInput, candidate: ReviewInput) -> tuple[int, str]:
    """Produce a reviewer-facing outcome even if one lane has no valid report."""
    if base.report is None or candidate.report is None:
        problem = "Both lanes need a readable, valid report."
    elif base.commit is None or candidate.commit is None:
        problem = "Both lanes need a full lowercase Git commit ID."
    elif base.commit == candidate.commit:
        problem = "Review requires two different Git commits."
    elif base.test_exit is None or candidate.test_exit is None:
        problem = "Both lanes need a numeric test exit."
    else:
        return review(base.report, candidate.report, base_commit=base.commit, candidate_commit=candidate.commit,
                      base_exit=base.test_exit, candidate_exit=candidate.test_exit)
    return 2, f"ERROR  {problem}\n{base.describe('Base')}\n{candidate.describe('Candidate')}"
