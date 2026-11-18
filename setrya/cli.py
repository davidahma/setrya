"""Command-line entry point for check, replay, demo and qualification."""

import argparse
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import re
import shlex
import sqlite3
import sys

from . import __version__
from .checker import Status, Verdict, evaluate
from .drill import Drill, InputError
from .evidence import Report, matches_search
from .runner import RunResult, run

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "support-search"
DEMO_CASES = {"broken": Status.FAIL, "fixed": Status.PASS, "opaque": Status.NOT_TESTED}
DEMO_PASSAGES = {"refund@A": "30 days", "refund@B": "14 days"}

def preflight() -> None:
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
            lines.append(f"  Adapter error during {event['operation']} ({event['error']})")
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
    if re.fullmatch(r"examples/support-search/(drill|python|retired)\.toml", path):
        return "python3 -m setrya check " + shlex.quote(path)
    return 'python3 -m setrya check "$DRILL" --report run.json'


def replay_command(report_path: str | None) -> str:
    if report_path is None:
        return "Save with --report run.json, then run python3 -m setrya replay run.json"
    target = "run.json" if report_path == "run.json" else '"$REPORT"'
    return f"python3 -m setrya replay {target}"


def console_main() -> int:
    # Match python -m when the installed entry point is invoked from a checkout.
    sys.path.insert(0, str(Path.cwd()))
    return main()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="setrya", description="Test whether search stays current after document updates.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo_parser = commands.add_parser("demo", help="run three synthetic outcomes")
    demo_parser.add_argument("--case", choices=DEMO_CASES)
    check = commands.add_parser("check", help="run a drill")
    check.add_argument("drill")
    check.add_argument("--report", help="write a portable JSON report")
    check.add_argument("--summary", help="write a Markdown summary")
    replay = commands.add_parser("replay", help="recompute a verdict from saved observations")
    replay.add_argument("report")
    qualify = commands.add_parser("qualify", help="run the shipped qualification cases")
    qualify.add_argument("--case")
    qualify.add_argument("--quick", action="store_true", help="skip the 90-run repeatability case")
    args = parser.parse_args(argv)

    try:
        if args.command == "replay":
            report = Report.load(args.report)
            verdict = evaluate(report)
            print(format_verdict(verdict, report))
            print(history(report))
            return verdict.status.exit_code
        if args.command == "check":
            paths = [Path(path).resolve() for path in (args.drill, args.report, args.summary) if path is not None]
            if len(set(paths)) != len(paths):
                raise InputError("drill, report and summary must use distinct files")
            drill = Drill.load(args.drill)
            preflight()
            result = run(drill)
            if args.report:
                result.report.save(args.report)
            summary = format_verdict(result.verdict, result.report, rerun_command(args.drill),
                                     replay_command(args.report))
            print(summary)
            if args.summary:
                Path(args.summary).write_text(f"### Setrya · {result.report.rules.id}\n\n```text\n{summary}\n```\n",
                                              encoding="utf-8")
            return result.verdict.status.exit_code
        preflight()
        if args.command == "demo":
            return demo(args.case)
        from .qualification import qualify as run_qualification

        return run_qualification(args.case, args.quick)
    except InputError as error:
        print(f"ERROR  {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR  Interrupted. Fixture teardown requested.", file=sys.stderr)
        return 2
    except Exception as error:
        # Third-party errors may contain queries, snippets and local URLs.
        print(f"ERROR  {type(error).__name__} during {args.command}. Check adapter health and local permissions.",
              file=sys.stderr)
        return 2
