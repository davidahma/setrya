"""Command-line entry point for check, replay, demo and qualification."""

import argparse
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import re
import shlex
import sys
from tempfile import NamedTemporaryFile

from . import __version__
from .checker import Status, Verdict, evaluate
from .drill import Drill, InputError
from .evidence import Report, matches_search
from .runner import RunResult, run

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "support-search"
PGVECTOR = ROOT / "examples" / "pgvector-search"
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
    if re.fullmatch(r"examples/(?:support-search/(?:drill|python|retired)|pgvector-search/(?:drill|reupload))\.toml", path):
        return "python3 -m setrya check " + shlex.quote(path)
    return 'python3 -m setrya check "$DRILL" --report run.json'


def replay_command(report_path: str | None, *, artifact: str = "run.json", variable: str = "REPORT") -> str:
    if report_path is None:
        return "Save with --report run.json, then run setrya replay run.json"
    target = artifact if Path(report_path).name == artifact else f'"${variable}"'
    return f"setrya replay {target}"


def console_main() -> int:
    if sys.argv[1:2] != ["check"]:
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # The offboarding evidence formats have their own CLI exit-code contract.
    if argv[:1] == ["offboarding"]:
        from .offboarding.cli import main as offboarding_main

        return offboarding_main(argv[1:])
    parser = argparse.ArgumentParser(prog="setrya", description="Test what an application can retrieve after a document update or tenant offboarding.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("offboarding", help="check tenant offboarding in CI or review one deletion request")
    demo_parser = commands.add_parser("demo", help="run three synthetic outcomes")
    demo_parser.add_argument("--case", choices=DEMO_CASES)
    check = commands.add_parser("check", help="run a drill")
    check.add_argument("drill")
    check.add_argument("--report", help="write a portable JSON report")
    check.add_argument("--summary", help="write a Markdown summary")
    check.add_argument("--revision", help="record the full Git commit ID supplying the application")
    replay = commands.add_parser("replay", help="recompute a verdict from saved observations")
    replay.add_argument("report")
    diagnose = commands.add_parser("diagnose", help="show the verdict and the first missing observation")
    diagnose.add_argument("report")
    comparison = commands.add_parser("compare", help="tell whether a before-and-after pair establishes a fix")
    comparison.add_argument("before")
    comparison.add_argument("after")
    comparison.add_argument("--summary", help="write a Markdown summary")
    review_parser = commands.add_parser("review", help="decide a base-versus-candidate CI check from two reports")
    review_parser.add_argument("before")
    review_parser.add_argument("after")
    review_parser.add_argument("--base-commit", help="expected full base Git commit ID (required)")
    review_parser.add_argument("--candidate-commit", help="expected full candidate Git commit ID (required)")
    review_parser.add_argument("--base-exit", help="native base test exit (required)")
    review_parser.add_argument("--candidate-exit", help="native candidate test exit (required)")
    review_parser.add_argument("--summary", help="write a Markdown summary")
    qualify = commands.add_parser("qualify", help="run the shipped qualification cases")
    qualify.add_argument("--case")
    qualify.add_argument("--quick", action="store_true", help="skip the 90-run repeatability case")
    args = parser.parse_args(argv)
    error_summary = None

    try:
        if args.command in {"replay", "diagnose"}:
            report = Report.load(args.report)
            verdict = evaluate(report)
            print(format_verdict(verdict, report))
            if args.command == "replay":
                print(history(report))
            return verdict.status.exit_code
        if args.command in {"compare", "review"}:
            from .comparison import compare

            if args.summary and Path(args.summary).resolve() in {Path(args.before).resolve(), Path(args.after).resolve()}:
                raise InputError("comparison summary must use a distinct file")
            error_summary = args.summary
            if args.command == "review":
                from .review import ReviewInput, review_inputs

                base = ReviewInput.load(args.before, args.base_commit, args.base_exit)
                candidate = ReviewInput.load(args.after, args.candidate_commit, args.candidate_exit)
                code, summary = review_inputs(base, candidate)
                for name, lane, path in (("base", base, args.before), ("candidate", candidate, args.after)):
                    if lane.report is not None:
                        command = replay_command(path, artifact=f"{name}.json", variable=f"{name.upper()}_REPORT")
                        summary += f"\nReplay {name}  {command}"
                title = f"{base.report.rules.id} release review" if base.report else "release review"
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
            if drill.factory in {"examples.support_search.adapter:http_fixture",
                                 "examples.support_search.adapter:python_fixture"}:
                preflight()
            result = run(drill, revision=args.revision)
            if report_path:
                result.report.save(report_path)
            summary = format_verdict(result.verdict, result.report, rerun_command(args.drill),
                                     replay_command(args.report))
            print(summary)
            error_summary = None
            if summary_path:
                _write_summary(summary_path, result.report.rules.id, summary)
            return result.verdict.status.exit_code
        if args.command == "demo":
            preflight()
            return demo(args.case)
        if args.case not in {"pgvector", "offboarding", "offboarding-compatibility"}:
            preflight()
        from .qualification import qualify as run_qualification

        return run_qualification(args.case, args.quick)
    except InputError as error:
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
