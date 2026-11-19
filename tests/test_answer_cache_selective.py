"""Replay mutations of an actual Celery, pgvector and authenticated HTTP drill."""
from copy import deepcopy
from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.checker import Status
from setrya.drill import InputError
from setrya.examples.answer_cache import selective

_REQUIRED = ("psycopg", "langchain_core", "celery")


@unittest.skipUnless(os.environ.get("SETRYA_PGVECTOR_DSN") and all(find_spec(p) for p in _REQUIRED),
                     "requires disposable pgvector and the answer-cache extra")
class SelectiveReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.bundle = Path(directory.name) / "run"
        verdict, _ = selective.run(cls.bundle, "guarded")
        if verdict.status != Status.PASS:
            raise AssertionError(verdict.detail)
        cls.trace = json.loads((cls.bundle / selective.JOURNAL).read_text())

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name)
        self.trace = deepcopy(type(self).trace)

    def assess(self):
        raw = json.dumps(self.trace).encode()
        (self.path / selective.JOURNAL).write_bytes(raw)
        manifest = json.loads((self.bundle / "answer-cache.json").read_text())
        manifest["trace_sha256"] = sha256(raw).hexdigest()
        (self.path / "answer-cache.json").write_text(json.dumps(manifest))
        verdict, _ = selective.replay(self.path)
        _, metrics = selective.read_metrics(self.path)
        return verdict.status, metrics.snapshot()

    def test_native_reuse_and_generation(self):
        verdict, counts = self.assess()
        self.assertEqual(verdict, Status.PASS)
        self.assertEqual((counts.hits, counts.misses, counts.checked, counts.stale_serves,
                          counts.skipped_generations, counts.rejected_fills), (6, 2, 8, 0, 6, 1))

    def test_source_scope_or_worker_access_change_must_explain_rejection(self):
        for change in ("acl", "regress", "same"):
            with self.subTest(change=change):
                self.trace = deepcopy(type(self).trace)
                if change == "acl":
                    self.trace["capture"]["dependencies"]["access_version"] = "999"
                else:
                    before = self.trace["capture"]["dependencies"]["retrieval_version"]
                    self.trace["published"]["refund"] = "0" if change == "regress" else before
                verdict, counts = self.assess()
                self.assertEqual(verdict, Status.NOT_TESTED)
                self.assertEqual((counts.checked, counts.rejected_fills), (2, 0))

    def test_missing_sources_and_receipts_reduce_the_denominator(self):
        for row in self.trace["reads"]:
            row.update(dependencies=None, sources=[])
        verdict, counts = self.assess()
        self.assertEqual((verdict, counts.checked, counts.unverified), (Status.NOT_TESTED, 0, 8))
        self.trace = deepcopy(type(self).trace)
        self.trace.update(old_receipt=None, new_receipt=None)
        verdict, counts = self.assess()
        self.assertEqual((verdict, counts.checked, counts.rejected_fills), (Status.NOT_TESTED, 4, 0))

    def test_caller_query_and_native_relevant_write_are_required(self):
        for field, value in (("caller", "revoked"), ("query_sha256", "wrong"), ("commit", 0)):
            with self.subTest(field=field):
                self.trace = deepcopy(type(self).trace)
                self.trace["reads"][6][field] = value
                verdict, counts = self.assess()
                self.assertEqual((verdict, counts.unverified), (Status.NOT_TESTED, 1))
        self.trace = deepcopy(type(self).trace)
        self.trace["refill_capture"]["dependencies"]["answer_id"] = "another-generation"
        self.assertEqual(self.assess()[0], Status.NOT_TESTED)

    def test_recognized_old_body_or_source_is_a_failure_even_when_later_proof_is_missing(self):
        for field, value in (("body", "old-refund"), ("sources", [selective.PRIVATE])):
            with self.subTest(field=field):
                self.trace = deepcopy(type(self).trace)
                self.trace["reads"][2][field] = value
                self.trace.update(new_receipt=None, error="ConnectionError")
                verdict, counts = self.assess()
                self.assertEqual((verdict, counts.stale_serves), (Status.FAIL, 1))

    def test_unaffected_answer_must_reuse_the_same_generation_and_skip_work(self):
        for field, value in (("cache_writer", "another-worker"), ("generation_skipped", False)):
            with self.subTest(field=field):
                self.trace = deepcopy(type(self).trace)
                self.trace["reads"][3][field] = value
                verdict, counts = self.assess()
                self.assertEqual((verdict, counts.stale_serves), (Status.FAIL, 0))

    def test_unattributable_hit_and_unrecognized_answer_are_unknown(self):
        self.trace["reads"][2].update(body=None, cache_hit=True, cache_writer=None)
        verdict, counts = self.assess()
        self.assertEqual((verdict, counts.unverified, counts.hits + counts.misses),
                         (Status.NOT_TESTED, 6, 7))

    def test_rejection_needs_both_pre_release_lookups_of_the_selected_task(self):
        for stage in ("published", "before_release"):
            for change in ("wrong-task", "terminal", "missing"):
                with self.subTest(stage=stage, change=change):
                    self.trace = deepcopy(type(self).trace)
                    check = self.trace["receipt_checks"][stage]
                    if change == "wrong-task":
                        check["attempt"] = "different-task"
                    elif change == "terminal":
                        check["receipt"] = self.trace["old_receipt"]
                    else:
                        del self.trace["receipt_checks"][stage]
                    verdict, counts = self.assess()
                    self.assertEqual((verdict, counts.rejected_fills), (Status.NOT_TESTED, 0))

    def test_native_receipt_before_release_stops_the_held_worker(self):
        from setrya.examples.answer_cache.app import Application
        from setrya.examples.answer_cache.contract import DELIVERY_QUERY, REFUND_QUERY

        trace = selective._new_trace()
        with Application(trace["run_id"], "guarded", self.path) as app:
            publish = app.store.supersede_refund
            answer = app.answer
            old = None

            def supersede():
                nonlocal old
                old = app.store.capture("retained", REFUND_QUERY)
                return publish()

            def answer_then_reject(caller, query):
                row = answer(caller, query)
                if old is not None and query == DELIVERY_QUERY:
                    app.store.fill(old, app.attempt)
                return row

            with (patch.object(app.store, "supersede_refund", supersede),
                  patch.object(app, "answer", answer_then_reject), patch.object(app, "release") as release):
                selective._exercise(app, trace, 30)
                release.assert_not_called()
                self.assertIsNotNone(app.checkpoint())
        self.assertIsNone(trace["receipt_checks"]["published"]["receipt"])
        self.assertEqual(trace["receipt_checks"]["before_release"]["receipt"]["outcome"], "rejected")
        self.assertIsNone(trace["refill"])
        verdict, _, metrics = selective._assess(trace)
        self.assertEqual((verdict.status, metrics.snapshot().rejected_fills), (Status.NOT_TESTED, 0))

    def test_wrong_caller_before_release_does_not_resume_the_selected_worker(self):
        from setrya.examples.answer_cache.app import Application

        trace = selective._new_trace()
        with Application(trace["run_id"], "guarded", self.path) as app:
            answer = app.answer

            def preview(caller, question):
                row = answer(caller, question)
                if trace["published"]:
                    row["caller"] = "preview"
                return row
            with patch.object(app, "answer", preview), patch.object(app, "release") as release:
                selective._exercise(app, trace, 30)
                release.assert_not_called()
                self.assertIsNotNone(app.checkpoint())
        verdict, _, metrics = selective._assess(trace)
        self.assertEqual((verdict.status, metrics.snapshot().rejected_fills), (Status.NOT_TESTED, 0))

    def test_real_stale_cache_returns_the_obsolete_answer_after_supersession(self):
        from setrya.examples.answer_cache.app import Application

        trace = selective._new_trace()
        with Application(trace["run_id"], "broken", self.path) as app:
            selective._exercise(app, trace, 30)
        verdict, _, metrics = selective._assess(trace)
        self.assertEqual(verdict.status, Status.FAIL)
        self.assertEqual(trace["reads"][2]["body"], "old-refund")
        self.assertIsNone(trace["old_receipt"])
        self.assertIsNone(trace["refill"])
        self.assertEqual(metrics.snapshot().stale_serves, 1)

    def test_resumed_unguarded_worker_can_undo_a_verified_clean_refund_read(self):
        from setrya.examples.answer_cache.app import Application
        from setrya.examples.answer_cache.contract import REFUND_QUERY
        from setrya.examples.pgvector_search.app import connection

        trace = selective._new_trace()
        with Application(trace["run_id"], "broken", self.path) as app:
            publish = app.store.supersede_refund

            def evict_current_refund():
                versions = publish()
                with connection(app.store.schema) as db:
                    db.execute("DELETE FROM answers WHERE question = %s", (REFUND_QUERY,))
                return versions

            with patch.object(app.store, "supersede_refund", evict_current_refund):
                selective._exercise(app, trace, 30)
        verdict, _, metrics = selective._assess(trace)
        self.assertEqual((trace["reads"][2]["body"], trace["reads"][4]["body"]), ("new-refund", "old-refund"))
        self.assertEqual(trace["old_receipt"]["outcome"], "committed")
        self.assertEqual((verdict.status, metrics.snapshot().stale_serves), (Status.FAIL, 1))

    def test_digest_and_observation_order_are_checked(self):
        self.assess()
        with (self.path / selective.JOURNAL).open("ab") as stream:
            stream.write(b" ")
        with self.assertRaises(InputError):
            selective.replay(self.path)
        self.trace["reads"].reverse()
        with self.assertRaises(InputError):
            self.assess()
