"""Turn two recorded tests and their process exits into a CI decision."""

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
