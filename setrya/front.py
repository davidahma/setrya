"""Primary commands for an erasure test and its reviewer."""
import argparse
from importlib.resources import files
from pathlib import Path
from shlex import join
import sys

from . import POSITIONING, __version__
from .drill import InputError
from .erasure.modes import CLI_MODES, evidence_mode
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
        ("doctor", "check fixture wiring and observations before running a drill"),
        ("replay", "recompute the verdict from saved evidence"),
        ("demo", "run the workspace deletion drill or another bundled example"),
        ("journal", "verify journal integrity or import copy events from OTLP"),
        ("branches", "check live forks and disclosed restore windows"),
        ("conformance", "run or review replayable vendor checks"),
        ("crypto-erasure", "observe AWS KMS key destruction using read-only credentials"),
        ("mcp", "serve local erasure tools over MCP stdio"),
        ("receipt", "summarize, pack or verify a deletion receipt"),
        ("recheck", "recheck due follow-up items using read-only evidence"),
        ("discovery", "import findings, scan Python PDF writes, and check reviewed mappings"),
        ("route-coverage", "match changed routes and jobs to candidate evidence"),
        ("coverage-drift", "find journalled systems without inspection or scanning coverage"),
        ("assess", "sign a multi-repository deletion assessment"),
        ("observe", "check known keys for completed deletion requests with read-only credentials"),
        ("verify", "authenticate a receipt and check its evidence against a trusted signer"),
    ):
        commands.add_parser(name, help=help_text)
    return parser


def _start(command, args):
    is_demo = command == "demo"
    description = ("Run a synthetic deletion test and save the results." if is_demo
                   else "Create a runnable person fixture or a tenant observation policy.")
    parser = argparse.ArgumentParser(prog=f"setrya {command}", description=description)
    parser.add_argument("--scope", choices=("person", "tenant"), default="person", help="deletion scope (default: person)")
    parser.add_argument("--output", type=Path, help="fresh output directory")
    if is_demo:
        parser.add_argument("example", choices=("workspace", "analyst", "memory-files", "branches", "northline",
                                               "northline-source", "legal-hold"), nargs="?",
                            help="person example (default: workspace)")
        parser.add_argument("--mode", type=evidence_mode, choices=CLI_MODES, help="person drill mode (default: write-back)")
    else:
        from .erasure.case_starters import STACKS
        parser.add_argument("--stack", choices=STACKS, help="connect a store and an HTTP application fixture")
    opts = parser.parse_args(args)
    if not is_demo and opts.stack:
        if opts.scope != "person":
            raise InputError("Stack starters use person scope.")
        from .erasure.case_starters import init_stack
        case = init_stack(opts.output or Path("setrya-case"), opts.stack)
        print(f"Created {case}. Configure the disposable app and storage bindings.")
        print(join(["setrya", "doctor", "--case", str(case)]))
        return 0
    if is_demo and opts.example in ("northline", "northline-source"):
        if opts.scope != "person" or opts.mode not in (None, "late-write"):
            raise InputError("Northline tests person erasure with a delayed refresh.")
        if opts.example == "northline-source":
            from .discovery.northline_source import demo
        else:
            from .discovery.northline import demo
        return demo(opts.output or Path(opts.example))
    if is_demo and opts.example == "branches":
        if opts.scope != "person" or opts.mode is not None:
            raise InputError("The branch demo supplies its own fork and restore schedule.")
        from .branches.demo import demo
        return demo(opts.output or Path("branches"))
    if is_demo and opts.example == "legal-hold":
        if opts.scope != "person" or opts.mode not in (None, "deletion"):
            raise InputError("The legal hold demo runs a person deletion with fixed maintenance steps.")
        from .examples.legal_hold import demo
        return demo(opts.output or Path("legal-hold"))
    if opts.scope == "tenant":
        from .erasure.cli import main
        return main([command, *args], prog="setrya")
    if is_demo:
        mode = opts.mode or "late-write"
        if opts.example == "memory-files":
            from .examples.memory_files import demo
            return demo(opts.output or Path("memory-files"), mode)
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
        if cmd == "branches":
            from .branches.cli import command
            return command(args)
        if cmd == "conformance":
            from .vendor.cli import command
            return command(args)
        if cmd == "crypto-erasure":
            from .casework.crypto_erasure import command
            return command(args)
        if cmd == "mcp":
            from .mcp_server import command
            return command(args)
        if cmd == "journal":
            from .erasure.journal_cli import command
            return command(args)
        if cmd == "route-coverage":
            from .route_coverage import main
            return main(args)
        if cmd == "assess":
            from .casework.assessment import command
            return command(args)
        if cmd == "coverage-drift":
            from .coverage_drift import main
            return main(args)
        if cmd == "recheck":
            from .casework.recheck import command
            return command(args)
        if cmd in ("receipt", "observe"):
            from .casework.cli import command
            return command(argv)
        if cmd == "doctor":
            from .erasure.doctor import run
            return run(args)
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
