"""An expected native failure must be observed, even when the receipt also replays FAIL."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class GraphitiTrialTests(unittest.TestCase):
    def test_expected_failure_requires_matching_observations(self):
        script = Path(__file__).resolve().parents[1] / "trials" / "graphiti" / "trial.py"
        source = {"shared_node": "case", "target_fact": True, "control_fact": True,
                  "target_episode": True, "control_episode": True}
        packet = {"graphiti_version": "0.31.1", "source_revision": "5961293865a57844fe13d4eb061deb3501971fb2",
                  "before": source, "after": {**source, "target_episode": False}, "setrya_verdict": "FAIL"}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.json"
            command = [sys.executable, "-I", str(script), str(path), "--expect", "FAIL"]
            for data, valid in ((packet, True), ({**packet, "setrya_verdict": "PASS"}, False),
                                ({**packet, "before": {}}, False)):
                with self.subTest(packet=data):
                    path.write_text(json.dumps(data))
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, valid, result.stderr)
