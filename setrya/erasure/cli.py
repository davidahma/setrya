"""Verify and sign a subject erasure receipt with Cosign."""
import argparse
from pathlib import Path

from setrya.offboarding import signature
from .receipt import EXIT, MAX_BUNDLE_FILE_BYTES, RECEIPT, replay_bytes


def report(receipt):
    print(f"{receipt['verdict']}  {receipt['unknown_copies']} unknown copies  "
          f"{receipt['time_to_first_verdict_ms']} ms to first verdict")
    for copy in receipt["copies"]:
        print(f"{copy['status']}  {copy['store']}  {copy['key']}")
    for reason in receipt["failures"] + receipt["gaps"]:
        print(reason)
    for error in receipt["errors"]:
        print(error["operation"], error["error"])
    return EXIT[receipt["verdict"]]


def main(argv):
    parser = argparse.ArgumentParser(prog="setrya erasure")
    commands = parser.add_subparsers(dest="offboarding_action", required=True)
    signature.add_commands(commands, local_keys=True)
    args = parser.parse_args(argv)
    if args.receipt.name != RECEIPT:
        parser.error(f"expected a bundle's {RECEIPT}")
    return signature.run(args, receipt_limit=MAX_BUNDLE_FILE_BYTES, validate=lambda raw: replay_bytes(Path(args.receipt).parent, raw))
