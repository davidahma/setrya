"""Run or review an erasure drill scoped to one person or a tenant."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from setrya.drill import InputError
from setrya.offboarding import signature
from setrya.offboarding.inputs import InvalidReport, read_regular_bytes, unique_fields
from .receipt import EXIT, MAX_BUNDLE_FILE_BYTES, RECEIPT, replay_bytes


def validated(directory, raw):
    header = json.loads(raw.decode(), object_pairs_hook=unique_fields)
    if isinstance(header, dict) and header.get("format") == "setrya.erasure.receipt.v3":
        from .v3_receipt import replay_bytes as shared_replay

        return shared_replay(directory, raw)
    return replay_bytes(directory, raw)


def load_directory(directory):
    from setrya.evidence import bundle_manifest

    directory = Path(directory)
    if bundle_manifest(directory) != RECEIPT:
        raise InputError("Expected a person erasure bundle with one manifest.")
    raw = read_regular_bytes(directory / RECEIPT, "erasure receipt", MAX_BUNDLE_FILE_BYTES)
    return validated(directory, raw)


def replay_directory(directory, *, case_review=False):
    return report(load_directory(directory), case_review=case_review)


def receipt_exit(receipt, *, case_review=False):
    if receipt["version"] == 3:
        from .v3_receipt import exit_code

        return exit_code(receipt, case_review=case_review)
    return EXIT[receipt["verdict"]]


def report(receipt, *, case_review=False):
    if receipt["version"] == 3:
        from .v3_receipt import summary

        print(summary(receipt))
        return receipt_exit(receipt, case_review=case_review)
    print(f"{receipt['verdict']}  {receipt['unknown_copies']} unknown copies  "
          f"drill duration {receipt['time_to_first_verdict_ms']} ms")
    for copy in receipt["copies"]:
        print(f"{copy['status']}  {copy['store']}  {copy['key']}")
    for reason in receipt["failures"] + receipt["gaps"]:
        print(reason)
    for error in receipt["errors"]:
        print(error["operation"], error["error"])
    return EXIT[receipt["verdict"]]


def main(argv):
    parser = argparse.ArgumentParser(prog="setrya erasure", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    demo = commands.add_parser("demo", help="exercise the shared investigation or tenant example")
    demo.add_argument("--scope", choices=("person", "tenant"), default="person")
    demo.add_argument("--output", type=Path, default=Path("investigation"))
    demo.add_argument("--example", choices=("shared", "legacy"))
    demo.add_argument("--mode", choices=("deletion", "late-write"))
    init = commands.add_parser("init", help="create an application starter or tenant observation policy")
    init.add_argument("--scope", choices=("person", "tenant"), default="person")
    init.add_argument("--output", type=Path, default=Path("setrya-erasure"))
    init.add_argument("--example", choices=("shared", "legacy"))
    run = commands.add_parser("run", help="execute a trusted local starter or evaluate a tenant gate")
    run.add_argument("--scope", choices=("person", "tenant"), default="person")
    run.add_argument("--starter", type=Path, help="trusted generated test file")
    run.add_argument("--mode", choices=("deletion", "late-write"))
    run.add_argument("--output", type=Path)
    replay = commands.add_parser("replay", help="recompute engineering or case-review outcomes")
    replay.add_argument("--scope", choices=("person", "tenant"), default="person")
    replay.add_argument("directory", nargs="?", type=Path)
    replay.add_argument("--case-review", action="store_true")
    signature.add_commands(commands, local_keys=True, aliases=True)

    # Tenant commands retain the native request, policy and exhibits as inputs.
    # Delegate their argument parsing so the frozen v1 contract stays authoritative.
    if argv[:1] in (["replay"], ["run"]):
        scope_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        scope_parser.add_argument("--scope", choices=("person", "tenant"))
        scope, rest = scope_parser.parse_known_args(argv[1:])
        if scope.scope == "tenant":
            from setrya.offboarding.cli import main as tenant_main

            return tenant_main([{"replay": "verify-receipt", "run": "gate"}[argv[0]], *rest],
                               command_prog=f"setrya erasure {argv[0]} --scope tenant")
    args = parser.parse_args(argv)
    try:
        if args.action in ("sign-receipt", "sign", "verify-signature", "verify"):
            args.offboarding_action = "sign-receipt" if args.action in ("sign", "sign-receipt") else "verify-signature"
            if args.scope == "tenant":
                return signature.run(args)
            if args.receipt.name != RECEIPT:
                raise InputError(f"Expected a bundle's {RECEIPT}.")
            return signature.run(args, receipt_limit=MAX_BUNDLE_FILE_BYTES,
                                 validate=lambda raw: validated(args.receipt.parent, raw))
        if args.action == "replay":
            if args.directory is None:
                raise InputError("Supply the directory containing the receipt and its evidence.")
            return replay_directory(args.directory, case_review=args.case_review)
        if args.action == "run":
            if args.output is None or args.starter is None:
                raise InputError("Supply --starter and a new --output directory.")
            if not args.starter.is_file() or os.path.lexists(args.output):
                raise InputError("Choose a trusted starter file and a fresh output directory.")
            selected_mode = args.mode or "deletion"
            code = subprocess.run([sys.executable, str(args.starter.resolve()), "--mode", selected_mode,
                                   "--output", str(args.output.resolve())], stdout=subprocess.DEVNULL).returncode
            if code in (2, 3) and not (args.output / RECEIPT).is_file():
                return code
            receipt = load_directory(args.output)
            recorded_mode = receipt["mode"] if receipt["version"] == 3 else "late-write"
            if code != receipt_exit(receipt) or recorded_mode != selected_mode:
                raise InputError("Starter exit or recorded mode differs from the requested drill.")
            return report(receipt)
        if args.scope == "tenant" and (args.example or getattr(args, "mode", None)):
            raise InputError("Use person scope to select a shared investigation mode.")
        if args.action == "demo":
            if args.scope == "tenant":
                from setrya.offboarding.cli import main as tenant_main

                return tenant_main(["demo", "--output", str(args.output)])
            if args.example == "legacy":
                if args.mode == "deletion":
                    raise InputError("The v2 example uses a held worker.")
                from .demo import run_demo

                return run_demo(args.output)
            from .investigation import run_demo

            return run_demo(args.output, mode=args.mode or "late-write")
        if args.scope == "tenant":
            from importlib.resources import files

            args.output.mkdir(parents=True, exist_ok=False)
            policy = args.output / "policy.json"
            policy.write_bytes(files("setrya.examples.offboarding.sqlite").joinpath("policy.json").read_bytes())
            print(f"Created {policy}. Connect your tenant observer, then run erasure run --scope tenant --help.")
        else:
            from .starter import init_erasure

            init_erasure(args.output, shared=args.example != "legacy")
            print(f"Created {args.output}. Connect the disposable application fixture, then run")
            print(shlex.join(["python", "-m", "pytest", "-q", "-s", "--", str(args.output / "test_erasure.py")]))
        return 0
    except (ValueError, OSError, RecursionError) as exc:
        detail = str(exc) if isinstance(exc, (InputError, InvalidReport)) else type(exc).__name__
        print(f"ERROR  {detail}", file=sys.stderr)
        return 2
