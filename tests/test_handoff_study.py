"""A scripted exercise or missing reviewer cannot count as independent reuse."""

import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


from setrya.examples.handoff_study import session, summarize


class HandoffStudyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_scorecard_is_private_at_creation_and_exclusive_on_retry(self):
        with TemporaryDirectory() as directory:
            scorecard = Path(directory) / "session.json"
            command = [sys.executable, "-m", "setrya.examples.handoff_study", "init", str(scorecard)]
            subprocess.run(command, check=True, capture_output=True, umask=0o022)
            self.assertEqual(stat.S_IMODE(scorecard.stat().st_mode), 0o600)
            scorecard.write_text("existing study")
            repeated = subprocess.run(command, capture_output=True, umask=0o022)
            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(scorecard.read_text(), "existing study")

    def setUp(self):
        self.data = session()
        self.data.update(repository="app", own_application=True, engineer="one", independent_reviewer="two",
                         date="2026-11-20", first_arm="pytest", agent_model_tools_and_budget_for_both="same setup")
        for arm in self.data["arms"].values():
            for phase, row in arm.items():
                row.update(commit="a" if phase == "first_change" else "b", evidence="local-artifact",
                           reviewer_answer="reader received the retired answer", setup_seconds=3600,
                           review_seconds=120, correct=True)

    def report(self):
        return summarize(self.data)

    def test_assigned_change_is_separate_from_observed_reuse(self):
        self.assertIn("Own-application activation: observed", self.report())
        self.assertIn("later change: unmeasured", self.report())
        self.assertIn("ready to compare", self.report())
        self.data["later_setrya_use"].update(commit="c", evidence="followup", use="second-route", without_facilitator=True)
        self.assertIn("later change: unmeasured", self.report())
        self.data["later_setrya_use"]["team_chose_to_reuse"] = False
        self.assertIn("later change: unmeasured", self.report())
        self.data["later_setrya_use"]["team_chose_to_reuse"] = True
        self.assertIn("later change: second-route", self.report())
        self.data["own_application"] = False
        self.assertIn("activation: unmeasured", self.report())
        self.assertIn("later change: unmeasured", self.report())
        self.data["later_setrya_use"]["commit"] = "b"
        with self.assertRaises(ValueError):
            self.report()

    def test_missing_wrong_or_unmatched_evidence(self):
        self.data["arms"]["pytest"]["first_change"]["commit"] = "d"
        self.assertIn("different application revisions", self.report())
        self.data["arms"]["setrya"]["first_change"]["correct"] = False
        self.assertIn("incorrect decision", self.report())
        self.assertIn("activation: unmeasured", self.report())
        self.data["arms"]["setrya"]["first_change"]["correct"] = True
        self.data["arms"]["setrya"]["first_change"]["evidence"] = ""
        self.assertIn("activation: unmeasured", self.report())
        self.data["independent_reviewer"] = "ONE"
        with self.assertRaises(ValueError):
            self.report()
