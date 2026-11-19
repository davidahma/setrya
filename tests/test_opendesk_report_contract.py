import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from examples.opendesk import run
from examples.opendesk.verify_report import TARGET, validate


class UpstreamReportTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads((Path(__file__).resolve().parents[1] / "examples" / "opendesk" / "observed.json").read_text())
        self.before, self.after = [
            item["workspaces"] for item in self.report["observations"]
        ]

    def test_recorded_report(self):
        validate(self.report)

    def test_replay_accepts_only_recorded_target_combinations(self):
        previous = self.report["target"]
        self.report["target"] = TARGET
        with self.assertRaises(ValueError):
            validate(self.report)  # The new image only ran the v2 observer.
        self.report.update(report_version=2, observations=[], interrupted_at="startup",
                           offboarding_http_status=None, outcome="INCONCLUSIVE")
        for target in (previous, TARGET):
            self.report["target"] = target
            validate(self.report)
        for field in ("image", "commit", "version"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.report["target"] = {**previous, field: TARGET[field]}
                validate(self.report)

    def test_denied_request_cannot_be_called_absent(self):
        self.after["Acme"]["agent_summary"] = "NOT_RETURNED"
        with self.assertRaises(ValueError):
            validate(self.report)
        self.after["Acme"]["agent_http_status"] = 200
        validate(self.report)

    def test_residual_takes_precedence_over_unhealthy_control(self):
        self.after["Birch"]["source_message_row"] = "ABSENT_BY_ID"
        with self.assertRaises(ValueError):
            validate(self.report)
        self.report["outcome"] = "INCONCLUSIVE"
        validate(self.report)
        self.after["Acme"]["stored_ticket_summary"] = "FOUND"
        with self.assertRaises(ValueError):
            validate(self.report)
        self.report["outcome"] = "FAIL"
        validate(self.report)

    def test_incomplete_baseline_must_stop_before_offboarding(self):
        self.before["Acme"]["source_message_row"] = "ABSENT_BY_ID"
        self.report["outcome"] = "INCONCLUSIVE"
        with self.assertRaises(ValueError):
            validate(self.report)
        self.report["observations"].pop()
        self.report["interrupted_at"] = "baseline"
        with self.assertRaises(ValueError):
            validate(self.report)
        self.report["offboarding_http_status"] = None
        validate(self.report)

    def test_no_free_text_in_nested_results(self):
        self.after["Acme"]["answer"] = "synthetic answer"
        with self.assertRaises(ValueError):
            validate(self.report)
        del self.after["Acme"]["answer"]
        self.after["Acme"]["stored_ticket_summary"] = "synthetic answer"
        with self.assertRaises(ValueError):
            validate(self.report)

    def test_setup_failure_replaces_old_success_evidence_and_exits_unsuccessfully(self):
        def failed_start(*args):
            if args[0] == "up":
                raise OSError("raw diagnostic excluded from report")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "opendesk.json"
            output.write_text(json.dumps(self.report))
            with (
                patch("sys.argv", ["run.py", "--report", str(output)]),
                patch.object(run.Stack, "compose", side_effect=failed_start),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertNotEqual(run.main(), 0)
            saved = json.loads(output.read_text())
            validate(saved)
            self.assertEqual(saved["observations"], [])
            self.assertEqual(saved["outcome"], "INCONCLUSIVE")
            self.assertEqual(saved["interrupted_at"], "startup")
            self.assertNotIn("raw diagnostic", output.read_text())
