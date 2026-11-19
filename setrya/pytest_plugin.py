"""Optional pytest fixtures. Enable with -p setrya.pytest_plugin."""

from contextlib import contextmanager
from hashlib import sha256
from itertools import count
import os
from pathlib import Path

import pytest

from .checker import Status
from .drill import Drill, InputError
from .recorder import Recorder
from .runner import run


def _pr_context(drill, report, revision):
    if os.environ.get("SETRYA_RUN_PR") != "1":
        if drill is None:
            raise InputError("a drill is required outside setrya run-pr")
        return drill, report, revision
    source = Path(os.environ["SETRYA_REVIEW_DRILL"])
    target = Path(os.environ["SETRYA_REVIEW_REPORT"])
    commit = os.environ["SETRYA_REVIEW_REVISION"]
    # A broad selector, retry plugin or parallel worker can call this fixture
    # twice. A persistent marker vetoes a green handoff even if pytest catches
    # the exception or a later test would otherwise replace the first report.
    try:
        target.with_suffix(".claimed").touch(exist_ok=False)
    except FileExistsError:
        target.with_suffix(".ambiguous").touch()
        raise InputError("run-pr needs exactly one Setrya recording per command") from None
    if ((report is not None and Path(report).resolve() != target)
            or (revision is not None and revision != commit)):
        raise InputError("the fixture report path or revision differs from the run-pr lane")
    if drill is not None:
        requested = drill if isinstance(drill, Drill) else Drill.load(drill)
        if requested != Drill.load(source):
            raise InputError("the fixture drill differs from --drill")
    return source, target, commit


@pytest.fixture
def setrya_check():
    def check(path=None, report=None):
        path, report, revision = _pr_context(path, report, None)
        destination = Path(report).absolute() if report is not None else None
        if destination is not None and not isinstance(path, Drill) and Path(path).resolve() == destination.resolve():
            raise InputError("drill and check report must use distinct files")
        result = run(path, revision=revision)
        if destination is not None:
            result.report.save(destination)
        if result.verdict.status is not Status.PASS:
            pytest.fail(f"{result.verdict.status.label} · {result.verdict.detail}", pytrace=False)
        return result

    return check


@pytest.fixture
def setrya_record(request, tmp_path):
    """Record an existing test and require conclusive passing search evidence."""
    default_id = "pytest-" + sha256(request.node.nodeid.encode("utf-8")).hexdigest()
    recording_numbers = count(1)

    @contextmanager
    def record(drill=None, *, report=None, test_id=None, revision=None, run_id=None):
        drill, report, revision = _pr_context(drill, report, revision)
        path = tmp_path / f"setrya-{next(recording_numbers)}.json" if report is None else Path(report).absolute()
        recorder = Recorder(drill, test_id=default_id if test_id is None else test_id,
                            report=path, revision=revision, run_id=run_id)
        try:
            with recorder:
                yield recorder
        except BaseException as error:
            error.add_note(f"Setrya report path: {path}")
            raise
        verdict = recorder.result.verdict
        if verdict.status is not Status.PASS:
            pytest.fail(f"{verdict.status.label} · {verdict.detail}\nSetrya report: {path}", pytrace=False)

    return record
