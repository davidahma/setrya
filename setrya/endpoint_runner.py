"""Drive the endpoint race and persist its replayable evidence."""
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import shutil
import time

from .adapter import Checkpoint, Publication
from .drill import InputError
from .endpoint import MissingHook
from .git_refs import resolve_commit
from .endpoint_evidence import (
    BOUND_ENDPOINT_VERSION, ENDPOINT_VERSION, FIXTURE, FORMAT, JOURNAL, MANIFEST, TEST_ID, VERSION, drill,
    observation, replay, sample, verified_capture, worker_capture,
)
from .offboarding.inputs import save
from .recorder import Recorder


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


def _exercise(app, recorder, spec, manifest, wait_seconds, case, contract):
    def call(name, *args):
        # Keep the operation active while its return value enters the journal.
        if contract is not FIXTURE:
            manifest["error_hook"] = name
        method = getattr(app, name, None)
        if method is None:
            raise MissingHook(name)
        return method(*args)

    def read_all(stage):
        results = {}
        for configured in spec.probes:
            row = None

            def read(probe):
                nonlocal row
                row = sample(probe.rules.id, stage, call("answer", probe.rules.scope, probe.question),
                             manifest["worker_capture"], case, contract)
                return observation(row, contract)

            result = recorder.search(configured.rules.id, read)
            manifest["observations"].append(row)
            rules = configured.rules
            required = rules.before if stage == "baseline" else rules.require
            results[rules.id] = ((result.route, result.scope) == (rules.route, rules.scope)
                                 and None not in result.passages and set(required) <= set(result.passages)
                                 and (stage == "baseline" or set(rules.forbid).isdisjoint(result.passages)))
            if contract is not FIXTURE and not results[rules.id] and stage == "freshness":
                return False
        return all(results.values())

    call("seed")
    recorder.seeded()
    if not read_all("baseline"):
        return
    attempt = call("start_old")
    recorder.started(attempt)
    held = _wait(lambda: call("checkpoint"), wait_seconds)
    if held is None:
        return
    recorder.held(held)
    rules = spec.rules
    if held != Checkpoint(recorder.run_id, attempt, rules.document, rules.old_revision, rules.checkpoint):
        return
    if call("receipt") is not None:
        manifest["receipt_before_release"] = True
        return
    manifest["worker_capture"] = worker_capture(call("captured"), case, contract)
    if contract is not FIXTURE and not verified_capture(manifest["worker_capture"], recorder.run_id, attempt, contract):
        return
    publication = call("revoke")
    recorder.published(publication)
    if publication != Publication(recorder.run_id, rules.document, rules.new_revision):
        return
    if (not read_all("freshness") or call("checkpoint") != held
            or not verified_capture(manifest["worker_capture"], recorder.run_id, attempt, contract)):
        return
    manifest["held_after_reads"] = asdict(held)
    manifest["receipt_before_release"] = call("receipt") is not None
    if manifest["receipt_before_release"]:
        return
    call("release")
    recorder.released(attempt)
    receipt = _wait(lambda: call("receipt"), wait_seconds)
    if receipt is not None:
        recorder.receipt(receipt)
    read_all("final")


def run_endpoint(case, application, directory, wait_seconds):
    contract = case._contract()
    if os.environ.get("SETRYA_RUN_PR") != "endpoint":
        return record_endpoint(directory, "application", case, contract, application, wait_seconds)

    # Keep the native test's own output path. Publish only this live call's
    # manifest and journal to the parent, never the whole pytest temp directory.
    stage = Path(os.environ["SETRYA_REVIEW_ENDPOINT"])
    revision = resolve_commit(Path.cwd(), "HEAD")
    if revision != os.environ["SETRYA_REVIEW_REVISION"]:
        raise InputError("the endpoint test must run in the selected application checkout")
    try:
        stage.mkdir()
    except FileExistsError:
        stage.with_suffix(".ambiguous").touch()
        raise InputError("select a single endpoint test for the PR check") from None
    result = record_endpoint(directory, "application", case, contract, application, wait_seconds, revision=revision)
    for name in (MANIFEST, JOURNAL):
        shutil.copyfile(Path(directory) / name, stage / name)
    stage.with_suffix(".executed").write_text(sha256((stage / MANIFEST).read_bytes()).hexdigest(), encoding="ascii")
    return result


def record_endpoint(directory, mode, case, contract, application, wait_seconds, *, revision=None):
    out = Path(directory).absolute()
    out.mkdir(parents=True, exist_ok=False)
    spec = drill(contract, case.question)
    test_id = TEST_ID if contract is FIXTURE else contract.test_id()
    recorder = Recorder(spec, test_id=test_id, report=out / JOURNAL, revision=revision)
    manifest = {"format": FORMAT, "version": VERSION, "run_id": recorder.run_id, "mode": mode,
                "worker_capture": None, "held_after_reads": None, "receipt_before_release": None, "observations": []}
    if contract is not FIXTURE:
        manifest.update(version=ENDPOINT_VERSION, case=asdict(contract), missing_hook=None, error_hook=None)
        if revision is not None:
            manifest.update(version=BOUND_ENDPOINT_VERSION, revision=revision)
    body_error = None
    interrupted = None
    try:
        with recorder:
            with application(recorder.run_id) as app:
                try:
                    _exercise(app, recorder, spec, manifest, wait_seconds, case, contract)
                except (Exception, KeyboardInterrupt) as error:
                    body_error = error
                    raise
                if contract is not FIXTURE:
                    manifest["error_hook"] = None
            # An application's cleanup can suppress the hook exception. Preserve
            # it for the journal even in that case.
            if body_error is not None:
                raise body_error
    except (Exception, KeyboardInterrupt) as error:
        # Recorder retained the observed prefix and only the exception's type.
        if not (out / JOURNAL).is_file():
            raise
        if contract is not FIXTURE:
            manifest["error_hook"] = manifest["error_hook"] if error is body_error else "application"
            if isinstance(error, MissingHook):
                manifest["missing_hook"] = error.hook
            if isinstance(error, KeyboardInterrupt):
                interrupted = error
    manifest["report_sha256"] = sha256((out / JOURNAL).read_bytes()).hexdigest()
    save(manifest, out / MANIFEST)
    if interrupted is not None:
        raise interrupted
    return replay(out)
