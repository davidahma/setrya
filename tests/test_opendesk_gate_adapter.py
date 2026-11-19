import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from setrya.examples.opendesk import to_gate

TRIAL = Path(__file__).resolve().parents[1] / "setrya" / "examples" / "opendesk"
RECORDED = TRIAL / "observed.json"


def convert(trial):
    return to_gate.observations(json.dumps(trial).encode())


def probe_states(packet):
    return {probe["id"]: probe for probe in packet["probes"]}


class GateAdapterTests(unittest.TestCase):
    def setUp(self):
        self.trial = json.loads(RECORDED.read_bytes())

    def test_recorded_trial_through_gate_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            output = Path(directory) / "decision.json"
            subprocess.run(
                [sys.executable, "-m", "setrya.examples.opendesk.to_gate", RECORDED, "--output", evidence],
                check=True,
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "setrya",
                    "offboarding",
                    "gate",
                    "--policy",
                    TRIAL / "policy.json",
                    "--observations",
                    evidence,
                    "--output",
                    output,
                ],
                cwd=TRIAL.parents[2],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            packet = json.loads(evidence.read_bytes())
            decision = json.loads(output.read_bytes())
        self.assertEqual(packet["revision"], self.trial["target"]["commit"])
        self.assertEqual(
            packet["run_id"],
            "sha256-" + hashlib.sha256(RECORDED.read_bytes()).hexdigest(),
        )
        self.assertEqual(packet["offboarding"], "completed")
        rows = probe_states(packet)
        self.assertEqual(
            rows["agent-read"]["after"],
            {"subject": "unavailable", "control": "present"},
        )
        self.assertEqual(
            rows["worker-summary"]["after"],
            {"subject": "not_tested", "control": "not_tested"},
        )
        self.assertEqual(
            rows["worker-summary"]["worker"],
            {
                "captured_before_offboarding": False,
                "finished_after_offboarding": False,
            },
        )
        self.assertEqual(decision["verdict"], "INCONCLUSIVE")
        self.assertEqual(
            {row["id"]: row["verdict"] for row in decision["probes"]},
            {
                "source": "PASS",
                "summary": "PASS",
                "agent-read": "INCONCLUSIVE",
                "worker-summary": "INCONCLUSIVE",
            },
        )

    def test_missing_checkpoints_do_not_become_absent_rows(self):
        self.trial["outcome"] = "INCONCLUSIVE"
        self.trial["interrupted_at"] = "observation"
        self.trial["observations"].pop()
        packet = convert(self.trial)
        self.assertEqual(packet["offboarding"], "completed")
        for row in packet["probes"]:
            self.assertEqual(
                row["before"], {"subject": "present", "control": "present"}
            )
            self.assertEqual(
                row["after"], {"subject": "not_tested", "control": "not_tested"}
            )
        self.trial.update(
            interrupted_at="startup", observations=[], offboarding_http_status=None
        )
        packet = convert(self.trial)
        self.assertEqual(packet["offboarding"], "unconfirmed")
        for row in packet["probes"]:
            self.assertEqual(
                row["before"], {"subject": "not_tested", "control": "not_tested"}
            )

    def test_cleanup_interruption_preserves_observed_residual(self):
        self.trial.update(interrupted_at="cleanup", outcome="FAIL")
        acme = self.trial["observations"][1]["workspaces"]["Acme"]
        acme.update(agent_http_status=200, agent_summary="RETURNED")
        rows = probe_states(convert(self.trial))
        self.assertEqual(rows["agent-read"]["after"]["subject"], "present")
        self.assertEqual(rows["summary"]["after"]["subject"], "absent")

    def test_error_or_unpinned_build_rejected_before_writing(self):
        raw = RECORDED.read_bytes()
        payloads = [
            raw.replace(
                b'"report_version":', b'"report_version": 1, "report_version":'
            ),
            raw.replace(b'"v0.4.13"', b'"v0.0.0"'),
            raw.replace(
                b'"agent_summary": "INCONCLUSIVE"', b'"agent_summary": "NOT_RETURNED"'
            ),
        ]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            source = Path(directory) / "upstream.json"
            output = Path(directory) / "portable.json"
            for payload in payloads:
                with self.subTest(payload=payload[:30]):
                    source.write_bytes(payload)
                    self.assertNotEqual(
                        to_gate.main([str(source), "--output", str(output)]), 0
                    )
                    self.assertFalse(output.exists())
            source.write_bytes(raw)
            output.hardlink_to(source)
            self.assertNotEqual(to_gate.main([str(source), "--output", str(output)]), 0)
            self.assertEqual(source.read_bytes(), raw)
