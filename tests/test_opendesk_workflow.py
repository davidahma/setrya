"""Check the public OpenDesk report commands and their independent exit codes."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from importlib.resources import files
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.cli import main


class OpenDeskWorkflowTests(unittest.TestCase):
    def invoke(self, *argv):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return main(["offboarding", "opendesk", *map(str, argv)])

    def test_verify_and_replay_preserve_v1_bytes_without_a_live_application(self):
        report = files("examples.opendesk").joinpath("observed.json").read_bytes()
        with TemporaryDirectory() as folder, patch("examples.opendesk.run.collect") as collect:
            source = Path(folder, "v1.json")
            source.write_bytes(report)
            destination = Path(folder, "review")
            self.assertEqual(self.invoke("--verify-report", source), 0)
            self.assertEqual(self.invoke("--from-report", source, "--output", destination), 2)
            self.assertEqual((destination / "opendesk.json").read_bytes(), report)
            evidence = json.loads((destination / "opendesk-decision.json").read_bytes())
            self.assertEqual(evidence["run_id"], "sha256-" + sha256(report).hexdigest())
            self.assertEqual(evidence["verdict"], "INCONCLUSIVE")
            self.assertEqual(self.invoke("--from-report", source, "--output", destination), 3)
            collect.assert_not_called()

    def test_invalid_report_and_invalid_mode_leave_output_absent(self):
        with TemporaryDirectory() as folder:
            source = Path(folder, "bad.json")
            source.write_text("{}")
            destination = Path(folder, "result")
            self.assertEqual(self.invoke("--verify-report", source), 3)
            self.assertEqual(self.invoke("--from-report", source, "--output", destination), 3)
            self.assertFalse(destination.exists())
            self.assertEqual(self.invoke("--verify-report", source, "--output", destination), 3)
            self.assertEqual(self.invoke("--from-report", source), 3)

    def test_interrupted_startup_cannot_pass_even_when_report_replays(self):
        report = json.loads(files("examples.opendesk").joinpath("observed.json").read_bytes())
        report.update(report_version=2, observations=[], outcome="INCONCLUSIVE",
                      offboarding_http_status=None, interrupted_at="startup")
        with TemporaryDirectory() as folder:
            source = Path(folder, "interrupted.json")
            destination = Path(folder, "review")
            source.write_text(json.dumps(report))
            self.assertEqual(self.invoke("--verify-report", source), 0)
            self.assertEqual(self.invoke("--from-report", source, "--output", destination), 3)
            decision = json.loads((destination / "opendesk-decision.json").read_bytes())
            self.assertEqual(decision["verdict"], "INCONCLUSIVE")
