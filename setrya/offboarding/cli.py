"""CLI for tenant offboarding CI gates, deletion receipts and reference trials."""

import argparse
from importlib.resources import files
from pathlib import Path
import shlex
import sys

from setrya import __version__

from . import gate, receipt_cli, signature


class OffboardingParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"Invalid arguments. Run '{self.prog} --help' for usage.\n")


def main(argv, *, invocation=("setrya", "offboarding")):
    argv = list(argv)
    prog = shlex.join(invocation)
    if argv[:1] == ["drill"]:
        from setrya.scenarios import main as scenario_main

        return scenario_main(["delete", *argv[1:]])
    if argv[:1] == ["observe-sqlite"]:
        from setrya.examples.offboarding.sqlite.observe import main as observe

        return observe(argv[1:], prog=f"{prog} observe-sqlite")

    parser = OffboardingParser(prog=prog, description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="offboarding_action", required=True)
    demo = commands.add_parser("demo", help="run a synthetic offboarding and replay its evidence")
    demo.add_argument("--output", type=Path, required=True, help="a new directory for the example evidence")
    commands.add_parser("drill", help="drive a held worker through tenant deletion")
    gate.add_command(commands)
    receipt_cli.add_commands(commands)
    signature.add_commands(commands)
    reference_demo = commands.add_parser("reference-demo", help="write a timestamped worker reference report")
    reference_demo.add_argument("--output", type=Path, default=Path("artifacts/demo-report.json"))
    validate = commands.add_parser("validate", help="recompute reference report verdicts offline")
    validate.add_argument("report", type=Path)
    commands.add_parser("observe-sqlite", help="observe the example SQLite ticket and answer-cache deletion job")
    commands.add_parser("sqlite-policy", help="print the example SQLite observer policy as JSON")
    if argv[:1] == ["opendesk"]:
        from setrya.examples.opendesk import cli as opendesk

        opendesk.add_command(commands)
    else:
        commands.add_parser("opendesk", help="run or replay the pinned OpenDesk trial")
    args = parser.parse_args(argv)

    if args.offboarding_action == "gate":
        return gate.run(args)
    if args.offboarding_action in {"sign-receipt", "verify-signature"}:
        return signature.run(args)
    if args.offboarding_action in receipt_cli.COMMANDS:
        return receipt_cli.run(args)
    if args.offboarding_action == "opendesk":
        return opendesk.run(args)
    if args.offboarding_action == "sqlite-policy":
        sys.stdout.write(files("setrya.examples.offboarding.sqlite").joinpath("policy.json").read_text(encoding="utf-8"))
        return 0
    if args.offboarding_action in {"reference-demo", "validate"}:
        from . import reference

        if args.offboarding_action == "validate":
            return reference.verify(args.report)
        return reference.demo(args.output)

    from setrya.examples.offboarding.demo import run

    return run(args.output, invocation=invocation)
