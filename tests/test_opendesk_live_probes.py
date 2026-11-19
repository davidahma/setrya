import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from examples.opendesk import run
from examples.opendesk import to_gate
from examples.opendesk import transport
from examples.opendesk.verify_report import (
    HEALTHY_V2,
    MAX_INPUT_BYTES,
    METADATA,
    UNTESTED,
    outcome,
    validate,
)

from setrya.offboarding import gate

HERE = Path(__file__).resolve().parents[1] / "examples" / "opendesk"
MARKER = run.CASES["Acme"][0]
TICKET = str(uuid4())


class LiveProbeTests(unittest.TestCase):
    def test_changed_compose_target_stops_before_container_start(self):
        stack = Mock()
        stack.compose.return_value = json.dumps(
            {"services": {"app": {"image": "ghcr.io/useopendesk/opendesk:other"}}}
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
            run.run(stack, {})
        stack.compose.assert_called_once_with(
            "config", "--no-interpolate", "--format", "json", capture=True
        )

    def test_transport_sends_one_request_to_the_designated_service(self):
        call = {
            "method": "GET",
            "path": "/tickets/example",
            "workspace": "retained",
            "token": "synthetic",
            "body": None,
        }
        for status in (200, 302, 307):
            with (
                self.subTest(status=status),
                patch.object(transport, "HTTPConnection") as connect,
                patch("sys.stdin", io.StringIO(json.dumps(call))),
                redirect_stdout(io.StringIO()) as output,
            ):
                response = connect.return_value.getresponse.return_value
                response.status = status
                response.read.return_value = b'{"id":"example"}'
                transport.main()
                connect.assert_called_once_with("app", 8080, timeout=30)
                connect.return_value.request.assert_called_once_with(
                    "GET",
                    "/v1/tickets/example",
                    body=None,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer synthetic",
                        "OD-Workspace": "retained",
                    },
                )
                self.assertEqual(
                    json.loads(output.getvalue()),
                    [status, {"id": "example"} if status == 200 else {}],
                )

    def test_docker_commands_have_separate_startup_probe_and_cleanup_deadlines(self):
        stack = run.Stack()
        with patch.object(subprocess, "run") as execute:
            for action in ("up", "exec", "down"):
                stack.compose(action)
            self.assertEqual(
                [call.kwargs["timeout"] for call in execute.call_args_list],
                [900, 60, 90],
            )

    def test_timeout_during_startup_and_cleanup_writes_interrupted_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            with (
                patch("sys.argv", ["run.py", "--report", str(output)]),
                patch.object(
                    subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired("docker", 90),
                ) as execute,
                redirect_stderr(io.StringIO()) as stderr,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(run.main(), 1)
            self.assertEqual(execute.call_count, 2)
            report = json.loads(output.read_bytes())
            validate(report)
            self.assertEqual(report["interrupted_at"], "cleanup")
            self.assertEqual(report["outcome"], "INCONCLUSIVE")
            self.assertIn("setrya-od-", stderr.getvalue())

    def test_identity_and_summary_shape_are_required_on_http_200(self):
        marker_text = "Preface: use the copper-compass desk for this account."
        for body, expected in (
            (
                {"id": str(uuid4()), "custom": {"ai_summary": {"text": marker_text}}},
                "INCONCLUSIVE",
            ),
            ({"error": "unknown"}, "INCONCLUSIVE"),
            ({"id": TICKET}, "INCONCLUSIVE"),
            ({"id": TICKET, "custom": None}, "INCONCLUSIVE"),
            ({"id": TICKET, "custom": {}}, "NOT_RETURNED"),
            ({"id": TICKET, "custom": {"ai_summary": None}}, "NOT_RETURNED"),
            (
                {"id": TICKET, "custom": {"ai_summary": {"text": marker_text}}},
                "RETURNED",
            ),
            (
                {"id": TICKET, "custom": {"ai_summary": {"text": "other text"}}},
                "INCONCLUSIVE",
            ),
        ):
            with self.subTest(body=body):
                self.assertEqual(run.agent_read(200, body, TICKET, MARKER)[0], expected)
        self.assertEqual(
            run.agent_read(400, {"code": "workspace_required"}, TICKET, MARKER),
            ("INCONCLUSIVE", 400, "workspace_required"),
        )
        self.assertEqual(
            run.agent_read(400, {}, TICKET, MARKER), ("INCONCLUSIVE", 400, "http_error")
        )

    def test_stored_shape_and_marker(self):
        for summary, expected in (
            (None, "ABSENT_BY_ID"),
            ({}, "ABSENT_BY_ID"),
            ({"text": "", "key_points": []}, "ABSENT_BY_ID"),
            ({"text": "Escalate to COPPER-COMPASS."}, "FOUND"),
            ({"key_points": ["Use copper compass"]}, "FOUND"),
            ({"text": "other tenant summary"}, "INCONCLUSIVE"),
            ({"unexpected": True}, "INCONCLUSIVE"),
            ([], "INCONCLUSIVE"),
            ({"text": 1}, "INCONCLUSIVE"),
            ({"text": "", "key_points": ["other note"]}, "INCONCLUSIVE"),
        ):
            with self.subTest(summary=summary):
                self.assertEqual(run.summary_state(summary, MARKER), expected)

    def test_confirmed_residual_survives_other_failed_reads_and_gate_conversion(self):
        report = {
            **METADATA,
            "report_version": 2,
            "offboarding_http_status": 204,
            "interrupted_at": None,
            "observations": [
                {
                    "phase": "before_offboarding",
                    "observed_at": run.now(),
                    "workspaces": {label: HEALTHY_V2.copy() for label in run.CASES},
                }
            ],
        }
        ids = {label: tuple(str(uuid4()) for _ in range(3)) for label in run.CASES}
        db, api = Mock(), Mock()
        db.sql.side_effect = [
            "1",
            OSError("Acme summary read failed"),
            "1",
            '{"text":"Violet-sail"}',
        ]
        api.request.side_effect = [
            (400, {"code": "workspace_required"}),
            TimeoutError("Birch unavailable"),
        ]
        rows = run.checkpoint(db, api, "agent", ids, report, "after_offboarding")
        self.assertEqual(rows["Acme"]["source_message_row"], "FOUND")
        self.assertEqual(rows["Acme"]["stored_ticket_summary"], "INCONCLUSIVE")
        self.assertEqual(rows["Birch"]["agent_read_reason"], "read_error")
        self.assertEqual(rows["Birch"]["stored_ticket_summary"], "FOUND")
        report["outcome"] = outcome(report["observations"], 2)
        self.assertEqual(report["outcome"], "FAIL")
        validate(report)
        policy = json.loads((HERE / "policy-v2.json").read_bytes())
        result = gate.evaluate(
            policy, to_gate.observations(json.dumps(report).encode())
        )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(
            [probe["verdict"] for probe in result["probes"]],
            ["FAIL", "INCONCLUSIVE", "INCONCLUSIVE", "INCONCLUSIVE"],
        )
        for key, value in (
            ("agent_summary", "NOT_RETURNED"),
            ("agent_http_status", 200),
        ):
            original = rows["Birch"][key]
            rows["Birch"][key] = value
            with self.assertRaises(ValueError):
                validate(report)
            rows["Birch"][key] = original

    def test_interrupted_checkpoint_keeps_rows_still_unvisited(self):
        report = {
            **METADATA,
            "report_version": 2,
            "offboarding_http_status": 204,
            "interrupted_at": "observation",
            "observations": [
                {
                    "phase": "before_offboarding",
                    "observed_at": run.now(),
                    "workspaces": {label: HEALTHY_V2.copy() for label in run.CASES},
                },
                {
                    "phase": "after_offboarding",
                    "observed_at": run.now(),
                    "workspaces": {
                        "Acme": {**UNTESTED, "stored_ticket_summary": "FOUND"},
                        "Birch": UNTESTED.copy(),
                    },
                },
            ],
        }
        report["outcome"] = outcome(report["observations"], 2)
        self.assertEqual(report["outcome"], "FAIL")
        validate(report)
        packet = to_gate.observations(json.dumps(report).encode())
        self.assertEqual(
            packet["probes"][1]["after"],
            {"subject": "present", "control": "not_tested"},
        )

    def test_standalone_readers_reject_oversize_and_nonregular_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            large = directory / "oversize.json"
            large.write_bytes(b" " * (MAX_INPUT_BYTES + 1))
            inputs = [large, directory]
            if hasattr(os, "mkfifo"):
                fifo = directory / "pipe"
                os.mkfifo(fifo)
                inputs.append(fifo)
            for path in inputs:
                for args in (
                    ["-m", "examples.opendesk.verify_report", path],
                    [
                        "-m", "examples.opendesk.to_gate",
                        path,
                        "--output",
                        directory / "converted.json",
                    ],
                ):
                    with self.subTest(args=args):
                        result = subprocess.run(
                            [sys.executable, *args],
                            check=False,
                            capture_output=True,
                            timeout=5,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn(b"Traceback", result.stderr)
