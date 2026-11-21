"""Command-line entry point for check, replay, demo and qualification."""

import argparse
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import re
import shlex
import sys
from tempfile import NamedTemporaryFile

from . import __version__
from .checker import Status, Verdict, evaluate
from .drill import Drill, InputError
from .evidence import Report, bundle_manifest, matches_search
from .runner import RunResult, run
from .offboarding.inputs import InvalidReport

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "setrya" / "examples" / "support-search"
PGVECTOR = ROOT / "setrya" / "examples" / "pgvector-search"
DEMO_CASES = {"broken": Status.FAIL, "fixed": Status.PASS, "opaque": Status.NOT_TESTED}
DEMO_PASSAGES = {"refund@A": "30 days", "refund@B": "14 days"}

def preflight() -> None:
    try:
        import sqlite3
    except ImportError:
        raise InputError("The SQLite fixture needs a Python build with SQLite FTS5 enabled.") from None
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE preflight USING fts5(body)")
    except sqlite3.Error:
        raise InputError("SQLite FTS5 is unavailable. Use a Python build with FTS5 enabled.") from None


def format_verdict(
    verdict: Verdict, report: Report, rerun: str | None = None, replay: str | None = None
) -> str:
    lines = [f"{verdict.status.label}  {verdict.detail}"]
    if verdict.probe:
        rule = next(probe for probe in report.rules.probes if probe.id == verdict.probe)
        lines.append(f"  Probe {rule.id} · route {rule.route} · scope {rule.scope} · top {rule.top_k}")
    if verdict.event:
        event = report.events[verdict.event - 1]
        lines.append(f"  Event {verdict.event} at {event['ns'] / 1_000_000:.1f} ms")
        if event["kind"] == "probe":
            lines.append(f"  Observed {passage_list(event)}")
    publication = next((event.get("publication") for event in report.events if event["kind"] == "published"), None)
    if publication:
        lines.append(f"  Published {publication['document']} revision {publication['revision']}")
    if verdict.status is Status.NOT_TESTED:
        latest = {event["probe_id"]: event for event in report.events if event["kind"] == "probe"}
        for event in latest.values():
            where = f"{event.get('route') or '?'} / {event.get('scope') or '?'}"
            lines.append(f"  Last {event['stage']} {event['probe_id']} ({where}) → {passage_list(event)}")
    if verdict.attribution:
        lines.append(f"  {verdict.attribution}")
    for event in report.events:
        if event["kind"] == "adapter_error":
            operation = event["operation"]
            detail = "Test error" if operation == "test" else f"Adapter error during {operation}"
            lines.append(f"  {detail} ({event['error']})")
    lines.append(f"  Run {report.run_id}")
    if replay:
        lines.append(f"  Replay  {replay}")
    if rerun:
        lines.append(f"  Rerun  {rerun}")
    return "\n".join(lines)


def passage_list(event: dict) -> str:
    return ", ".join(marker if marker is not None else "?" for marker in event["passages"]) or "(empty)"


def history(report: Report) -> str:
    lines = ["  History"]
    for event in report.events:
        if event["kind"] == "probe":
            where = f"{event.get('route') or '?'} / {event.get('scope') or '?'}"
            lines.append(f"    {event['seq']:>3}  {event['stage']} {event['probe_id']} ({where}) → {passage_list(event)}")
        elif event["kind"] == "receipt":
            lines.append(f"    {event['seq']:>3}  old write {event['outcome']}")
        elif event["kind"] == "published" and not event.get("publication"):
            lines.append(f"    {event['seq']:>3}  publication attempted")
        elif event["kind"] == "adapter_error":
            lines.append(f"    {event['seq']:>3}  adapter error during {event['operation']}")
        else:
            lines.append(f"    {event['seq']:>3}  {event['kind']}")
    return "\n".join(lines)


def demo_case(mode: str) -> RunResult:
    drill = Drill.load(EXAMPLE / "python.toml")
    return run(replace(drill, options={"mode": mode}))


def demo(selected: str | None) -> int:
    ok = True
    for mode in (selected,) if selected else DEMO_CASES:
        result = demo_case(mode)
        verdict = result.verdict
        ok &= verdict.status is DEMO_CASES[mode]
        print(f"{verdict.status.label:<11} {verdict.detail}")
        primary = next(probe for probe in result.report.rules.probes
                       if probe.id == result.report.rules.freshness_probe)
        observed = []
        for event in result.report.events:
            if event.get("probe_id") == primary.id and matches_search(event, primary):
                result_text = ", ".join(DEMO_PASSAGES.get(marker, marker or "?") for marker in event["passages"])
                result_text = result_text or "(empty)"
                if not observed or observed[-1] != result_text:
                    observed.append(result_text)
        if observed:
            print(f"            Refund policy in search · {' → '.join(observed)}")
        for event in result.report.events:
            if event["kind"] == "held":
                print(f"            Checkpoint held revision {event['revision']}")
            elif event["kind"] == "released":
                print("            Old worker released")
            elif event["kind"] == "receipt":
                print(f"            Old write {event['outcome']}")
            elif event["kind"] == "adapter_error":
                print(f"            Adapter error during {event['operation']} ({event['error']})")
        print(f"            Rerun  python3 -m setrya demo --case {mode}")
    return 0 if ok else 1


def rerun_command(path: str) -> str:
    # Reports and summaries avoid embedding private absolute paths or file names.
    if re.fullmatch(r"setrya/examples/(?:support-search/(?:drill|python|retired)|pgvector-search/(?:drill|reupload))\.toml", path):
        return "python3 -m setrya check " + shlex.quote(path)
    return 'python3 -m setrya check "$DRILL" --report run.json'


def replay_command(report_path: str | None, *, artifact: str = "run.json", variable: str = "REPORT") -> str:
    if report_path is None:
        return "Save with --report run.json, then run setrya replay run.json"
    target = artifact if Path(report_path).name == artifact else f'"${variable}"'
    return f"setrya replay {target}"


def console_main() -> int:
    args = sys.argv[1:]
    live_scenario = args[:1] == ["scenario"] and args[1:2] in (["update"], ["delete"], ["revoke"])
    if args[:1] != ["check"] and args[:2] != ["offboarding", "drill"] and not live_scenario:
        return main()
    # Only live checks import adapters from the working checkout.
    original_path = sys.path
    try:
        sys.path = [str(Path.cwd()), *original_path]
        return main()
    finally:
        sys.path = original_path


def _write_summary(path: str | Path, title: str, text: str) -> None:
    target = Path(path).absolute()
    temporary = None
    try:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise InputError("summary destination must be a regular file")
        with NamedTemporaryFile("w", dir=target.parent, prefix=".setrya-", encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(f"### Setrya · {title}\n\n```text\n{text}\n```\n")
        temporary.replace(target)
    except OSError:
        raise InputError("could not write summary. Check the destination directory.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _replay_answers(directory: Path):
    from .endpoint_evidence import MANIFEST, replay
    from .offboarding.inputs import InvalidReport, read_json

    try:
        manifest, _ = read_json(directory / MANIFEST, "answer cache manifest")
        if isinstance(manifest, dict) and manifest.get("format") == "setrya.selective-cache":
            from .examples.answer_cache.selective import replay as replay_selective
            return replay_selective(directory)
        return replay(directory)
    except InvalidReport as error:
        raise InputError(str(error)) from None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    from .front import dispatch

    primary = dispatch(argv)
    if primary is not None:
        return primary
    if argv[:2] == ["demo", "refund"]:
        argv = ["demo", *argv[2:]]
    if argv[:1] == ["discovery"]:
        from .discovery.cli import main as discovery_main

        return discovery_main(argv[1:])
    if argv[:1] == ["cache-metrics"]:
        from .examples.answer_cache.metrics import main as metrics_main

        return metrics_main(argv[1:])
    if argv[:1] == ["answer-cache"]:
        from .examples.answer_cache.drill import main as answer_cache_main

        return answer_cache_main(argv[1:])
    if argv[:2] in (["demo", "erasure"], ["init", "erasure"]):
        from .erasure.cli import main as erasure_main

        return erasure_main([argv[0], *argv[2:]])
    if argv[:1] == ["erasure"]:
        from .erasure.cli import main as erasure_main

        return erasure_main(argv[1:])
    if argv[:1] == ["coverage"]:
        from .coverage import main as coverage_main

        return coverage_main(argv[1:])
    if argv[:1] == ["run-pr"]:
        from .run_pr import main as run_pr

        return run_pr(argv[1:])
    if argv[:1] == ["scenario"]:
        from .scenarios import main as scenario_main

        return scenario_main(argv[1:])
    if argv[:1] == ["offboarding"]:
        from .offboarding.cli import main as offboarding_main

        return offboarding_main(argv[1:])
    parser = argparse.ArgumentParser(prog="setrya", description="Test what an AI assistant or search endpoint returns after access revocation, a document update, or tenant offboarding.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discovery", help="verify a catalog finding through a disposable AI application")
    init = commands.add_parser("init", help="create a starter test in your application checkout")
    starters = init.add_subparsers(dest="starter", required=True)
    erasure = starters.add_parser("erasure", help="test deletion through a LangGraph application")
    erasure.add_argument("--output", type=Path, default=Path("setrya-erasure"))
    commands.add_parser("erasure", help="test person or tenant erasure and review signed evidence")
    endpoint = starters.add_parser(
        "endpoint", help="test your answer endpoint as a revoked user and a retained colleague",
        description="Write a pytest starter to verify two authenticated endpoint reads, then exercise the delayed cache-fill worker.",
    )
    endpoint.add_argument("--framework", choices=("generic", "celery-langchain"), default="generic",
                          help="choose native hooks or a Celery/LangChain worker coordinated by Redis")
    endpoint.add_argument("--output", type=Path, default=Path("tests/test_setrya_endpoint.py"),
                          help="new pytest file (default: tests/test_setrya_endpoint.py)")
    commands.add_parser("scenario", help="drive update, deletion or access-revocation races and replay a schedule")
    commands.add_parser("offboarding", help="check tenant offboarding in CI or review one deletion request")
    commands.add_parser("cache-metrics", help="measure checked answers and cache outcomes from local evidence")
    commands.add_parser("answer-cache", help="test a late Celery cache fill against a real answer endpoint")
    commands.add_parser("coverage", help="find changed routes and jobs without matching candidate evidence")
    commands.add_parser("run-pr", help="run an existing test on two Git revisions and review its evidence")
    demo_parser = commands.add_parser("demo", help="replay access revocation or run the offline refund-policy demo")
    demos = demo_parser.add_mutually_exclusive_group()
    demos.add_argument("example", nargs="?", choices=("access", "erasure"), help="run the access or subject erasure example")
    demos.add_argument("--case", choices=DEMO_CASES, help="run one case of the refund-policy search demo")
    demo_parser.add_argument("--output", type=Path, help="new evidence directory for the erasure demo")
    check = commands.add_parser("check", help="run a drill")
    check.add_argument("drill")
    check.add_argument("--report", help="write a portable JSON report")
    check.add_argument("--summary", help="write a Markdown summary")
    check.add_argument("--revision", help="record the full Git commit ID supplying the application")
    replay = commands.add_parser("replay", help="recompute a verdict from saved observations")
    replay.add_argument("report")
    replay.add_argument("--expect-request", help="independent tenant request SHA-256 digest")
    replay.add_argument("--expect-job", help="expected tenant job ID")
    replay.add_argument("--case-review", action="store_true", help="also gate on open case items")
    diagnose = commands.add_parser("diagnose", help="show the verdict and the first missing observation")
    diagnose.add_argument("report")
    comparison = commands.add_parser("compare", help="tell whether a before-and-after pair establishes a fix")
    comparison.add_argument("before")
    comparison.add_argument("after")
    comparison.add_argument("--summary", help="write a Markdown summary")
    review_parser = commands.add_parser("review", help="review two CI reports, endpoint bundles, or scenario directories")
    review_parser.add_argument("before")
    review_parser.add_argument("after")
    review_parser.add_argument("--base-commit", help="expected full base Git commit ID (required)")
    review_parser.add_argument("--candidate-commit", help="expected full candidate Git commit ID (required)")
    review_parser.add_argument("--base-exit", help="native base test exit (required)")
    review_parser.add_argument("--candidate-exit", help="native candidate test exit (required)")
    review_parser.add_argument("--exit-contract", choices=("pytest", "setrya"),
                               help="native exits (default: pytest for search/endpoint, setrya for scenarios)")
    review_parser.add_argument("--summary", help="write a Markdown summary")
    qualify = commands.add_parser("qualify", help="run the shipped qualification cases")
    qualify.add_argument("--case")
    qualify.add_argument("--quick", action="store_true", help="skip the 90-run repeatability case")
    args = parser.parse_args(argv)
    error_summary = None

    try:
        if args.command == "init":
            from .starter import write_endpoint_test

            write_endpoint_test(args.output, framework=args.framework)
            print(f"Created {args.output}")
            print("Read the two prepared sessions first  " + shlex.join(
                ["python", "-m", "pytest", "-q", "-s", "-p", "setrya.pytest_plugin", "--setrya-reads-only", "--", str(args.output)]))
            print("Run  " + shlex.join(["python", "-m", "pytest", "-q", "-s", "--", str(args.output)]))
            return 0
        if args.command in {"replay", "diagnose"}:
            directory = Path(args.report)
            manifest = bundle_manifest(directory) if directory.is_dir() else None
            receipt_names = ("case-result.json", "erasure-receipt.json", "receipt.json")
            receipt_file = directory.is_file() and directory.name in receipt_names
            historical = directory.is_dir() and manifest is None and (directory / "receipt.json").is_file()
            if manifest in receipt_names or receipt_file or historical:
                from .receipts import replay as replay_receipt, report as receipt_report

                receipt = replay_receipt(directory, expected_request=getattr(args, "expect_request", None),
                                        expected_job=getattr(args, "expect_job", None))
                return receipt_report(receipt, case_review=getattr(args, "case_review", False))
            if getattr(args, "expect_request", None) or getattr(args, "expect_job", None):
                raise InputError("Tenant pins require a tenant receipt.")
            if getattr(args, "case_review", False):
                raise InputError("--case-review requires a deletion receipt.")
            if manifest == "endpoint-reads.json":
                from .endpoint_reads import replay as replay_reads

                verdict, text = replay_reads(directory)
                print(text)
                return verdict.status.exit_code
            if manifest == "answer-cache.json":
                verdict, text = _replay_answers(directory)
                print(text)
                return verdict.status.exit_code
            if manifest == "scenario.json" or (directory.is_dir() and args.command == "replay"):
                from .scenarios import main as scenario_main

                return scenario_main(["replay", args.report])
            report = Report.load(args.report)
            verdict = evaluate(report)
            print(format_verdict(verdict, report))
            if args.command == "replay":
                print(history(report))
            return verdict.status.exit_code
        if args.command in {"compare", "review"}:
            from .comparison import compare

            if args.summary:
                destination = Path(args.summary).resolve()
                lexical = Path(os.path.abspath(args.summary))
                for path in (Path(args.before), Path(args.after)):
                    source = path.resolve()
                    if destination == source or (path.is_dir() and (
                        destination.is_relative_to(source) or lexical.is_relative_to(os.path.abspath(path))
                    )):
                        raise InputError("comparison summary must use a distinct file outside the evidence directories")
            error_summary = args.summary
            if args.command == "review":
                from .review import ReviewInput, review_inputs

                base = ReviewInput.load(args.before, args.base_commit, args.base_exit)
                candidate = ReviewInput.load(args.after, args.candidate_commit, args.candidate_exit)
                lanes = (("base", base, args.before), ("candidate", candidate, args.after))
                named_directories = frozenset(name for name, _, path in lanes if Path(path) == Path(name))
                code, summary = review_inputs(base, candidate, named_directories=named_directories,
                                              exit_contract=args.exit_contract)
                replay_lines = []
                for name, lane, path in lanes:
                    if lane.report is not None:
                        variable = f"{name.upper()}_REPORT"
                        if isinstance(lane.report, Report):
                            command = replay_command(path, artifact=f"{name}.json", variable=variable)
                        else:
                            from .review_scenarios import replay_scenario_command

                            command = replay_scenario_command(name, named_directories)
                            if name not in named_directories:
                                replay_lines.append(f"Set {variable} to the path of the downloaded {name} evidence directory.")
                        replay_lines.append(f"Replay {name}  {command}")
                from .review_endpoint import EndpointEvidence

                if any(isinstance(lane.report, EndpointEvidence) for _, lane, _ in lanes):
                    headline, _, detail = summary.partition("\n")
                    summary = "\n".join((headline, *replay_lines, detail))
                elif replay_lines:
                    summary += "\n" + "\n".join(replay_lines)
                title = f"{base.report.rules.id} release review" if isinstance(base.report, Report) else "release review"
            else:
                before, after = Report.load(args.before), Report.load(args.after)
                code, summary = compare(before, after)
                title = f"{before.rules.id} comparison"
            print(summary)
            error_summary = None
            if args.summary:
                _write_summary(args.summary, title, summary)
            return code
        if args.command == "check":
            paths = [Path(path).resolve() for path in (args.drill, args.report, args.summary) if path is not None]
            if len(set(paths)) != len(paths):
                raise InputError("drill, report and summary must use distinct files")
            report_path = Path(args.report).absolute() if args.report else None
            summary_path = Path(args.summary).absolute() if args.summary else None
            error_summary = summary_path
            drill = Drill.load(args.drill)
            if drill.factory in {"setrya.examples.support_search.adapter:http_fixture",
                                 "setrya.examples.support_search.adapter:python_fixture"}:
                preflight()
            result = run(drill, revision=args.revision)
            if report_path:
                result.report.save(report_path)
            replay_hint = replay_command(args.report)
            summary = format_verdict(result.verdict, result.report, rerun_command(args.drill), replay_hint)
            print(summary)
            interrupted = next((e for e in result.report.events if e["kind"] == "adapter_error"
                                and e["error"] == "KeyboardInterrupt"), None)
            if interrupted is not None:
                print(f"Interrupted during {interrupted['operation']}. {replay_hint}", file=sys.stderr)
            error_summary = None
            if summary_path:
                _write_summary(summary_path, result.report.rules.id, summary)
            return result.verdict.status.exit_code
        if args.command == "demo":
            if args.output is not None:
                raise InputError("--output requires demo erasure")
            if args.example == "access":
                from .examples.access_demo import demo_access

                return demo_access()
            preflight()
            return demo(args.case)
        if args.case not in {"pgvector", "offboarding", "offboarding-compatibility"}:
            preflight()
        from .qualification import qualify as run_qualification

        return run_qualification(args.case, args.quick)
    except (InputError, InvalidReport) as error:
        diagnostic = f"ERROR  {error}"
    except KeyboardInterrupt:
        diagnostic = "ERROR  Interrupted. Fixture teardown requested."
    except Exception as error:
        # Third-party errors may contain queries, snippets and local URLs.
        diagnostic = f"ERROR  {type(error).__name__} during {args.command}. Check adapter health and local permissions."
    print(diagnostic, file=sys.stderr)
    if error_summary:
        try:
            _write_summary(error_summary, f"{args.command} error", diagnostic)
        except InputError as error:
            print(f"ERROR  {error}", file=sys.stderr)
    return 2
