"""Import a discovery finding, exercise a trusted fixture, and return a portable case result."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import os
from pathlib import Path
import runpy
import sys

from setrya.drill import InputError
from setrya.erasure.receipt import MAX_BUNDLE_FILE_BYTES
from setrya.offboarding import signature
from setrya.offboarding.inputs import InvalidReport, publish_new, publish_new_bytes, read_regular_bytes, require


def report(directory, *, expected_binding=None, case_review=False):
    from .case import exit_code, replay, summary

    result = replay(directory, expected_binding=expected_binding)
    print(summary(result))
    return exit_code(result, case_review=case_review)


def _binding_options(parser, required):
    parser.add_argument("--finding", type=Path, required=required, help="one Macie finding or its normalized import")
    parser.add_argument("--mapping", type=Path, required=required, help="trusted, application-owned case mapping JSON")


def _binding(args):
    from .macie import load_finding
    from .mapping import bind_finding, load_mapping

    require(bool(args.finding) == bool(args.mapping), "Supply both --finding and --mapping.")
    return bind_finding(load_finding(args.finding), load_mapping(args.mapping)) if args.finding else None


def _run(args):
    from .case import exit_code, record_retention, replay, run_case, summary

    binding = _binding(args)
    require(not os.path.lexists(args.output), "Choose a fresh case directory.")
    if binding["source"]["action"] == "retain":
        result = record_retention(binding, output=args.output)
        print(summary(result))
        return exit_code(result)
    require(args.fixture is not None, "Supply --fixture for an application deletion or rewrite.")
    fixture = args.fixture.resolve()
    require(fixture.is_file() and fixture.suffix == ".py", "Supply a trusted local Python fixture file.")
    previous_path = sys.path
    produced = None
    try:
        # A fixture can import its own application. Passive import and replay never load it.
        sys.path = [str(fixture.parent), *previous_path]
        with redirect_stdout(sys.stderr):
            open_fixture = runpy.run_path(str(fixture)).get("open_fixture")
            require(callable(open_fixture), "The fixture must define open_fixture(binding).")
            with open_fixture(deepcopy(binding)) as settings:
                require(isinstance(settings, dict), "open_fixture must yield the run_case keyword arguments.")
                produced = run_case(binding, **settings, output=args.output, mode=args.mode)
    except (InvalidReport, InputError):
        raise
    except (Exception, SystemExit) as error:
        # Application exceptions can contain raw customer records and request credentials.
        raise InputError(f"Discovery fixture failed ({type(error).__name__}).") from None
    finally:
        sys.path = previous_path
    # Fixture teardown may move or clean up its output. Only report a replayable case.
    require(produced is not None, "The fixture ended without producing a case result.")
    verified = replay(args.output, expected_binding=binding)
    require(verified == produced, "The output no longer matches the case produced by this run.")
    print(summary(verified))
    return exit_code(verified)


def main(argv):
    parser = argparse.ArgumentParser(prog="setrya discovery", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name, description in (("demo", "exercise unread, broken and guarded cache cases"),
                              ("init", "write synthetic finding, mapping and a connected sample fixture")):
        sub = commands.add_parser(name, help=description)
        sub.add_argument("--output", required=True, type=Path, help="new directory")
    normalize = commands.add_parser("import", help="normalize a Macie finding without connecting to AWS")
    normalize.add_argument("--finding", required=True, type=Path)
    normalize.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run", help="test an authorized mapping against a trusted local application fixture")
    _binding_options(run, True)
    run.add_argument("--fixture", type=Path)
    run.add_argument("--mode", choices=("deletion", "late-write"), default="deletion")
    run.add_argument("--output", required=True, type=Path)
    replay = commands.add_parser("replay", help="recompute the engineering or case-review result offline")
    replay.add_argument("directory", type=Path)
    replay.add_argument("--case-review", action="store_true")
    _binding_options(replay, False)
    export = commands.add_parser("export", help="write the exact verified case-result JSON for the originating case")
    export.add_argument("directory", type=Path)
    export.add_argument("--output", required=True, type=Path)
    _binding_options(export, False)
    signature.add_commands(commands, local_keys=True, aliases=True, scope=False)
    args = parser.parse_args(argv)
    try:
        if args.action in ("demo", "init"):
            from . import demo
            if args.action == "demo":
                return demo.run_demo(args.output)
            demo.init_example(args.output)
            print(f"Created discovery example in {args.output}")
            return 0
        if args.action == "import":
            from .macie import load_finding
            publish_new(load_finding(args.finding), args.output)
            print(f"Wrote normalized Macie finding to {args.output}")
            return 0
        if args.action == "run":
            return _run(args)
        if args.action == "replay":
            return report(args.directory, expected_binding=_binding(args), case_review=args.case_review)
        from .case import replay_bytes
        if args.action == "export":
            raw = read_regular_bytes(args.directory / "case-result.json", "case result", MAX_BUNDLE_FILE_BYTES)
            replay_bytes(args.directory, raw, expected_binding=_binding(args))
            publish_new_bytes(raw, args.output, max_bytes=MAX_BUNDLE_FILE_BYTES)
            print(f"Wrote verified result to {args.output}")
            return 0
        require(args.receipt.name == "case-result.json", "Supply a discovery bundle's case-result.json.")
        args.offboarding_action = "sign-receipt" if args.action in ("sign", "sign-receipt") else "verify-signature"
        return signature.run(args, receipt_limit=MAX_BUNDLE_FILE_BYTES,
                             validate=lambda raw: replay_bytes(args.receipt.parent, raw))
    except (InputError, InvalidReport) as error:
        print(f"Discovery error. {error}", file=sys.stderr)
    except (OSError, ValueError, RecursionError):
        print("Discovery error. Check the input format, fixture, and output location.", file=sys.stderr)
    return 2
