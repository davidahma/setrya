"""Primary commands for an erasure test and its reviewer."""
import argparse
from importlib.resources import files
from pathlib import Path
import runpy
from shlex import join
import sys

from . import POSITIONING, __version__
from .drill import InputError
from .offboarding.inputs import InvalidReport


def help_parser():
    parser = argparse.ArgumentParser(prog="setrya", description=POSITIONING,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="Check fixture wiring before a run with setrya doctor --help.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(title="commands", dest="command", required=True)
    for name, help_text in (
        ("init", "create a working delete-only starter for your application"),
        ("run", "test erasure in a disposable application and save a receipt"),
        ("replay", "recompute the verdict from saved evidence"),
        ("demo", "run the two-user workspace example or the analyst investigation"),
        ("verify", "authenticate a receipt and check its evidence against a trusted signer"),
    ):
        commands.add_parser(name, help=help_text)
    return parser


def doctor(argv):
    from .erasure.v3 import SharedCase, observe_baseline

    parser = argparse.ArgumentParser(prog="setrya doctor", description="Read a trusted fixture's baseline before erasure.")
    parser.add_argument("--starter", type=Path, required=True)
    parser.add_argument("--mode", choices=("deletion", "late-write"), default="deletion")
    args = parser.parse_args(argv)
    if not args.starter.is_file():
        raise InputError("Supply a trusted starter file with --starter.")
    source = args.starter.resolve()
    original = sys.path
    try:
        sys.path = [str(source.parent), str(Path.cwd()), *original]
        hooks = runpy.run_path(str(source))
        case, open_fixture = hooks.get("CASE"), hooks.get("open_fixture")
        if not isinstance(case, SharedCase) or not callable(open_fixture):
            raise InputError("The trusted starter must define a SharedCase named CASE and open_fixture().")
        case.spec()
        with open_fixture() as (subject, app, items):
            required = ["inspect", "read", "erase"]
            if case.scan_stores:
                required.append("scan")
            if case.require_lineage:
                required.append("lineage")
            if args.mode == "late-write":
                required += ["hold", "checkpoint", "release_and_drain"]
            missing = [name for name in required if not callable(getattr(app, name, None))]
            if missing:
                print("NOT TESTED  Missing fixture methods: " + ", ".join(missing))
                return 3
            if not observe_baseline(subject, case, app, external_items=items):
                print("NOT TESTED  The baseline could not establish the declared keys, callers, questions or scans.")
                return 3
            print(f"READY  Read {len(case.artifacts)} declared keys and both questions for {len(case.callers)} caller(s).")
            print("Erasure and worker hooks will be exercised by setrya run.")
            return 0
    except NotImplementedError:
        print("NOT TESTED  Connect open_fixture to your disposable application.")
        return 3
    except SystemExit:
        raise InputError("The starter exited before its baseline could be checked.") from None
    finally:
        sys.path = original


def _start(command, args):
    is_demo = command == "demo"
    description = ("Run a synthetic deletion test and save the results." if is_demo
                   else "Create a runnable person fixture or a tenant observation policy.")
    parser = argparse.ArgumentParser(prog=f"setrya {command}", description=description)
    parser.add_argument("--scope", choices=("person", "tenant"), default="person", help="deletion scope (default: person)")
    parser.add_argument("--output", type=Path, help="fresh output directory")
    if is_demo:
        parser.add_argument("example", choices=("workspace", "analyst"), nargs="?", help="person example (default: workspace)")
        parser.add_argument("--mode", choices=("deletion", "late-write"), help="person drill mode (default: late-write)")
    opts = parser.parse_args(args)
    if opts.scope == "tenant":
        from .erasure.cli import main
        return main([command, *args], prog="setrya")
    if is_demo:
        mode = opts.mode or "late-write"
        if opts.example == "analyst":
            from .erasure.investigation import run_demo
            return run_demo(opts.output or Path("investigation"), mode=mode)
        from .examples.workspace import demo as run_workspace
        return run_workspace(opts.output or Path("workspace"), mode)

    path = opts.output or Path("setrya-case")
    body = files("setrya.erasure").joinpath("workspace_starter.py").read_bytes()
    path.mkdir(parents=True, exist_ok=False)
    starter = path / "test_erasure.py"
    starter.write_bytes(body)
    print("Created a synthetic fixture. Replace CASE and open_fixture to test your app.")
    print(join(["setrya", "doctor", "--starter", str(starter)]))
    return 0


def dispatch(argv):
    if not argv or argv in (["--help"], ["-h"], ["--version"]):
        help_parser().parse_args(argv)
    cmd, args = argv[0], argv[1:]
    # Route old commands only when explicitly named. Let argparse validate all
    # other spellings, including --option=value, against the primary interface.
    options = {arg.partition("=")[0] for arg in args if arg.startswith("--")}
    first = args[0] if args else None
    try:
        if cmd == "doctor":
            return doctor(args)
        if cmd in ("run", "verify"):
            from .erasure.cli import main
            return main([cmd, *args], prog="setrya")
        if (cmd == "demo" and first not in ("refund", "erasure", "access") and "--case" not in options
                or cmd == "init" and first not in ("endpoint", "erasure")):
            if "--example" in options:
                from .erasure.cli import main
                return main(argv, prog="setrya")
            return _start(cmd, args)
    except FileExistsError:
        print("ERROR  Output already exists. Choose a new directory with --output.", file=sys.stderr)
        return 2
    except NotImplementedError:
        print("NOT TESTED  Connect the fixture before running this check.", file=sys.stderr)
        return 3
    except Exception as exc:
        message = str(exc) if isinstance(exc, (InputError, InvalidReport)) else type(exc).__name__
        print(f"ERROR  {message}", file=sys.stderr)
        return 2
    return None
