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
from .endpoint_evidence import JOURNAL, MANIFEST
from .review_endpoint import EndpointEvidence
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
    env = _environment()
    env["SETRYA_REVIEW_REVISION"] = commit
    if expected is None:
        evidence = lane / "endpoint"
        env.update(SETRYA_RUN_PR="endpoint", SETRYA_REVIEW_ENDPOINT=str(evidence))
    else:
        source.write_bytes(drill_bytes)
        env.update(SETRYA_RUN_PR="1", SETRYA_REVIEW_REPORT=str(evidence), SETRYA_REVIEW_DRILL=str(source))
    test_exit = None
    command_problem = None
    try:
        _checkout(repo, checkout, commit)
        print(f"Running {name} {commit}", flush=True)
        try:
            test_exit = _test(command, checkout, env, timeout)
        except subprocess.TimeoutExpired:
            # Teardown can outlive the completed endpoint recording. Validate
            # that recording normally, but leave the missing native exit unset.
            if expected is not None or not evidence.with_suffix(".executed").is_file():
                raise
            command_problem = "command timed out after recording"
        if (resolve_commit(checkout, "HEAD") != commit
                or git_bytes(checkout, "status", "--porcelain", "--untracked-files=no")):
            raise InputError("the test changed its checked-out commit or tracked files")
        if evidence.with_suffix(".ambiguous").exists():
            raise InputError("select exactly one Setrya recording per lane")
        if not evidence.exists():
            raise InputError("the test did not produce its selected Setrya report")
        if evidence.is_symlink():
            raise InputError("the result must be a local report file")
        if expected is None:
            manifest = evidence / MANIFEST
            executed = evidence.with_suffix(".executed")
            if (not evidence.is_dir() or manifest.is_symlink() or (evidence / JOURNAL).is_symlink()
                    or not executed.is_file() or executed.is_symlink()
                    or read_regular_bytes(executed, "execution identity", 64)
                    != sha256(read_regular_bytes(manifest, "endpoint manifest")).hexdigest().encode("ascii")):
                raise InputError("the bundle does not match a live endpoint run")
            return ReviewInput(EndpointEvidence.load(evidence), commit, test_exit, command_problem)
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
    endpoint = drill_digest is None
    base, candidate = inputs.values()
    code, summary = review_inputs(base, candidate, exit_contract=exit_contract)
    metadata = {"format": "setrya.endpoint-pr" if endpoint else "setrya.run-pr", "version": 1,
                "exit_contract": exit_contract}
    if not endpoint:
        metadata["drill_sha256"] = drill_digest

    replay_lines = []
    for name, lane in inputs.items():
        digest = None
        if lane.report is not None:
            path = output / (name if endpoint else f"{name}.json")
            lane.report.save(path)
            digest = sha256(((path / MANIFEST) if endpoint else path).read_bytes()).hexdigest()
            replay_lines.append(f"Replay {name}  setrya replay {path.name}")
        digest_field = "manifest_sha256" if endpoint else "report_sha256"
        metadata[name] = {"commit": lane.commit, "test_exit": lane.test_exit, digest_field: digest}
    if endpoint:
        headline, _, detail = summary.partition("\n")
        if any(lane.report is None or lane.test_exit is None for lane in inputs.values()):
            replay_lines.append("Weakest evidence · incomplete")
        summary = "\n".join((headline, *replay_lines, detail))
    elif replay_lines:
        summary += "\n" + "\n".join(replay_lines)
    if all(lane.report is not None and lane.test_exit is not None for lane in inputs.values()):
        paths = "base candidate" if endpoint else "base.json candidate.json"
        summary += (f"\nReview again  setrya review {paths} --base-commit {base.commit}"
                    f" --candidate-commit {candidate.commit} --base-exit {base.test_exit}"
                    f" --candidate-exit {candidate.test_exit} --exit-contract {exit_contract}")
    save(metadata, output / "run-pr.json")
    _write_summary(output / "review.md", "two-user endpoint review" if endpoint else "release review", summary)
    if publish:
        print(summary)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setrya run-pr", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="local application repository")
    parser.add_argument("--base", required=True, help="local base Git ref or commit")
    parser.add_argument("--candidate", required=True, help="local candidate Git ref or commit")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--drill", type=Path, help="shared local search drill TOML")
    mode.add_argument("--endpoint", action="store_true", help="review a two-user test that calls check_endpoint once")
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
        drill_bytes = None if args.endpoint else read_regular_bytes(args.drill, "drill")
        with TemporaryDirectory(prefix="setrya-run-pr-") as scratch:
            root = Path(scratch).resolve()
            expected = digest = None
            if drill_bytes is not None:
                template = root / "drill.toml"
                template.write_bytes(drill_bytes)
                expected = Drill.load(template)
                digest = sha256(drill_bytes).hexdigest()
            # Reserve the artifact directory before executing either test. An
            # interrupted or skipped test can never pick up an older PASS.
            output = args.output.absolute()
            output.mkdir(parents=True, exist_ok=False)
            inputs = {name: ReviewInput(None, commit, None, "was not run") for name, commit in commits.items()}
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
