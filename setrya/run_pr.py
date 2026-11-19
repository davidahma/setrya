"""Run a trusted local test command against two application commits and review its evidence."""

import argparse
from hashlib import sha256
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory

from .cli import _write_summary
from .drill import Drill, InputError
from .evidence import Report, bind_inputs
from .git_refs import git_bytes, resolve_commit
from .offboarding.inputs import read_regular_bytes, save
from .review import EXIT_CONTRACTS, ReviewInput, review_inputs


def _environment():
    # A hook or parent worktree can export repository-selection variables. Git
    # and the test's own rev-parse must inspect the checkout supplied as cwd.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _checkout(repo, target, commit):
    # Shared clones borrow local objects; refs, index and working files belong
    # to the disposable clone. No worktree registrations or source branch changes.
    for cwd, args in ((target.parent, ["clone", "--local", "--shared", "--no-checkout", "--", str(repo), str(target)]),
                      (target, ["checkout", "--quiet", "--detach", commit])):
        subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", *args], cwd=cwd, env=_environment(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120)
    if resolve_commit(target, "HEAD") != commit:
        raise InputError("could not check out the expected commit")


def _test(command, checkout, env, timeout):
    with subprocess.Popen(command, cwd=checkout, env=env, start_new_session=(os.name == "posix")) as child:
        try:
            return child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "posix":
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                child.kill()
            raise


def _lane(repo, root, name, commit, drill_bytes, expected, command, timeout):
    lane = root / name
    lane.mkdir()
    checkout, source, evidence = lane / "app", lane / "drill.toml", lane / "report.json"
    source.write_bytes(drill_bytes)
    env = _environment()
    env.update(SETRYA_RUN_PR="1", SETRYA_REVIEW_REPORT=str(evidence), SETRYA_REVIEW_DRILL=str(source),
               SETRYA_REVIEW_REVISION=commit)
    test_exit = None
    try:
        _checkout(repo, checkout, commit)
        print(f"Running {name} {commit}", flush=True)
        test_exit = _test(command, checkout, env, timeout)
        if (resolve_commit(checkout, "HEAD") != commit
                or git_bytes(checkout, "status", "--porcelain", "--untracked-files=no")):
            raise InputError("the test changed its checked-out commit or tracked files")
        if evidence.with_suffix(".ambiguous").exists():
            raise InputError("select exactly one Setrya recording per lane")
        if not evidence.exists():
            raise InputError("the test did not produce its selected Setrya report")
        if evidence.is_symlink():
            raise InputError("the result must be a local report file")
        report = Report.load(evidence)
        if (report.rules != expected.rules or not report.inputs
                or report.inputs.get("questions") != bind_inputs(expected, test_id="run-pr")["questions"]):
            raise InputError("the recorded rules or questions differ from the selected drill")
        # Search v1 omits adapter options. The live SDK checks the complete
        # Drill, including the factory and options, before opening its journal.
        executed = evidence.with_suffix(".executed")
        identity = f"{report.run_id} {report.inputs['adapter']}".encode("ascii")
        if (read_regular_bytes(source, "drill") != drill_bytes or not executed.is_file() or executed.is_symlink()
                or read_regular_bytes(executed, "execution identity", 256) != identity):
            raise InputError("the report does not match a live SDK run of the selected drill")
        return ReviewInput(report, commit, test_exit, None)
    except subprocess.TimeoutExpired:
        problem = "checkout or command timed out"
    except (OSError, subprocess.CalledProcessError):
        problem = "checkout or command could not run"
    except ValueError as exc:
        problem = str(exc)
    return ReviewInput(None, commit, test_exit, problem)


def _handoff(output, inputs, drill_digest, exit_contract, *, publish=False):
    base, candidate = inputs.values()
    code, summary = review_inputs(base, candidate, exit_contract=exit_contract)
    lanes = {}
    for name, lane in inputs.items():
        digest = None
        if lane.report is not None:
            path = output / f"{name}.json"
            lane.report.save(path)
            digest = sha256(path.read_bytes()).hexdigest()
            summary += f"\nReplay {name}  setrya replay {name}.json"
        lanes[name] = {"commit": lane.commit, "test_exit": lane.test_exit, "report_sha256": digest}
    if base.report is not None and candidate.report is not None:
        summary += (f"\nReview again  setrya review base.json candidate.json --base-commit {base.commit}"
                    f" --candidate-commit {candidate.commit} --base-exit {base.test_exit}"
                    f" --candidate-exit {candidate.test_exit} --exit-contract {exit_contract}")
    save({"format": "setrya.run-pr", "version": 1, "drill_sha256": drill_digest, "exit_contract": exit_contract, **lanes},
         output / "run-pr.json")
    _write_summary(output / "review.md", "release review", summary)
    if publish:
        print(summary)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setrya run-pr", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="local application repository")
    parser.add_argument("--base", required=True, help="local base Git ref or commit")
    parser.add_argument("--candidate", required=True, help="local candidate Git ref or commit")
    parser.add_argument("--drill", required=True, type=Path, help="shared local drill TOML")
    parser.add_argument("--output", required=True, type=Path, help="new directory for the reviewer handoff")
    parser.add_argument("--exit-contract", choices=EXIT_CONTRACTS, default="pytest",
                        help="interpret native exits as pytest (default) or Setrya verdicts")
    parser.add_argument("--timeout", type=int, default=900, help="maximum seconds per test command (default 900)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARG ...]")
    args = parser.parse_args(argv)
    if args.command[:1] != ["--"] or len(args.command) < 2 or args.timeout < 1:
        parser.error("supply a positive timeout and a test command after --")
    try:
        repo = args.repo.resolve()
        commits = {name: resolve_commit(repo, getattr(args, name)) for name in ("base", "candidate")}
        # Resolve HEAD in the selected worktree, then clone its common object
        # store. This also supports invocation from an application subdirectory.
        common_dir = git_bytes(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        repo = Path(os.fsdecode(common_dir).removesuffix("\n"))
        if commits["base"] == commits["candidate"]:
            raise InputError("select two different application commits")
        drill_bytes = read_regular_bytes(args.drill, "drill")
        with TemporaryDirectory(prefix="setrya-run-pr-") as scratch:
            root = Path(scratch).resolve()
            template = root / "drill.toml"
            template.write_bytes(drill_bytes)
            expected = Drill.load(template)
            # Reserve the artifact directory before executing either test. An
            # interrupted or skipped test can never pick up an older PASS.
            output = args.output.absolute()
            output.mkdir(parents=True, exist_ok=False)
            inputs = {name: ReviewInput(None, commit, None, "was not run") for name, commit in commits.items()}
            digest = sha256(drill_bytes).hexdigest()
            try:
                for name, commit in commits.items():
                    _handoff(output, inputs, digest, args.exit_contract)
                    inputs[name] = _lane(repo, root, name, commit, drill_bytes, expected,
                                         args.command[1:], args.timeout)
            except KeyboardInterrupt:
                print("Interrupted during the PR run.", file=sys.stderr)
            return _handoff(output, inputs, digest, args.exit_contract, publish=True)
    except FileExistsError:
        print("ERROR  Choose a new output directory.", file=sys.stderr)
    except (InputError, ValueError) as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
    except OSError:
        print("ERROR  Could not read the checkout or write the review handoff.", file=sys.stderr)
    return 2
