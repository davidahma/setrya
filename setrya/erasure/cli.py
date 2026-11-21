"""Run or review an erasure drill scoped to one person or a tenant."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

from setrya.drill import InputError
from setrya.offboarding import signature
from setrya.offboarding.inputs import InvalidReport
from .receipt import RECEIPT
from setrya.receipts import exit_code as receipt_exit, replay as load_directory, report, signature_command


def replay_directory(directory, *, case_review=False):
    return report(load_directory(directory), case_review=case_review)


def main(argv, *, prog="setrya erasure"):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    demo = commands.add_parser("demo", help="exercise the shared investigation or tenant example")
    demo.add_argument("--scope", choices=("person", "tenant"), default="person")
    demo.add_argument("--output", type=Path, default=Path("investigation"))
    demo.add_argument("--example", choices=("shared", "investigation"))
    demo.add_argument("--mode", choices=("deletion", "late-write"))
    init = commands.add_parser("init", help="create an application starter or tenant observation policy")
    init.add_argument("--scope", choices=("person", "tenant"), default="person")
    init.add_argument("--output", type=Path, default=Path("setrya-erasure"))
    init.add_argument("--example", choices=("shared", "investigation"))
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
            prog = f"{prog} {argv[0]} --scope tenant"
            if argv[0] == "replay" and "--request" in rest:
                from setrya.offboarding.cli import main as tenant_main
                return tenant_main(["verify-receipt", *rest], command_prog=prog)
            try:
                if argv[0] == "run":
                    from setrya.offboarding.tenant_v3 import command
                    return command(rest, prog=prog)
                tenant = argparse.ArgumentParser(prog=prog, description="Replay a request-bound tenant receipt.")
                tenant.add_argument("directory", type=Path)
                tenant.add_argument("--expect-request")
                tenant.add_argument("--expect-job")
                tenant.add_argument("--case-review", action="store_true")
                opts = tenant.parse_args(rest)
                receipt = load_directory(opts.directory, expected_request=opts.expect_request, expected_job=opts.expect_job)
                if receipt.get("scope") != "tenant":
                    raise InputError("Expected a tenant receipt.")
                return report(receipt, case_review=opts.case_review)
            except (ValueError, OSError, RecursionError) as error:
                detail = str(error) if isinstance(error, (InputError, InvalidReport)) else type(error).__name__
                print(f"ERROR  {detail}", file=sys.stderr)
                return 2
    args = parser.parse_args(argv)
    try:
        if args.action in ("sign-receipt", "sign", "verify-signature", "verify"):
            args.offboarding_action = "sign-receipt" if args.action in ("sign", "sign-receipt") else "verify-signature"
            return signature_command(args)
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
            # A trusted starter can import its own siblings and application modules
            # in the caller's checkout, including when Setrya was installed from PyPI.
            child_env = os.environ.copy()
            paths = [str(Path.cwd()), child_env.get("PYTHONPATH", "")]
            child_env["PYTHONPATH"] = os.pathsep.join(filter(None, paths))
            code = subprocess.run([sys.executable, str(args.starter.resolve()), "--mode", selected_mode,
                                   "--output", str(args.output.resolve())], stdout=subprocess.DEVNULL,
                                  env=child_env).returncode
            if code in (2, 3) and not (args.output / RECEIPT).is_file():
                return code
            receipt = load_directory(args.output)
            if receipt["version"] != 3:
                raise InputError("Update the starter to issue v3 receipts. Use replay for historical v2 evidence.")
            if (receipt.get("scope") != "person" or "profile" in receipt
                    or code != receipt_exit(receipt) or receipt.get("mode") != selected_mode):
                raise InputError("Starter exit or recorded mode differs from the requested drill.")
            return report(receipt)
        if args.scope == "tenant" and (args.example or getattr(args, "mode", None)):
            raise InputError("Use person scope to select a shared investigation mode.")
        if args.action == "demo":
            if args.scope == "tenant":
                from setrya.offboarding.cli import main as tenant_main

                return tenant_main(["demo", "--output", str(args.output)])
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

            init_erasure(args.output)
            print(f"Created {args.output}. Connect the disposable application fixture, then run")
            print(shlex.join(["python", "-m", "pytest", "-q", "-s", "--", str(args.output / "test_erasure.py")]))
        return 0
    except (ValueError, OSError, RecursionError) as exc:
        detail = str(exc) if isinstance(exc, (InputError, InvalidReport)) else type(exc).__name__
        print(f"ERROR  {detail}", file=sys.stderr)
        return 2
