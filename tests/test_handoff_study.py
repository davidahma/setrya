"""A scripted exercise or missing reviewer cannot count as independent reuse."""

import unittest


from setrya.examples.handoff_study import session, summarize


class HandoffStudyTests(unittest.TestCase):
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
