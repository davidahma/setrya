"""Optional pytest fixtures. Enable with -p setrya.pytest_plugin."""

from contextlib import contextmanager
from hashlib import sha256
from itertools import count
from pathlib import Path

import pytest

from .checker import Status
from .recorder import Recorder
from .runner import run


@pytest.fixture
def setrya_check():
    def check(path, report=None):
        result = run(path)
        if report is not None:
            result.report.save(report)
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
    def record(drill, *, report=None, test_id=None, revision=None, run_id=None):
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
