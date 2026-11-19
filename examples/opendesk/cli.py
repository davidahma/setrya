"""Exercise a pinned OpenDesk service or replay its recorded offboarding reads."""

import argparse
import hashlib
from importlib.resources import files
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from setrya.offboarding import gate
from setrya.offboarding.inputs import save

from .to_gate import convert
from .verify_report import parse_report, read_raw


def add_command(commands):
    parser = commands.add_parser("opendesk", help=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--from-report", type=Path, help="export a recorded v1 or v2 trial without Docker")
    modes.add_argument("--verify-report", type=Path, help="check the raw trial report without running a gate")
    parser.add_argument("--output", type=Path, help="new directory for the report, policy, observations and decision")


def run(args):
    if (args.output is None) != (args.verify_report is not None):
        print("Specify --output for a live run or --from-report, or use --verify-report on its own.", file=sys.stderr)
        return 3
    try:
        if args.verify_report is not None:
            parse_report(read_raw(args.verify_report))
            print("Upstream report matches its pinned, content-free contract.")
            return 0

        dest = args.output
        if dest.exists() or dest.is_symlink():
            print("Choose a new output directory for the OpenDesk trial.", file=sys.stderr)
            return 3
        dest.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=dest.parent) as scratch:
            work = Path(scratch) / "evidence"
            work.mkdir()
            saved = work / "opendesk.json"
            if args.from_report is None:
                from .run import collect

                collect(saved)
                raw = read_raw(saved)
            else:
                raw = read_raw(args.from_report)
                saved.write_bytes(raw)
            trial = parse_report(raw)
            print(f"Trial outcome {trial['outcome']}")
            packet = convert(trial, hashlib.sha256(raw).hexdigest())
            observations = work / "opendesk-observations.json"
            save(packet, observations)
            policy = work / "opendesk-policy.json"
            asset = "policy-v2.json" if trial["report_version"] == 2 else "policy.json"
            policy.write_bytes(files("examples.opendesk").joinpath(asset).read_bytes())
            code = gate.run(argparse.Namespace(
                policy=policy, observations=observations, output=work / "opendesk-decision.json",
                expect_run=packet["run_id"], expect_revision=trial["target"]["commit"],
            ))
            if code == 3:
                return code
            work.rename(dest)
        print(f"OpenDesk evidence saved to {dest}")
        if trial["interrupted_at"] in ("startup", "cleanup") and code != 1:
            return 3
        return code
    except (KeyError, TypeError, ValueError, OSError, RecursionError) as error:
        print(f"Could not read the OpenDesk report or save evidence ({type(error).__name__}).", file=sys.stderr)
        return 3
