"""Import a discovery finding, exercise a trusted fixture, and return a portable case result."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import os
from pathlib import Path
import runpy
import sys

from setrya.drill import InputError
from setrya.erasure.modes import CLI_MODES, evidence_mode
from setrya.erasure.receipt import MAX_BUNDLE_FILE_BYTES
from setrya.offboarding import signature
from setrya.offboarding.inputs import InvalidReport, publish_new, publish_new_bytes, read_regular_bytes, require


def report(directory, *, expected_binding=None, case_review=False):
    from .case import replay
    from setrya.receipts import report as receipt_report

    result = replay(directory, expected_binding=expected_binding)
    return receipt_report(result, case_review=case_review, bundle=directory)


def _binding_options(parser, required):
    parser.add_argument("--finding", type=Path, required=required, help="one Macie finding or its normalized import")
    parser.add_argument("--mapping", type=Path, required=required, help="trusted, application-owned case mapping JSON")


def _binding(args):
    from .macie import load_finding
    from .mapping import bind_finding, load_mapping

    require(bool(args.finding) == bool(args.mapping), "Supply both --finding and --mapping.")
    return bind_finding(load_finding(args.finding), load_mapping(args.mapping)) if args.finding else None


def _run(args):
    from .case import record_retention, replay, run_case
    from setrya.receipts import report as receipt_report

    binding = _binding(args)
    require(not os.path.lexists(args.output), "Choose a fresh case directory.")
    if binding["source"]["action"] == "retain":
        result = record_retention(binding, output=args.output)
        return receipt_report(result)
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
    return receipt_report(verified, bundle=args.output)


def main(argv):
    parser = argparse.ArgumentParser(prog="setrya discovery", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name, description in (("demo", "exercise unread, broken and guarded cache cases"),
                              ("init", "write synthetic finding, mapping and a connected sample fixture")):
        sub = commands.add_parser(name, help=description)
        sub.add_argument("--output", required=True, type=Path, help="new directory")
        if name == "init":
            sub.add_argument("--example", choices=("investigation", "northline", "northline-source"), default="investigation",
                             help="choose the packaged finding and fixture")
    scan = commands.add_parser("source-scan", help="find possible Python PDF writes without running application code")
    check = commands.add_parser("source-check", help="check candidate PDF writes against approved discovery services")
    for command in (scan, check):
        command.add_argument("source", type=Path, help="directory of Python source to scan")
        command.add_argument("--output", type=Path, help="write a new JSON report")
    _binding_options(check, True)
    check.add_argument("--approvals", type=Path, help="reviewed candidate-to-service approvals")
    check.add_argument("--baseline", type=Path, help="source-check report from a trusted earlier revision")
    check.add_argument("--receipt", type=Path, help="optional replayable discovery receipt directory")
    copy_scan = commands.add_parser("copy-scan", help="find possible customer-data copies without a fixture")
    copy_scan.add_argument("source", type=Path, nargs="?", default=Path.cwd(), help="source directory")
    copy_check = commands.add_parser("copy-check", help="review new copy sites from the Git merge base to a candidate")
    copy_check.add_argument("--repo", type=Path, default=Path("."))
    copy_check.add_argument("--base", required=True)
    copy_check.add_argument("--candidate", default="HEAD")
    copy_check.add_argument("--map", default="setrya-copy-coverage.json", help="reviewed mapping path relative to the repository")
    copy_check.add_argument("--receipt", type=Path, action="append", default=[], help="replayable v3.2 person evidence bundle")
    for command in (copy_scan, copy_check):
        command.add_argument("--output", type=Path, help="write a new JSON report")
        command.add_argument("--language", choices=("python", "typescript", "all"), default="python",
                             help="source language to scan (default: python)")
    normalize = commands.add_parser("import", help="normalize a Macie finding without connecting to AWS")
    normalize.add_argument("--finding", required=True, type=Path)
    normalize.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run", help="test an authorized mapping against a trusted local application fixture")
    _binding_options(run, True)
    run.add_argument("--fixture", type=Path)
    run.add_argument("--mode", type=evidence_mode, choices=CLI_MODES, default="deletion")
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
        if args.action == "demo":
            from .demo import run_demo
            return run_demo(args.output)
        if args.action in ("source-scan", "source-check"):
            from .source_coverage import assess, load_document, scan, summarize
            result = (scan(args.source) if args.action == "source-scan" else
                      assess(args.source, _binding(args),
                             approvals=load_document(args.approvals, "source approvals"),
                             baseline=load_document(args.baseline, "baseline source review"),
                             bundle=args.receipt))
            if args.output:
                publish_new(result, args.output)
            print(summarize(result))
            return 3 if result.get("review_required") else 0
        if args.action in ("copy-scan", "copy-check"):
            from . import copy_sites
            result = (copy_sites.scan(args.source, language=args.language) if args.action == "copy-scan" else
                      copy_sites.check(args.repo, args.base, args.candidate, map_path=args.map, receipts=args.receipt,
                                       language=args.language))
            if args.output:
                publish_new(result, args.output)
            print(copy_sites.summarize(result))
            return 3 if copy_sites.review_needed(result) else 0
        if args.action == "init":
            if args.example == "northline-source":
                from .northline_source import init_example
            elif args.example == "northline":
                from .northline import init_example
            else:
                from .demo import init_example
            init_example(args.output)
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
