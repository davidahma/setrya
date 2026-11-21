"""Direct untagged discovery with real storage clients and retained cross-scope controls."""
import importlib.util
import os
import secrets
import sqlite3
import unittest
from unittest.mock import patch

from setrya.erasure.canary import ElasticsearchCanaryScanner, PostgresCanaryScanner, RedisCanaryScanner
from setrya.erasure.langgraph_canary import (
    LangGraphMemoryCanaryScanner, PostgresCheckpointCanaryScanner, PostgresMemoryCanaryScanner,
    SQLiteCheckpointCanaryScanner,
)

MARKER = "untagged-%_incident-9141"


def installed(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


class LangGraphTest(unittest.TestCase):
    @unittest.skipUnless(installed("langgraph.checkpoint.sqlite"), "install erasure extra")
    def test_sqlite_orphan_checkpoint_writes(self):
        from langgraph.checkpoint.sqlite import SqliteSaver
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        saver = SqliteSaver(db)
        saver.setup()
        for thread in ("trial", "other"):
            cfg = {"configurable": {"thread_id": thread, "checkpoint_id": "pending", "checkpoint_ns": ""}}
            saver.put_writes(cfg, [("notes", {"summary": MARKER})], "queued")
        scanner = SQLiteCheckpointCanaryScanner(db)
        self.assertEqual(scanner.find(scope="trial", marker=MARKER), {"trial"})
        saver.delete_thread("trial")
        self.assertEqual(scanner.find(scope="trial", marker=MARKER), set())
        self.assertEqual(scanner.find(scope="other", marker=MARKER), {"other"})

    @unittest.skipUnless(installed("langgraph.store.memory"), "install erasure extra")
    def test_nested_memory_namespaces(self):
        from langgraph.store.memory import InMemoryStore
        store = InMemoryStore()
        for scope in ("trial", "other"):
            store.put(("teams", scope), "root", {"text": MARKER})
            store.put(("teams", scope, "inbox"), "notes", {"text": MARKER})
        scanner = LangGraphMemoryCanaryScanner(store, namespace=("teams",))
        before = scanner.find(scope="trial", marker=MARKER)
        self.assertIn("root", before)
        self.assertEqual(len(before), 2)
        store.delete(("teams", "trial", "inbox"), "notes")
        self.assertEqual(scanner.find(scope="trial", marker=MARKER), {"root"})
        self.assertEqual(scanner.find(scope="other", marker=MARKER), before)


@unittest.skipUnless(os.getenv("SETRYA_PGVECTOR_DSN") and installed("psycopg"), "configure disposable pgvector")
class PostgresTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.db = psycopg.connect(os.environ["SETRYA_PGVECTOR_DSN"], autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        schema = "canary_" + secrets.token_hex(8)
        self.db.execute("CREATE SCHEMA " + schema)
        self.addCleanup(self.db.execute, "DROP SCHEMA " + schema + " CASCADE")
        self.db.execute("SET search_path TO " + schema + ", public")

    def test_literal_content_even_without_embedding_or_subject_tag(self):
        self.db.execute("CREATE TABLE passages(id bigint, workspace text, body text, embedding vector(2))")
        self.db.execute("INSERT INTO passages VALUES(1,'trial',%s,NULL),(2,'other',%s,NULL),(3,'trial','public',NULL)",
                        (MARKER, MARKER))
        scanner = PostgresCanaryScanner(self.db, table="passages", key="id", content="body", scope_column="workspace")
        self.assertEqual(scanner.find(scope="trial", marker=MARKER), {"1"})
        self.db.execute("DELETE FROM passages WHERE id=1")
        self.assertEqual(scanner.find(scope="trial", marker=MARKER), set())
        self.assertEqual(scanner.find(scope="other", marker=MARKER), {"2"})

    def test_binary_content_cannot_be_read_as_a_text_column(self):
        from psycopg.errors import UndefinedFunction
        self.db.execute("CREATE TABLE passages(id bigint, workspace text, body bytea)")
        self.db.execute("INSERT INTO passages VALUES(1,'trial',%s)", (MARKER.encode(),))
        scanner = PostgresCanaryScanner(self.db, table="passages", key="id", content="body", scope_column="workspace")
        with self.assertRaises(UndefinedFunction):
            scanner.find(scope="trial", marker=MARKER)

    @unittest.skipUnless(installed("langgraph.checkpoint.postgres"), "install erasure-postgres extra")
    def test_orphan_pending_write_and_expired_memory(self):
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore
        saver = PostgresSaver(self.db)
        saver.setup()
        store = PostgresStore(self.db)
        store.setup()
        for scope in ("trial", "other"):
            cfg = {"configurable": {"thread_id": scope, "checkpoint_id": "pending", "checkpoint_ns": ""}}
            saver.put_writes(cfg, [("notes", MARKER)], "queued")
            store.put(("teams", scope), "saved", {"notes": MARKER})
        self.db.execute("UPDATE store SET expires_at=now() - INTERVAL '1 hour'")
        cp = PostgresCheckpointCanaryScanner(self.db)
        mem = PostgresMemoryCanaryScanner(self.db, namespace=("teams",))
        self.assertEqual(cp.find(scope="trial", marker=MARKER), {"trial"})
        self.assertEqual(mem.find(scope="trial", marker=MARKER), {"saved"})
        saver.delete_thread("trial")
        store.delete(("teams", "trial"), "saved")
        self.assertEqual(cp.find(scope="trial", marker=MARKER), set())
        self.assertEqual(mem.find(scope="trial", marker=MARKER), set())
        self.assertEqual(cp.find(scope="other", marker=MARKER), {"other"})
        self.assertEqual(mem.find(scope="other", marker=MARKER), {"saved"})


@unittest.skipUnless(os.getenv("SETRYA_REDIS_URL") and installed("redis"), "configure disposable Redis")
class RedisTest(unittest.TestCase):
    def test_literal_scope_and_non_string_failure(self):
        from redis import Redis, ResponseError
        client = Redis.from_url(os.environ["SETRYA_REDIS_URL"])
        self.addCleanup(client.close)
        prefix = "setrya-canary:" + secrets.token_hex(8)
        key, other = (f"{prefix}:{scope}:lost" for scope in ("trial", "other"))
        self.addCleanup(client.delete, key, other)
        client.mset({key: MARKER, other: MARKER})
        scan = RedisCanaryScanner(client, prefix=prefix)
        self.assertEqual(scan.find(scope="trial", marker=MARKER), {"lost"})
        client.delete(key)
        self.assertEqual(scan.find(scope="trial", marker=MARKER), set())
        self.assertEqual(scan.find(scope="other", marker=MARKER), {"lost"})
        client.hset(key, mapping={"body": MARKER})
        with self.assertRaises(ResponseError):
            scan.find(scope="trial", marker=MARKER)


@unittest.skipUnless(os.getenv("SETRYA_ELASTICSEARCH_PORT") and installed("elasticsearch"), "configure disposable Elasticsearch")
class ElasticTest(unittest.TestCase):
    def test_pit_cross_scope_untagged_and_missing_source(self):
        from elasticsearch import Elasticsearch
        client = Elasticsearch("http://127.0.0.1:" + os.environ["SETRYA_ELASTICSEARCH_PORT"])
        self.addCleanup(client.close)
        index = "setrya-canary-" + secrets.token_hex(8)
        client.indices.create(index=index, mappings={"properties": {"workspace": {"type": "keyword"},
                                                                   "payload": {"type": "object", "enabled": False}}})
        self.addCleanup(client.indices.delete, index=index)
        for scope in ("trial", "other"):
            client.index(index=index, id=scope, document={"workspace": scope, "payload": {"nested": [MARKER]}})
        scan = ElasticsearchCanaryScanner(client, index, scope_field="workspace", content_field="payload")
        self.assertEqual(scan.find(scope="trial", marker=MARKER), {"trial"})
        client.delete(index=index, id="trial")
        self.assertEqual(scan.find(scope="trial", marker=MARKER), set())
        self.assertEqual(scan.find(scope="other", marker=MARKER), {"other"})
        client.index(index=index, id="missing", document={"workspace": "trial"})
        with self.assertRaises(KeyError):
            scan.find(scope="trial", marker=MARKER)


class CacheCanaryTests(unittest.TestCase):
    def exercise(self, cache):
        from setrya.erasure.canary import AnswerCacheCanaryScanner
        from setrya.local_cache import CacheKey
        from setrya.store_signals import passage_metadata
        for tenant in ("trial", "other"):
            key = CacheKey(tenant, "reviewer", "room", "saved")
            self.assertTrue(cache.publish(cache.begin(key), MARKER, chunks=[passage_metadata("ticket", "1", "0")]))
        scanner = AnswerCacheCanaryScanner(cache, tenant="trial")
        self.assertEqual(scanner.find(scope="room", marker=MARKER), {"trial:reviewer:saved"})
        cache.invalidate_retrieval("trial", "room")
        self.assertEqual(scanner.find(scope="room", marker=MARKER), set())
        self.assertEqual(AnswerCacheCanaryScanner(cache, tenant="other").find(scope="room", marker=MARKER),
                         {"other:reviewer:saved"})

    def test_published_local_answers(self):
        from setrya.local_cache import LocalAnswerCache
        self.exercise(LocalAnswerCache())

    def test_local_scan_preserves_eviction_order(self):
        from setrya.erasure.canary import AnswerCacheCanaryScanner
        from setrya.local_cache import CacheKey, LocalAnswerCache
        from setrya.store_signals import passage_metadata

        cache = LocalAnswerCache(max_entries=2)
        keys = [CacheKey("trial", "reader", scope, "query") for scope in ("first", "second", "third")]
        chunks = [passage_metadata("ticket", "1", "0")]
        for key in keys[:2]:
            self.assertTrue(cache.publish(cache.begin(key), MARKER, chunks=chunks))
        scan = AnswerCacheCanaryScanner(cache, tenant="trial")
        self.assertEqual(scan.find(scope="first", marker=MARKER), {"trial:reader:query"})
        self.assertTrue(cache.publish(cache.begin(keys[2]), MARKER, chunks=chunks))
        self.assertIsNone(cache.get(keys[0]))
        self.assertEqual(cache.get(keys[1]).answer, MARKER)

    @unittest.skipUnless(os.getenv("SETRYA_REDIS_URL") and installed("redis"), "configure disposable Redis")
    def test_strict_redis_scan_reads_hidden_answer_and_checks_concurrent_scope_change(self):
        from redis import Redis, WatchError
        from setrya.erasure.canary import AnswerCacheCanaryScanner
        from setrya.local_cache import CacheKey
        from setrya.redis_cache import RedisAnswerCache
        from setrya.store_signals import passage_metadata

        client = Redis.from_url(os.environ["SETRYA_REDIS_URL"])
        self.addCleanup(client.close)
        namespace = "canary-" + secrets.token_hex(8)
        writer = RedisAnswerCache(client, namespace=namespace, ttl_seconds=60)
        observer = RedisAnswerCache(client, namespace=namespace, ttl_seconds=60, require_current_sources=True)
        key = CacheKey("trial", "reader", "room", "old")
        chunks = [passage_metadata("ticket", "1", "0")]
        self.assertTrue(writer.publish(writer.begin(key), MARKER, chunks=chunks))
        self.assertIsNone(observer.get(key))
        # Losing a reverse index does not erase the stored copy.
        client.delete(observer._prefix(key.tenant) + "retrieval:room")
        scan = AnswerCacheCanaryScanner(observer, tenant=key.tenant)
        self.assertEqual(scan.find(scope="room", marker=MARKER), {"trial:reader:old"})

        original = observer._answer_slots
        def during_scan(*args):
            yield from original(*args)
            new_key = CacheKey("trial", "reader", "room", "new")
            self.assertTrue(writer.publish(writer.begin(new_key), MARKER, chunks=chunks))

        with patch.object(observer, "_answer_slots", during_scan), self.assertRaises(WatchError):
            scan.find(scope="room", marker=MARKER)
        self.assertEqual(scan.find(scope="room", marker=MARKER), {"trial:reader:old", "trial:reader:new"})

    @unittest.skipUnless(os.getenv("SETRYA_REDIS_URL") and installed("redis"), "configure disposable Redis")
    def test_published_redis_answers(self):
        from redis import Redis
        from setrya.redis_cache import RedisAnswerCache
        client = Redis.from_url(os.environ["SETRYA_REDIS_URL"])
        self.addCleanup(client.close)
        self.exercise(RedisAnswerCache(client, namespace="canary-" + secrets.token_hex(8), ttl_seconds=60))
