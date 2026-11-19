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

from examples.opendesk import run
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

    def test_ctrl_c_keeps_observed_residual_and_reports_startup_or_cleanup_gaps(self):
        for phase, residual, expected in (("observation", True, 1), ("startup", False, 3),
                                          ("cleanup", True, 1), ("cleanup", False, 3)):
            with self.subTest(phase=phase, residual=residual), TemporaryDirectory() as folder:
                destination = Path(folder, "trial")

                def interrupt_at_checkpoint(stack, report):
                    if phase == "startup":
                        raise KeyboardInterrupt
                    before = {label: run.HEALTHY_V2.copy() for label in run.CASES}
                    after = {label: run.UNTESTED.copy() for label in run.CASES}
                    if residual:
                        after["Acme"]["source_message_row"] = "FOUND"
                    report.update(interrupted_at="observation", offboarding_http_status=204)
                    for checkpoint, rows in (("before_offboarding", before), ("after_offboarding", after)):
                        report["observations"].append({
                            "phase": checkpoint, "observed_at": run.now(), "workspaces": rows,
                        })
                    if phase == "observation":
                        raise KeyboardInterrupt
                    report["interrupted_at"] = None

                with (patch.object(run, "run", side_effect=interrupt_at_checkpoint),
                      patch.object(run.Stack, "compose", side_effect=KeyboardInterrupt if phase == "cleanup" else None) as cleanup):
                    self.assertEqual(self.invoke("--output", destination), expected)
                cleanup.assert_called_once_with("down", "--volumes", "--remove-orphans")
                report = destination / "opendesk.json"
                self.assertEqual(self.invoke("--verify-report", report), 0)
                self.assertEqual(json.loads(report.read_bytes())["interrupted_at"], phase)
                decision = json.loads((destination / "opendesk-decision.json").read_bytes())
                self.assertEqual(decision["verdict"], "FAIL" if residual else "INCONCLUSIVE")
                self.assertEqual(self.invoke("--from-report", report, "--output", Path(folder, "review")), expected)

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
