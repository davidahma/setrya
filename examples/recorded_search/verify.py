"""Run the same pytest test against two checked-out fixture revisions."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

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


def run_test(root, subject, name, revision, report):
    checkout = root / name
    git(root, "clone", "--quiet", "--local", str(subject), str(checkout))
    git(checkout, "checkout", "--quiet", "--detach", revision)
    env = os.environ.copy()
    env.update(SETRYA_REVIEW_DRILL=str(DRILL), SETRYA_REVIEW_REPORT=str(report),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", str(TEST)], cwd=checkout, env=env,
                          timeout=20).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True)
    before, after = output / "base.json", output / "candidate.json"
    with TemporaryDirectory(prefix="setrya-review-") as temporary:
        root = Path(temporary)
        subject, base, candidate = make_history(root)
        base_exit = run_test(root, subject, "base", base, before)
        candidate_exit = run_test(root, subject, "candidate", candidate, after)
    completed = subprocess.run(
        [sys.executable, "-m", "setrya", "review", str(before), str(after),
         "--base-commit", base, "--candidate-commit", candidate, "--base-exit", str(base_exit),
         "--candidate-exit", str(candidate_exit), "--summary", str(output / "review.md")], timeout=10,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
