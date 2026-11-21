from dataclasses import replace
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic_ns
import unittest
from unittest.mock import Mock, patch

from setrya.cli import EXAMPLE
from setrya.drill import Drill
from setrya.runner import run
from setrya.status import Status
from setrya.examples.support_search.adapter import SupportAdapter
from setrya.examples.support_search.client import Client, FixtureError


class FixtureTeardownTests(unittest.TestCase):
    def test_server_reports_cleanup_failure(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "cleanup_case.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "from setrya.examples.support_search.__main__ import serve\n"
                "class Fixture:\n"
                "    def close(self):\n"
                "        raise RuntimeError('cleanup failed')\n"
                "serve(Fixture(), Path(sys.argv[2]), sys.argv[3])\n"
            )
            pythonpath = os.pathsep.join((folder, str(Path(__file__).resolve().parents[1])))
            with patch.dict(os.environ, PYTHONPATH=pythonpath):
                client = Client(directory, "run", "fixed", "refund", module="cleanup_case")
                with self.assertRaisesRegex(FixtureError, "cleanup failed"):
                    client.close()

    def test_forced_shutdown_is_reported(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 3), -9]
        client = Client.__new__(Client)
        client.process = process
        with self.assertRaisesRegex(FixtureError, "forced shutdown"):
            client.close()
        process.kill.assert_called_once()

    def test_cleanup_failure_preserves_startup_error(self):
        with TemporaryDirectory() as folder, patch("setrya.examples.support_search.client.subprocess.Popen") as spawn:
            spawn.return_value.poll.return_value = 1
            spawn.return_value.wait.return_value = 1
            with self.assertRaisesRegex(FixtureError, "did not start") as raised:
                Client(Path(folder), "run", "fixed", "refund")
        self.assertIn("cleanup also failed", raised.exception.__notes__[0])


class FixtureComparisonTests(unittest.TestCase):
    def test_receipt_first_schedule_requires_an_observed_receipt_before_final_reads(self):
        drill = Drill.load(EXAMPLE / "python.toml")
        drill = replace(drill, rules=replace(drill.rules, settle_ms=500))
        for timing, expects_reads in (("during-settle", True), ("after-receipt", False)):
            with self.subTest(timing=timing), patch.object(SupportAdapter, "receipt", return_value=None):
                trace = run(replace(drill, options={"mode": "fixed", "final_reads": timing}))
                self.assertIs(trace.verdict.status, Status.NOT_TESTED)
                self.assertTrue(any(event["kind"] == "released" for event in trace.report.events))
                self.assertEqual(any(event.get("stage") == "final" for event in trace.report.events), expects_reads)
        complete = run(replace(drill, options={"mode": "fixed", "final_reads": "after-receipt"}))
        self.assertIs(complete.verdict.status, Status.PASS)
        receipt = next(e["seq"] for e in complete.report.events if e["kind"] == "receipt")
        self.assertTrue(all(e["seq"] > receipt for e in complete.report.events if e.get("stage") == "final"))

    def test_wrong_or_late_receipt_cannot_start_the_selected_final_reads(self):
        original = SupportAdapter.receipt
        drill = Drill.load(EXAMPLE / "python.toml")
        drill = replace(drill, rules=replace(drill.rules, checkpoint_ms=10_000, freshness_ms=5_000, settle_ms=5_000),
                        options={"mode": "broken", "final_reads": "after-receipt"})
        for mode in ("wrong-attempt", "late"):
            offset_ns = 0

            def observed(app):
                nonlocal offset_ns
                receipt = original(app)
                if receipt is not None:
                    if mode == "late":
                        # Deliver the real receipt beyond the deadline without
                        # depending on the CI host's process scheduling.
                        offset_ns = (drill.rules.settle_ms + 1) * 1_000_000
                    else:
                        return replace(receipt, attempt="other")
                return receipt

            with (self.subTest(mode=mode), patch.object(SupportAdapter, "receipt", observed),
                  patch("setrya.recorder.monotonic_ns", lambda: monotonic_ns() + offset_ns)):
                result = run(drill)
                self.assertIs(result.verdict.status, Status.NOT_TESTED)
                self.assertTrue(any(e["kind"] == "receipt" for e in result.report.events))
                self.assertFalse(any(e.get("stage") == "final" for e in result.report.events))
