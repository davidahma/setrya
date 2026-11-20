"""Run the bundled pgvector and Celery answer-cache application."""

import argparse
from contextlib import contextmanager
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory

from setrya.drill import InputError
from setrya.endpoint_evidence import FIXTURE, FIXTURE_CASE
from setrya.endpoint_runner import record_endpoint
from setrya.offboarding.inputs import InvalidReport


def run(directory, mode, *, application=None, wait_seconds=30):
    if application is None:
        try:
            from .app import Application as application
        except ModuleNotFoundError:
            raise InputError("install the answer-cache extra to run the live drill") from None
    if mode not in ("broken", "guarded"):
        raise InputError("unknown answer-cache mode")

    @contextmanager
    def open_app(run_id):
        with TemporaryDirectory(prefix="setrya-answer-cache-") as work:
            with application(run_id, mode, Path(work)) as app:
                yield app

    return record_endpoint(directory, mode, FIXTURE_CASE, FIXTURE, open_app, wait_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setrya answer-cache", description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="save a live run in a new directory")
    action.add_argument("--replay", type=Path, help="recompute a saved decision offline")
    parser.add_argument("--mode", choices=("guarded", "broken"), help="live worker policy (default: guarded)")
    parser.add_argument("--case", choices=("late-fill", "selective"), help="live drill (default: late-fill)")
    args = parser.parse_args(argv)
    if args.replay and (args.mode is not None or args.case is not None):
        parser.error("--case and --mode select a live drill")
    try:
        if args.replay:
            from setrya.cli import _replay_answers

            verdict, text = _replay_answers(args.replay)
        elif args.case == "selective":
            from . import selective

            verdict, text = selective.run(args.output, args.mode or "guarded")
        else:
            verdict, text = run(args.output, args.mode or "guarded")
    except FileExistsError:
        print("ERROR  Choose a new answer-cache output directory.", file=sys.stderr)
        return 2
    except (InputError, InvalidReport, OSError) as error:
        print("ERROR  " + (str(error) if isinstance(error, ValueError) else "Could not access the answer-cache bundle."),
              file=sys.stderr)
        return 2
    print(text)
    if args.output:
        print("Replay  setrya replay " + shlex.quote(str(args.output)))
    return verdict.status.exit_code
