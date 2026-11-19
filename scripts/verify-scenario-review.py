"""Exercise the installed CLI against two Git revisions of a disposable SQLite worker."""

import argparse
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory

from setrya import scenario_fixture
from setrya.examples.recorded_search.verify import git


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--worker-order", action="store_true", help="verify the transient two-worker regression")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True)
    fixed = Path(scenario_fixture.__file__).read_text().replace("from .adapter import", "from setrya.adapter import")
    guard = ('            if not self.app.broken and epoch != db.execute("SELECT epoch FROM state").fetchone()[0]:\n'
             '                return "rejected"\n')
    kind, schedule = "revoke", "all"
    if args.worker_order:
        fixed = (Path(__file__).parent / "worker_order/application.py").read_text()
        guard = "GUARD_CAPTURED_WRITE = True"
        broken = fixed.replace(guard, "GUARD_CAPTURED_WRITE = False")
        kind, schedule = "update", "forward"
    else:
        broken = fixed.replace(guard, "")
    if fixed.count(guard) != 1:
        raise RuntimeError("cannot locate the worker epoch check in the installed SQLite application")

    review = ["setrya", "review", "base", "candidate"]
    exits = {}
    with TemporaryDirectory(prefix="setrya-scenario-review-") as temporary:
        repo = Path(temporary)
        git(repo, "init", "--quiet")
        git(repo, "config", "user.name", "Setrya fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        git(repo, "config", "commit.gpgsign", "false")
        for lane, source in (("base", broken), ("candidate", fixed)):
            (repo / "application.py").write_text(source)
            git(repo, "add", "application.py")
            git(repo, "commit", "--quiet", "-m", f"{lane} worker epoch check")
            revision = git(repo, "rev-parse", "HEAD")
            command = ["setrya", "scenario", kind, "--schedule", schedule, "--adapter", "application:Application",
                       "--revision", revision, "--output", str(output / lane)]
            exits[lane] = subprocess.run(command, cwd=repo, timeout=60).returncode
            review.extend([f"--{lane}-commit", revision, f"--{lane}-exit", str(exits[lane])])

    (output / "review-command.txt").write_text(shlex.join(review) + "\n")
    subprocess.run([*review, "--summary", "review.md"], cwd=output, check=True, timeout=30)
    summary = (output / "review.md").read_text()
    if exits != {"base": 1, "candidate": 0} or "FIX VERIFIED" not in summary:
        raise RuntimeError("the release review must confirm the fix against a failing base")
    if args.worker_order:
        from setrya.evidence import Report

        base = Report.load(output / "base" / "delivery-1.json")
        last = [event["passages"] for event in base.events
                if event.get("stage") == "final" and event["probe_id"] == "subject"]
        if last != [["subject@B"]] or "between releases" not in summary:
            raise RuntimeError("expected a repaired final read and a decisive read between releases")
    # These standard artifact names should produce commands a reviewer can run
    # directly after extracting the bundle into a fresh directory.
    commands = [line.partition("  ")[2] for line in summary.splitlines() if line.startswith("Replay ")]
    if len(commands) != 4:
        raise RuntimeError("expected commands for both schedules and the two decisive deliveries")
    for command in commands:
        argv = shlex.split(command)
        lane = Path(argv[-1]).parts[0]
        if argv[:2] != ["setrya", "replay"] or lane not in exits:
            raise RuntimeError("summary replay command needs an unrecorded path")
        if subprocess.run(argv, cwd=output, timeout=30).returncode != exits[lane]:
            raise RuntimeError(f"the saved {lane} verdict differs from its live result")


if __name__ == "__main__":
    main()
