import json
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

from pathlib import Path

from setrya.adapter import Checkpoint, Publication, ReadView, Receipt, SearchResult
from setrya.checker import Status, Verdict, evaluate
from setrya.drill import Drill
from setrya.evidence import Report
from setrya.recorder import Recorder
from trials.opendesk_revocation.policy import policies
from trials.opendesk_revocation.provider import SEED, UPDATED
from trials.opendesk_revocation.run import HERE, classify, decision, native_result, run_lane


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

    def test_retained_summary_revision_survives_offline_replay(self):
        drill = Drill.load(HERE / "drill.toml")
        rules = drill.rules
        for final_text, extra, expected, detail in (
            (UPDATED, [], Status.PASS, ""),
            (SEED, [], Status.FAIL, "unexpected-seeded-summary"),
            (UPDATED, [{"id": "other"}], Status.NOT_TESTED, "lacked revision"),
            (UPDATED, [{"id": "other"}, {"id": "team", "custom": {"ai_summary": {"text": UPDATED}}}],
             Status.FAIL, "forbidden team-summary at rank 3"),
        ):
            with self.subTest(expected=expected, detail=detail), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "run.json"
                with Recorder(drill, test_id="opendesk-team-revocation", report=path) as rec:
                    def observe_all(stage):
                        def read(probe):
                            scope = probe.rules.scope
                            final = stage == "final"
                            rows = [{"id": "public", "subject": "witness" if final else "public"}]
                            if stage == "baseline" or scope == "colleague":
                                rows.append({"id": "team", "custom": {"ai_summary": {"text": final_text if final else SEED}}})
                            if final and scope == "departing":
                                rows.extend(extra)
                            markers, _, seen = classify(200, {"tickets": rows}, "team", "public",
                                                        "witness" if final else None, retained=scope == "colleague")
                            view = ReadView(rec.run_id, 1, probe.rules.route, scope) if seen else None
                            return SearchResult(markers, view=view, route=probe.rules.route, scope=scope)
                        for probe in drill.probes:
                            rec.search(probe.rules.id, read)
                    rec.seeded()
                    observe_all("baseline")
                    rec.started("job")
                    rec.held(Checkpoint(rec.run_id, "job", rules.document, rules.old_revision, rules.checkpoint))
                    rec.published(Publication(rec.run_id, rules.document, rules.new_revision))
                    observe_all("held")
                    rec.released("job")
                    rec.receipt(Receipt(rec.run_id, "job", rules.document, rules.old_revision, "committed", rec.run_id, 1))
                    observe_all("final")
                replay = evaluate(Report.load(path))
                self.assertEqual((rec.result.verdict.status, replay.status), (expected, expected))
                self.assertIn(detail, replay.detail)

    def test_partial_results_preserve_known_rows_and_cannot_prove_absence(self):
        public = {"id": "y", "subject": "witness"}
        protected = {"id": "x", "custom": {"ai_summary": {"text": UPDATED}}}
        unknown = {"id": "other"}
        for status, result, expected in (
            (400, {}, ((None,), "unknown", False)),
            (200, {}, ((None,), "unknown", False)),
            (200, {"tickets": [unknown]}, ((None,), "unknown", False)),
            (200, {"tickets": [public, public]}, (("public-control", None), "unknown", True)),
            (200, {"tickets": [None, protected, unknown, public]},
             ((None, "team-summary", None, "public-control"), "recomputed", True)),
            (200, {"tickets": [unknown] * 5 + [protected]}, ((None,) * 5, "unknown", False)),
        ):
            with self.subTest(result=result):
                self.assertEqual(classify(status, result, "x", "y", "witness"), expected)

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

    def test_trial_errors_preserve_lane_evidence_and_candidate_decision(self):
        for operation in ("startup", "cleanup"):
            with (self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory,
                  patch("trials.opendesk_revocation.run.Stack") as stack,
                  patch("trials.opendesk_revocation.run.start_verified") as start,
                  patch("trials.opendesk_revocation.run.exercise")):
                out = Path(directory)
                start.return_value = {}
                stack.return_value.project = "isolated-project"
                if operation == "startup":
                    start.side_effect = OSError("test environment unavailable")
                else:
                    stack.return_value.compose.side_effect = OSError("test cleanup unavailable")
                result, log = run_lane(out, "base", {"private": False}, "a" * 40)
                self.assertEqual(evaluate(Report.load(out / "base.json")).status, Status.ERROR)
                self.assertEqual(json.loads((out / "base-observations.json").read_bytes()), log)
                self.assertEqual(log["trial_error"], "OSError")
                if operation == "cleanup":
                    self.assertEqual(log["cleanup_project"], "isolated-project")
                candidate = SimpleNamespace(verdict=Verdict(Status.PASS, ""))
                self.assertEqual(decision(result, log, candidate, self.caller_log(), {})[0], 2)
                observed = self.caller_log()
                observed["searches"][2]["summary"] = "recomputed"
                self.assertEqual(decision(result, log, candidate, observed, {})[0], 1)

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
