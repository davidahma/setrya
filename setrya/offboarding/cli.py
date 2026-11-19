"""CLI for tenant offboarding CI gates and deletion receipts."""

import argparse
from pathlib import Path

from . import gate, receipt_cli


class OffboardingParser(argparse.ArgumentParser):
    def error(self, message):
        # Exit 2 is a valid inconclusive gate or an open receipt.
        self.exit(3, f"Invalid arguments. Run '{self.prog} --help' for usage.\n")


def main(argv):
    parser = OffboardingParser(prog="setrya offboarding", description=__doc__)
    commands = parser.add_subparsers(dest="offboarding_action", required=True)
    demo = commands.add_parser("demo", help="run a synthetic offboarding and replay its evidence")
    demo.add_argument("--output", type=Path, required=True, help="a new directory for the example evidence")
    gate.add_command(commands)
    receipt_cli.add_commands(commands)
    args = parser.parse_args(argv)

    if args.offboarding_action == "gate":
        return gate.run(args)
    if args.offboarding_action in receipt_cli.COMMANDS:
        return receipt_cli.run(args)

    from examples.offboarding.demo import run

    return run(args.output)
