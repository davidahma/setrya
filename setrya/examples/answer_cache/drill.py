"""Run or replay the late answer-cache fill on an authenticated answer endpoint."""

import argparse
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import time

from setrya.adapter import Checkpoint, Publication
from setrya.drill import InputError
from setrya.offboarding.inputs import InvalidReport, save
from setrya.recorder import Recorder

from .contract import CHECKPOINT
from .evidence import FORMAT, JOURNAL, MANIFEST, TEST_ID, drill, observation, replay, sample, verified_capture, worker_capture


def _wait(read, seconds):
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if value is not None:
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.05, remaining))


def _exercise(app, recorder, spec, manifest, wait_seconds):
    def read_all(stage):
        results = {}
        for configured in spec.probes:
            def read(probe):
                row = sample(probe.rules.id, stage, app.answer(probe.rules.scope, probe.question),
                             manifest["worker_capture"])
                value = observation(row)
                manifest["observations"].append(row)
                return value

            result = recorder.search(configured.rules.id, read)
            rules = configured.rules
            required = rules.before if stage == "baseline" else rules.require
            results[rules.id] = ((result.route, result.scope) == (rules.route, rules.scope)
                                 and None not in result.passages and set(required) <= set(result.passages)
                                 and (stage == "baseline" or set(rules.forbid).isdisjoint(result.passages)))
        return all(results.values())

    app.seed()
    recorder.seeded()
    if not read_all("baseline"):
        return
    attempt = app.start_old()
    recorder.started(attempt)
    held = _wait(app.checkpoint, wait_seconds)
    if held is None:
        return
    recorder.held(held)
    rules = spec.rules
    if held != Checkpoint(recorder.run_id, attempt, rules.document, rules.old_revision, CHECKPOINT):
        return
    manifest["worker_capture"] = worker_capture(app.captured())
    publication = app.revoke()
    recorder.published(publication)
    if publication != Publication(recorder.run_id, rules.document, rules.new_revision):
        return
    if (not read_all("freshness") or app.checkpoint() != held
            or not verified_capture(manifest["worker_capture"], recorder.run_id, attempt)):
        return
    manifest["held_after_reads"] = asdict(held)
    app.release()
    recorder.released(attempt)
    receipt = _wait(app.receipt, wait_seconds)
    if receipt is not None:
        recorder.receipt(receipt)
    read_all("final")


def run(directory, mode, *, application=None, wait_seconds=30):
    if application is None:
        try:
            from .app import Application as application
        except ModuleNotFoundError:
            raise InputError("install the answer-cache extra to run the live drill") from None
    if mode not in ("broken", "guarded"):
        raise InputError("unknown answer-cache mode")
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=False)
    spec = drill()
    recorder = Recorder(spec, test_id=TEST_ID, report=out / JOURNAL)
    manifest = {"format": FORMAT, "version": 1, "run_id": recorder.run_id, "mode": mode,
                "worker_capture": None, "held_after_reads": None, "observations": []}
    try:
        with recorder, TemporaryDirectory(prefix="setrya-answer-cache-") as work:
            with application(recorder.run_id, mode, Path(work)) as app:
                _exercise(app, recorder, spec, manifest, wait_seconds)
    except (Exception, KeyboardInterrupt):
        # Recorder saved the observed prefix with the error type. The replayed
        # checker preserves a conclusive failure that preceded cleanup errors.
        if not (out / JOURNAL).is_file():
            raise
    manifest["report_sha256"] = sha256((out / JOURNAL).read_bytes()).hexdigest()
    save(manifest, out / MANIFEST)
    return replay(out)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setrya answer-cache", description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="save a live run in a new directory")
    action.add_argument("--replay", type=Path, help="recompute a saved decision offline")
    parser.add_argument("--mode", choices=("guarded", "broken"), help="live worker policy (default: guarded)")
    args = parser.parse_args(argv)
    if args.replay and args.mode is not None:
        parser.error("--mode selects a live application policy")
    try:
        verdict, text = replay(args.replay) if args.replay else run(args.output, args.mode or "guarded")
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
