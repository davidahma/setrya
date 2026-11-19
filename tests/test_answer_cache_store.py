"""Check the cache read and write guards separately against PostgreSQL and its real HTTP API."""
from http.client import HTTPConnection
from importlib.util import find_spec
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from setrya.examples.answer_cache.contract import PRIVATE_TEXT, PUBLIC_TEXT, QUESTION

_REQUIRED = ("psycopg", "langchain_core", "celery")


@unittest.skipUnless(os.environ.get("SETRYA_PGVECTOR_DSN") and all(find_spec(p) for p in _REQUIRED),
                     "requires disposable pgvector and the answer-cache extra")
class AnswerCacheStoreTests(unittest.TestCase):
    def setUp(self):
        from setrya.examples.answer_cache.app import Application
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.app = Application(uuid4().hex, "guarded", Path(self.scratch.name))
        self.addCleanup(self.app.close)
        self.app.seed()

    def test_guarded_http_cannot_serve_an_obsolete_physical_entry(self):
        from setrya.examples.pgvector_search.app import connection
        self.app.revoke()
        # Put an old copy back independently of the cache-fill guard.
        with connection(self.app.store.schema) as db:
            db.execute("""INSERT INTO answers SELECT 'revoked', question, body,
                          jsonb_set(dependencies,'{access_scope}','"revoked"'), writer FROM answers
                          WHERE caller='retained' AND question=%s""", (QUESTION,))
            # Even a row stamped as current must have readable dependencies.
            db.execute("""UPDATE answers SET dependencies=jsonb_set(dependencies,'{access_version}',
                           to_jsonb((SELECT epoch::text FROM access_policy WHERE caller='revoked')))
                          WHERE caller='revoked'""")
        revoked = self.app.answer("revoked")
        retained = self.app.answer("retained")
        self.assertEqual((revoked["http_status"], revoked["cache_hit"], revoked["answer"]), (200, False, PUBLIC_TEXT))
        self.assertEqual((retained["http_status"], retained["cache_hit"]), (200, True))
        self.assertIn(PRIVATE_TEXT, retained["answer"])

    def test_source_update_and_delete_reject_old_fills_and_old_hits(self):
        from psycopg.types.json import Jsonb
        from setrya.examples.pgvector_search.app import connection
        from setrya.store_signals import passage_metadata

        store = self.app.store
        initial = store.capture("retained", QUESTION)
        updated = passage_metadata("policy", "B", "0")
        new_text = "The private refund ceiling is 240 credits."
        with connection(store.schema) as db:
            db.execute("""UPDATE chunks SET marker=%s, metadata=%s, body=%s WHERE private""",
                       (updated["setrya_marker"], Jsonb(updated), new_text))
        store.fill(initial, "before-update")
        self.assertEqual(store.receipt("before-update").outcome, "rejected")
        row = self.app.answer("retained")
        self.assertEqual((row["cache_hit"], row["answer"]), (False, new_text + " " + PUBLIC_TEXT))
        self.assertIn(updated["setrya_marker"], row["sources"])

        fresh = store.capture("retained", QUESTION)
        store.fill(fresh, "current")
        self.assertEqual(store.receipt("current").outcome, "committed")
        self.assertTrue(self.app.answer("retained")["cache_hit"])
        with connection(store.schema) as db:
            db.execute("DELETE FROM chunks WHERE private")
        store.fill(fresh, "before-deletion")
        self.assertEqual(store.receipt("before-deletion").outcome, "rejected")
        row = self.app.answer("retained")
        self.assertEqual((row["cache_hit"], row["answer"]), (False, PUBLIC_TEXT))
        self.assertNotIn(updated["setrya_marker"], row["sources"])

    def test_rank_changing_insert_preserves_other_scope_and_move_invalidates_both(self):
        from setrya.examples.answer_cache.contract import DELIVERY_QUERY, PRIVATE, REFUND_QUERY
        from setrya.examples.pgvector_search.app import connection

        store = self.app.store
        refund = store.capture("retained", REFUND_QUERY)
        delivery = store.capture("retained", DELIVERY_QUERY)
        store.supersede_refund()
        store.fill(refund, "former-rank")
        store.fill(delivery, "still-delivery")
        self.assertEqual(store.receipt("former-rank").outcome, "rejected")
        self.assertEqual(store.receipt("still-delivery").outcome, "committed")
        self.assertEqual(self.app.answer("retained", DELIVERY_QUERY)["cache_writer"], "still-delivery")

        refund = store.capture("retained", REFUND_QUERY)
        with connection(store.schema) as db:
            db.execute("UPDATE chunks SET scope='delivery' WHERE marker=%s", (PRIVATE,))
        for question, capture in ((REFUND_QUERY, refund), (DELIVERY_QUERY, delivery)):
            attempt = capture.dependencies.retrieval_scope
            store.fill(capture, attempt)
            self.assertEqual(store.receipt(attempt).outcome, "rejected")
            self.assertFalse(self.app.answer("retained", question)["cache_hit"])

    def test_truncation_rejects_all_previously_captured_fills(self):
        from setrya.examples.answer_cache.contract import DELIVERY_QUERY, REFUND_QUERY
        from setrya.examples.pgvector_search.app import connection

        store = self.app.store
        captures = [store.capture("retained", question) for question in (REFUND_QUERY, DELIVERY_QUERY)]
        with connection(store.schema) as db:
            db.execute("TRUNCATE chunks")
        for capture in captures:
            attempt = capture.dependencies.retrieval_scope
            store.fill(capture, attempt)
            self.assertEqual(store.receipt(attempt).outcome, "rejected")

    def test_endpoint_requires_a_real_credential_and_refuses_client_supplied_identity(self):
        for headers, payload, expected in [
            ({}, {"question": QUESTION}, 401),
            ({"Authorization": "Bearer " + self.app.tokens["revoked"]},
             {"question": QUESTION, "caller": "retained"}, 400),
        ]:
            with self.subTest(status=expected):
                client = HTTPConnection(*self.app.server.server_address, timeout=5)
                try:
                    client.request("POST", "/answer", json.dumps(payload), headers)
                    self.assertEqual(client.getresponse().status, expected)
                finally:
                    client.close()
