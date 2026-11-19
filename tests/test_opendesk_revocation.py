from types import SimpleNamespace
import tempfile
import unittest

from setrya.checker import Status, Verdict
from trials.opendesk_revocation.policy import policies
from trials.opendesk_revocation.provider import SEED, UPDATED
from trials.opendesk_revocation.run import classify, decision, native_result


class OpenDeskRevocationTests(unittest.TestCase):
    def test_deployment_history_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            lanes = policies(directory)
        self.assertEqual(lanes["base"][0], "eb71707cbb47915bf35c93dbd360cee7390339ac")
        self.assertEqual(lanes["candidate"][0], "3da56d7b53089eb18144bfd9a305c0c4dae6fd40")
        self.assertEqual(lanes["base"][1] | {"private": True}, lanes["candidate"][1])

    def test_only_recognized_results_with_the_later_public_write_carry_visibility(self):
        for text, expected in ((SEED, "seeded"), (UPDATED, "recomputed"), ("unexpected", "unknown")):
            with self.subTest(text=text):
                result = {"tickets": [{"id": "x", "custom": {"ai_summary": {"text": text}}},
                                      {"id": "y", "subject": "witness"}]}
                markers, state, seen = classify(200, result, "x", "y", "witness")
                self.assertEqual(state, expected)
                self.assertEqual(markers, (None if expected == "unknown" else "team-summary", "public-control"))
                self.assertTrue(seen)
                self.assertFalse(classify(200, result, "x", "y", "earlier-title")[2])
        public_only = {"tickets": [{"id": "y", "subject": "witness"}]}
        self.assertEqual(classify(200, public_only, "x", "y", "witness"), (("public-control",), "absent", True))

    def test_error_duplicate_and_unrecognized_results_cannot_establish_absence(self):
        for status, result in ((400, {}), (200, {}), (200, {"tickets": [{"id": "other"}]}),
                               (200, {"tickets": [{"id": "y"}, {"id": "y"}]})):
            with self.subTest(result=result):
                self.assertEqual(classify(status, result, "x", "y", "witness"), ((None,), "unknown", False))

    def caller_log(self):
        sample = {"worker_held": True, "worker_completed_with_new_summary": True, "searches": []}
        for phase in ("membership-removed-worker-held", "worker-completed"):
            for caller in ("departing", "colleague"):
                state = "absent" if caller == "departing" else (
                    "seeded" if phase == "membership-removed-worker-held" else "recomputed")
                sample["searches"].append({"phase": phase, "caller": caller, "http": 200, "summary": state,
                                           "public_control": True, "write_witness": phase == "worker-completed"})
        return sample

    def test_incomplete_worker_and_missing_colleague_leave_release_untested(self):
        sample = self.caller_log()
        self.assertEqual(native_result(sample)[0], 0)
        self.assertEqual(native_result(sample | {"worker_held": False})[0], 3)
        self.assertEqual(native_result(sample | {"worker_completed_with_new_summary": False})[0], 3)
        self.assertEqual(native_result(sample | {"searches": sample["searches"][:-1]})[0], 3)
        sample["searches"][-1]["write_witness"] = False
        self.assertEqual(native_result(sample)[0], 3)

    def test_permission_works_before_release_then_leaks_after_the_write(self):
        sample = self.caller_log()
        sample["searches"][2]["summary"] = "recomputed"
        self.assertEqual(native_result(sample)[0], 1)
        incomplete_base = sample | {"worker_held": False}
        candidate = SimpleNamespace(verdict=Verdict(Status.FAIL, "Forbidden team summary", 14))
        code, summary = decision(None, incomplete_base, candidate, sample, {})
        self.assertEqual(code, 1)
        self.assertIn("after the old write", summary)
        # A verified core failure also survives an incomplete native observation.
        sample["searches"][2]["write_witness"] = False
        code, summary = decision(None, incomplete_base, candidate, sample, {})
        self.assertEqual(code, 1)
        self.assertIn("event 14", summary)

    def test_native_controls_and_a_reappearing_held_ticket_can_fail(self):
        sample = self.caller_log()
        old_read = sample["searches"][0] | {"summary": "seeded"}
        # The runner polls during propagation. An old read preceding the first
        # absence can satisfy the native contract when the timed core check passes.
        sample["searches"].insert(0, old_read)
        self.assertEqual(native_result(sample)[0], 0)
        sample["searches"].insert(2, old_read)
        self.assertEqual(native_result(sample)[0], 1)
        sample = self.caller_log()
        sample["searches"][-1]["summary"] = "seeded"
        code, summary = decision(None, self.caller_log(), SimpleNamespace(verdict=Verdict(Status.PASS, "")), sample, {})
        self.assertEqual(code, 1)
        self.assertIn("retained colleague", summary)
