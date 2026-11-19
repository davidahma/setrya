"""Run the same pytest test against two checked-out fixture revisions."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from setrya.checker import Status, evaluate
from setrya.evidence import Report

EXAMPLES = Path(__file__).resolve().parent.parent
DRILL = EXAMPLES / "recorded-search" / "drill.toml"
TEST = Path(__file__).with_name("existing_test.py")


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True, timeout=15).strip()


def make_history(root):
    subject = root / "subject"
    subject.mkdir()
    package = subject / "examples"
    package.mkdir()
    shutil.copyfile(EXAMPLES / "__init__.py", package / "__init__.py")
    shutil.copytree(EXAMPLES / "support_search", package / "support_search", ignore=shutil.ignore_patterns("__pycache__"))
    worker = package / "support_search" / "app.py"
    fixed = worker.read_text()
    guard = "if revision != current and not broken:"
    if fixed.count(guard) != 1:
        raise RuntimeError("the replay fixture's worker guard changed")
    git(subject, "init", "--quiet", "--initial-branch=main")
    git(subject, "config", "user.name", "Setrya fixture")
    git(subject, "config", "user.email", "fixture@example.invalid")
    git(subject, "config", "commit.gpgsign", "false")
    worker.write_text(fixed.replace(guard, "if revision != current and scenario == 'retired':"))
    git(subject, "add", "examples")
    git(subject, "commit", "--quiet", "-m", "fixture: allow the late refund overwrite")
    base = git(subject, "rev-parse", "HEAD")
    worker.write_text(fixed)
    git(subject, "add", str(worker))
    git(subject, "commit", "--quiet", "-m", "fixture: reject the old refund write")
    candidate = git(subject, "rev-parse", "HEAD")
    return subject, base, candidate


def run_test(root, subject, name, revision, report, *, mode="fixed"):
    checkout = root / name
    git(root, "clone", "--quiet", "--local", str(subject), str(checkout))
    git(checkout, "checkout", "--quiet", "--detach", revision)
    env = os.environ.copy()
    env.update(SETRYA_REVIEW_DRILL=str(DRILL), SETRYA_REVIEW_REPORT=str(report),
               SETRYA_REVIEW_MODE=mode, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "setrya.pytest_plugin", "-q", "--tb=line", str(TEST)],
        cwd=checkout, env=env, timeout=20,
    ).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True)
    before, after = output / "base.json", output / "candidate.json"
    opaque = output / "opaque.json"
    with TemporaryDirectory(prefix="setrya-review-") as temporary:
        root = Path(temporary)
        subject, base, candidate = make_history(root)
        base_exit = run_test(root, subject, "base", base, before)
        candidate_exit = run_test(root, subject, "candidate", candidate, after)
        opaque_exit = run_test(root, subject, "opaque", candidate, opaque, mode="opaque")
    completed = subprocess.run(
        [sys.executable, "-m", "setrya", "review", str(before), str(after),
         "--base-commit", base, "--candidate-commit", candidate, "--base-exit", str(base_exit),
         "--candidate-exit", str(candidate_exit), "--summary", str(output / "review.md")], timeout=10,
    )
    if completed.returncode:
        return completed.returncode
    if (base_exit, candidate_exit) != (1, 0):
        raise RuntimeError("the regression example requires failing base and passing candidate tests")
    opaque_verdict = evaluate(Report.load(opaque))
    print(f"Opaque search: pytest exit {opaque_exit} · {opaque_verdict.status.label} · {opaque_verdict.detail}")
    if opaque_exit != 1 or opaque_verdict.status is not Status.NOT_TESTED or "visibility" not in opaque_verdict.detail:
        raise RuntimeError("pytest must fail when search cannot confirm visibility of the committed write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
