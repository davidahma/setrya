"""Verify native pytest exit codes, original failures, and saved evidence."""

import os
from pathlib import Path
import subprocess
import sys

from setrya.checker import Status, evaluate
from setrya.evidence import Report


CASES = Path(__file__).with_name("recording_cases.py")


def run_pytest(cases, temp, **env):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "setrya.pytest_plugin", "-q", "--tb=short",
         "--basetemp", str(temp), *(f"{CASES}::{case}" for case in cases)],
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", **env},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
    )


def test_complete_parameterized_tests_pass_and_keep_distinct_private_identities(tmp_path):
    def run(lane):
        directory = tmp_path / lane
        result = run_pytest(["test_clean_update"], directory)
        assert result.returncode == 0, result.stdout
        paths = sorted(directory.rglob("setrya-*.json"))
        assert len(paths) == 2
        identities = {}
        for path in paths:
            assert "private-account" not in path.read_text()
            report = Report.load(path)
            assert evaluate(report).status is Status.PASS
            identities[report.inputs["revision"]] = report.inputs["adapter"]
        assert len(set(identities.values())) == 2
        return identities

    assert run("first") == run("second")


def test_stale_incomplete_and_native_failures_keep_their_reports(tmp_path):
    explicit = tmp_path / "stale.json"
    result = run_pytest(["test_stale_update", "test_missing_receipt", "test_native_failure"], tmp_path / "pytest",
                        SETRYA_TEST_REPORT=str(explicit))
    assert result.returncode == 1, result.stdout
    assert "3 failed" in result.stdout
    assert "FAIL ·" in result.stdout and "NOT TESTED ·" in result.stdout
    assert "AssertionError: original native assertion" in result.stdout
    assert evaluate(Report.load(explicit)).status is Status.FAIL
    paths = list((tmp_path / "pytest").rglob("setrya-*.json"))
    assert len(paths) == 2
    statuses = set()
    for path in (explicit, *paths):
        assert str(path) in result.stdout
        report = Report.load(path)
        statuses.add(evaluate(report).status)
        assert "original native assertion" not in path.read_text()
    assert statuses == {Status.FAIL, Status.NOT_TESTED}


def test_check_rejects_output_that_would_replace_the_drill(tmp_path):
    result = run_pytest(["test_check_protects_drill_before_running"], tmp_path / "pytest")
    assert result.returncode == 0, result.stdout


def test_run_pr_fixture_defaults_bind_one_recording_and_reject_a_second(tmp_path, monkeypatch):
    from setrya.drill import InputError
    from setrya.pytest_plugin import _pr_context
    import pytest

    source = Path(__file__).resolve().parents[2] / "setrya/examples/recorded-search/drill.toml"
    target = tmp_path / "run.json"
    monkeypatch.setenv("SETRYA_RUN_PR", "1")
    monkeypatch.setenv("SETRYA_REVIEW_DRILL", str(source))
    monkeypatch.setenv("SETRYA_REVIEW_REPORT", str(target))
    monkeypatch.setenv("SETRYA_REVIEW_REVISION", "a" * 40)
    assert _pr_context(None, None, None) == (source, target, "a" * 40)
    with pytest.raises(InputError, match="exactly one"):
        _pr_context(None, None, None)
    assert target.with_suffix(".ambiguous").is_file()
