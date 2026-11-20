"""Check the own-endpoint replay boundary independently of its HTTP integration."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.drill import InputError
from setrya.endpoint import MissingHook, check_endpoint
from setrya.examples.answer_cache.evidence import FIXTURE_CASE, MANIFEST, replay
from setrya.examples.answer_cache.metrics import bundle_metrics
from setrya.status import Status
from test_answer_cache_drill import SyntheticAnswers


class EndpointIntegrityTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)

    def exercise(self, name, app=SyntheticAnswers, *, mode="guarded"):
        directory = self.root / name
        result = check_endpoint(FIXTURE_CASE, lambda run_id: app(run_id, mode, directory), directory, wait_seconds=0)
        self.assertEqual(result, replay(directory))
        return directory, result

    def test_exactly_one_read_per_caller_and_phase_and_no_raw_text(self):
        directory, (verdict, _) = self.exercise("fixed")
        self.assertIs(verdict.status, Status.PASS)
        raw = (directory / MANIFEST).read_text()
        manifest = json.loads(raw)
        for expected in (FIXTURE_CASE.private_answer, FIXTURE_CASE.public_answer, FIXTURE_CASE.question):
            self.assertNotIn(expected, raw + (directory / "run.json").read_text())
        self.assertEqual([(r["stage"], r["caller"]) for r in manifest["observations"]],
                         [(stage, caller) for stage in ("baseline", "freshness", "final")
                          for caller in (FIXTURE_CASE.revoked, FIXTURE_CASE.retained)])
        self.assertEqual(bundle_metrics([directory]).snapshot().rejected_fills, 1)

    def test_unexpected_serving_and_worker_identities_stay_out_of_exported_evidence(self):
        unexpected_caller = "other-person@example.test"
        unexpected_route = "internal-service"

        class WrongSession(SyntheticAnswers):
            def answer(self, caller, question):
                row = super().answer(caller, question)
                if caller == FIXTURE_CASE.revoked:
                    row.update(caller=unexpected_caller, route=unexpected_route)
                return row

        class WrongWorker(SyntheticAnswers):
            def captured(self):
                return {**super().captured(), "caller": unexpected_caller}

        for app in (WrongSession, WrongWorker):
            with self.subTest(app=app.__name__):
                directory, (verdict, summary) = self.exercise(app.__name__, app)
                self.assertIs(verdict.status, Status.NOT_TESTED)
                raw = summary + (directory / MANIFEST).read_text() + (directory / "run.json").read_text()
                self.assertNotIn(unexpected_caller, raw)
                self.assertNotIn(unexpected_route, raw)

    def test_case_is_bound_to_portable_evidence_and_rejects_ambiguous_inputs(self):
        for change in ({"retained": FIXTURE_CASE.revoked}, {"private_source": "cache-hit"},
                       {"private_source": FIXTURE_CASE.public_source}, {"public_answer": FIXTURE_CASE.private_answer}):
            with self.subTest(change=change), self.assertRaises(InputError):
                replace(FIXTURE_CASE, **change)
        directory, _ = self.exercise("fixed")
        path = directory / MANIFEST
        manifest = json.loads(path.read_text())
        for key, value in (("private_answer_sha256", "1" * 64), ("route", "another-route"),
                           ("public_source", [])):
            with self.subTest(key=key):
                path.write_text(json.dumps({**manifest, "case": {**manifest["case"], key: value}}))
                with self.assertRaises(InputError):
                    replay(directory)
        for value in (None, [], "text", {"body": []}):
            with self.subTest(row=value):
                path.write_text(json.dumps({**manifest, "observations": [value, *manifest["observations"][1:]]}))
                with self.assertRaises(InputError):
                    replay(directory)

    def test_hook_and_phase_specific_diagnosis(self):
        for fault, hook in (("no-capture", "captured()"), ("no-checkpoint", "checkpoint()"),
                            ("missing-write", "receipt()"), ("other-question", "query_sha256"),
                            ("wrong-user", "authenticated caller"), ("early-after-change", "receipt()")):
            with self.subTest(fault=fault):
                app = type("App", (SyntheticAnswers,), {"fault": fault})
                _, (verdict, _) = self.exercise(fault, app)
                self.assertIs(verdict.status, Status.NOT_TESTED)
                self.assertIn(hook, verdict.detail)

    def test_first_unwired_hook_and_raised_app_error_are_distinct(self):
        for exc, expected in ((MissingHook("seed"), Status.NOT_TESTED),
                              (ConnectionError("private error message"), Status.ERROR)):
            class Unwired(SyntheticAnswers):
                def seed(self):
                    raise exc
            path, (verdict, text) = self.exercise(type(exc).__name__, Unwired)
            self.assertIs(verdict.status, expected)
            self.assertIn("seed()", verdict.detail)
            self.assertNotIn("private error message", text + (path / MANIFEST).read_text() + (path / "run.json").read_text())

    def test_malformed_return_is_attributed_to_the_actual_hook(self):
        class WrongReceipt(SyntheticAnswers):
            def receipt(self):
                return {} if self.released else None
        for name, app in (
            ("start_old", type("WrongStart", (SyntheticAnswers,), {"start_old": lambda self: None})),
            ("checkpoint", type("WrongHold", (SyntheticAnswers,), {"checkpoint": lambda self: {}})),
            ("revoke", type("WrongAck", (SyntheticAnswers,), {"revoke": lambda self: {}})),
            ("receipt", WrongReceipt),
        ):
            with self.subTest(name=name):
                _, (verdict, _) = self.exercise(name, app)
                self.assertIs(verdict.status, Status.ERROR)
                self.assertIn(name + "()", verdict.detail)

    def test_each_caller_needs_a_warm_baseline(self):
        for unwarmed in (FIXTURE_CASE.revoked, FIXTURE_CASE.retained):
            class Cold(SyntheticAnswers):
                def answer(self, caller, question):
                    row = super().answer(caller, question)
                    if not self.revoked and caller == unwarmed:
                        row.update(cache_hit=False, cache_writer=None)
                    return row
            with self.subTest(caller=unwarmed):
                _, (verdict, _) = self.exercise(unwarmed, Cold)
                self.assertIs(verdict.status, Status.NOT_TESTED)
                self.assertIn("baseline read for " + unwarmed, verdict.detail)
                self.assertIn("cache-hit", verdict.detail)

    def test_interrupt_is_propagated_after_cleanup_and_replayable_prefix(self):
        cleaned = []
        class Interrupted(SyntheticAnswers):
            def revoke(self):
                raise KeyboardInterrupt
            def __exit__(self, *args):
                cleaned.append(self.run_id)
        directory = self.root / "interrupted"
        with self.assertRaises(KeyboardInterrupt):
            check_endpoint(FIXTURE_CASE, lambda rid: Interrupted(rid, "guarded", directory), directory, wait_seconds=0)
        self.assertEqual(len(cleaned), 1)
        verdict, summary = replay(directory)
        self.assertIs(verdict.status, Status.ERROR)
        self.assertIn("revoke()", verdict.detail)
        self.assertIn("baseline", summary)
        self.assertNotIn("final ·", summary)

    def test_observed_private_response_survives_unknown_writer_and_cleanup_error(self):
        class BadCleanup(SyntheticAnswers):
            fault = "close"
            def answer(self, caller, question):
                row = super().answer(caller, question)
                if self.released and caller == FIXTURE_CASE.revoked:
                    row.update(http_status=403, cache_writer="unrelated-attempt")
                return row
        directory, (verdict, summary) = self.exercise("broken", BadCleanup, mode="broken")
        self.assertIs(verdict.status, Status.FAIL)
        self.assertNotIn("served the resumed task", summary)
        self.assertEqual(bundle_metrics([directory]).snapshot().stale_serves, 1)

    def test_unidentified_http_status_cannot_establish_a_served_answer(self):
        for status in (None, 0, 700):
            class MissingStatus(SyntheticAnswers):
                def answer(self, caller, question):
                    row = super().answer(caller, question)
                    if self.released and caller == FIXTURE_CASE.revoked:
                        row["http_status"] = status
                    return row
            with self.subTest(status=status):
                directory, (verdict, _) = self.exercise(str(status), MissingStatus, mode="broken")
                self.assertIs(verdict.status, Status.NOT_TESTED)
                self.assertEqual(bundle_metrics([directory]).snapshot().stale_serves, 0)
