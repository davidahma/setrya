"""Test subjects run in separate pytest processes by test_fixture.py."""

import os
from pathlib import Path

import pytest

from setrya import __file__ as package_file
from setrya.adapter import Checkpoint, Publication, Receipt, SearchResult
from setrya.drill import InputError


DRILL = Path(package_file).resolve().parents[1] / "setrya" / "examples" / "recorded-search" / "drill.toml"


def observe(recorder, probe_id, marker):
    def search(probe):
        return SearchResult((marker,), route=probe.rules.route, scope=probe.rules.scope)

    return recorder.search(probe_id, search)


def run_update(recorder, *, final="refund@B", receipt=True):
    recorder.seeded()
    observe(recorder, "refund-window", "refund@A")
    observe(recorder, "delivery", "delivery@1")
    recorder.started("attempt")
    recorder.held(Checkpoint(recorder.run_id, "attempt", "refund", "A", "after_source_read"))
    recorder.published(Publication(recorder.run_id, "refund", "B"))
    observe(recorder, "refund-window", "refund@B")
    recorder.released("attempt")
    if receipt:
        recorder.receipt(Receipt(recorder.run_id, "attempt", "refund", "A", "rejected"))
    observe(recorder, "refund-window", final)
    observe(recorder, "delivery", "delivery@1")


@pytest.mark.parametrize("revision", ["a" * 40, "b" * 40], ids=["private-account-one", "private-account-two"])
def test_clean_update(setrya_record, revision):
    with setrya_record(DRILL, revision=revision) as recorder:
        run_update(recorder)


def test_stale_update(setrya_record):
    with setrya_record(DRILL, report=os.environ["SETRYA_TEST_REPORT"], test_id="refund-update") as recorder:
        run_update(recorder, final="refund@A")


def test_missing_receipt(setrya_record):
    with setrya_record(DRILL) as recorder:
        run_update(recorder, receipt=False)


def test_native_failure(setrya_record):
    with setrya_record(DRILL) as recorder:
        run_update(recorder, final="refund@A")
        raise AssertionError("original native assertion")



def test_check_protects_drill_before_running(setrya_check, tmp_path, monkeypatch):
    def unexpected_run(*args):
        pytest.fail("adapter must not run when the output aliases the drill")

    monkeypatch.setattr("setrya.pytest_plugin.run", unexpected_run)
    drill = tmp_path / "drill.toml"
    original = DRILL.read_bytes()
    drill.write_bytes(original)
    alias = tmp_path / "alias"
    alias.symlink_to(drill)
    for output in (drill, alias):
        with pytest.raises(InputError, match="distinct files"):
            setrya_check(drill, report=output)
    assert drill.read_bytes() == original
