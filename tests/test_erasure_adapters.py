"""Conformance across native storage clients; live tests use disposable, isolated fixtures."""

from __future__ import annotations

from contextlib import closing
import importlib.util
import os
import secrets
import sqlite3
import unittest

from setrya.drill import InputError
from setrya.erasure.adapters import (
    ElasticsearchAdapter, LangGraphMemoryAdapter, LangGraphPostgresCheckpointAdapter,
    LangGraphPostgresMemoryAdapter, LangGraphSQLiteCheckpointAdapter, PgvectorAdapter,
    RedisAdapter, SQLiteAdapter, checkpoint_thread, memory_item_key, memory_namespace, redis_key,
)
from setrya.erasure.conformance import check_erasure_adapter
from setrya.erasure.subject import Subject


def installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def dict_row(cursor, row):
    return {column[0]: value for column, value in zip(cursor.description, row)}


class Fixture(unittest.TestCase):
    def setUp(self):
        secret = secrets.token_bytes(32)
        self.a = Subject.derive(secret, "tenant-a", "same-test-person", "restricted")
        self.b = Subject.derive(secret, "tenant-b", "same-test-person", "restricted")

    def exercise(self, adapter, keys=("a", "b"), others=("a",)):
        check_erasure_adapter(adapter, subject=self.a, retained=self.b, expected=set(keys), retained_expected=set(others))


class SQLiteTests(Fixture):
    def test_sqlite_conformance_scopes_and_errors(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE docs (id, setrya_subject, payload)")
            with db:
                db.executemany("INSERT INTO docs VALUES(?, ?, 'fixture')", [
                    ("a", self.a.ref), ("b", self.a.ref), ("a", self.b.ref)
                ])
            with self.assertRaises(sqlite3.OperationalError):
                SQLiteAdapter(db, "docs", subject_column="missing").verify_absent(self.a)
            adapter = SQLiteAdapter(db, "docs")
            self.assertEqual(adapter.find(self.a), {"a", "b"})
            db.row_factory = dict_row
            self.exercise(adapter)
            self.assertEqual(db.execute("SELECT payload FROM docs").fetchall(), [{"payload": "fixture"}])
            with self.assertRaises(InputError):
                SQLiteAdapter(db, "main.docs")

    def test_open_transactions_and_duplicate_ids_are_inconclusive(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE docs(id, setrya_subject)")
            adapter = SQLiteAdapter(db, "docs")
            for _ in range(2):
                db.execute("INSERT INTO docs VALUES('duplicate', ?)", (self.a.ref,))
            with self.assertRaisesRegex(ValueError, "committed"):
                adapter.delete(self.a)
            db.commit()
            with self.assertRaisesRegex(ValueError, "multiple store rows"):
                adapter.find(self.a)


@unittest.skipUnless(installed("langgraph.checkpoint.sqlite"), "install the LangGraph SQLite extra")
class SQLiteCheckpointTests(Fixture):
    def test_orphan_pending_writes_and_subject_isolation(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        with closing(sqlite3.connect(":memory:")) as db:
            saver = SqliteSaver(db)
            saver.setup()
            for person in (self.a, self.b):
                thread = checkpoint_thread(person, "same-chat")
                config = {"configurable": {"thread_id": thread, "checkpoint_ns": "", "checkpoint_id": "fixture"}}
                saver.put_writes(config, [("chart", "fixture")], "pending")
            self.assertEqual(db.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 0)
            adapter = LangGraphSQLiteCheckpointAdapter(saver)
            self.assertEqual(adapter.find(self.a), {checkpoint_thread(self.a, "same-chat")})
            db.row_factory = dict_row
            self.exercise(adapter, [checkpoint_thread(self.a, "same-chat")],
                          [checkpoint_thread(self.b, "same-chat")])


@unittest.skipUnless(installed("langgraph.store.memory"), "install LangGraph")
class MemoryTests(Fixture):
    def test_paging_and_controls(self):
        from langgraph.store.memory import InMemoryStore

        store = InMemoryStore()
        keys = {str(n) for n in range(134)}
        for key in keys:
            store.put(memory_namespace(self.a), key, {"chart": "fixture"})
        store.put(memory_namespace(self.b), "1", {"chart": "control"})
        self.exercise(LangGraphMemoryAdapter(store), keys, ["1"])

    def test_nested_items_with_the_same_key_remain_distinguishable(self):
        from langgraph.store.memory import InMemoryStore

        store = InMemoryStore()
        nested = (*memory_namespace(self.a), "nested")
        store.put(nested, "copy", {"chart": "fixture"})
        for subject in (self.a, self.b):
            store.put(memory_namespace(subject), "copy", {"chart": "fixture"})
        self.exercise(LangGraphMemoryAdapter(store), ["copy", memory_item_key(self.a, nested, "copy")], ["copy"])


@unittest.skipUnless(os.getenv("SETRYA_REDIS_URL") and installed("redis"), "configure a disposable Redis service")
class RedisTests(Fixture):
    def test_scan_and_idempotent_scoped_deletion(self):
        import redis

        store = redis.Redis.from_url(os.environ["SETRYA_REDIS_URL"])
        self.addCleanup(store.close)
        copies = [(self.a, "a"), (self.a, "b"), (self.b, "a")]
        keys = [redis_key(p, k) for p, k in copies]
        self.addCleanup(store.delete, *keys)
        for key in keys:
            store.set(key, "fixture")
        self.exercise(RedisAdapter(store))
        self.assertEqual(store.get(keys[-1]), b"fixture")


@unittest.skipUnless(os.getenv("SETRYA_ELASTICSEARCH_PORT") and installed("elasticsearch"), "configure disposable ES 8")
class ElasticsearchTests(Fixture):
    def setUp(self):
        super().setUp()
        from elasticsearch import Elasticsearch

        self.client = Elasticsearch("http://127.0.0.1:" + os.environ["SETRYA_ELASTICSEARCH_PORT"])
        self.addCleanup(self.client.close)
        self.index = "setrya-erasure-" + secrets.token_hex(10)
        self.client.indices.create(index=self.index, settings={"number_of_shards": 3, "number_of_replicas": 0}, mappings={"properties": {"metadata": {
            "properties": {"setrya_subject": {"type": "keyword"}}
        }}})
        self.addCleanup(self.client.indices.delete, index=self.index)
        self.adapter = ElasticsearchAdapter(self.client, self.index, subject_field="metadata.setrya_subject")

    def write(self, key, subj, **options):
        self.client.index(index=self.index, id=key, document={"metadata": {"setrya_subject": subj}}, **options)

    def test_paging_unrefreshed_writes_and_retained_subject(self):
        keys = {str(n) for n in range(131)}
        ops = []
        for key in sorted(keys):
            ops.extend([{"index": {"_index": self.index, "_id": key}}, {"metadata": {"setrya_subject": self.a.ref}}])
        self.assertFalse(self.client.bulk(operations=ops)["errors"])
        self.write("retained", self.b.ref, routing="custom-routing")
        self.write("routed", self.a.ref, routing="custom-routing")
        self.exercise(self.adapter, keys | {"routed"}, {"retained"})

    def test_shared_document_and_alias_fail_closed(self):
        self.write("shared", [self.a.ref, self.b.ref])
        with self.assertRaisesRegex(ValueError, "shared"):
            self.adapter.delete(self.a)
        self.assertTrue(self.client.exists(index=self.index, id="shared"))
        alias = self.index + "-alias"
        self.client.indices.put_alias(index=self.index, name=alias)
        with self.assertRaisesRegex(ValueError, "concrete"):
            ElasticsearchAdapter(self.client, alias).delete(self.a)


@unittest.skipUnless(os.getenv("SETRYA_PGVECTOR_DSN") and installed("psycopg"), "configure disposable pgvector")
class PostgreSQLTests(Fixture):
    def setUp(self):
        super().setUp()
        import psycopg
        from psycopg.rows import dict_row

        self.db = psycopg.connect(os.environ["SETRYA_PGVECTOR_DSN"], autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        schema = "test_erasure_" + secrets.token_hex(10)
        self.db.execute("CREATE SCHEMA " + schema)
        self.addCleanup(self.db.execute, "DROP SCHEMA " + schema + " CASCADE")
        self.db.execute("SET search_path TO " + schema + ", public")

    def test_pgvector_jsonb_reads_rows(self):
        from psycopg.types.json import Jsonb

        self.db.execute("CREATE TABLE documents(id text, embedding vector(2), metadata jsonb)")
        for key, subject in (("a", self.a), ("b", self.a), ("a", self.b)):
            self.db.execute("INSERT INTO documents VALUES (%s, '[1,2]', %s)", (key, Jsonb(subject.metadata())))
        self.exercise(PgvectorAdapter(self.db, "documents", metadata_column="metadata"))
        self.assertEqual(self.db.execute("SELECT metadata FROM documents").fetchone()["metadata"], self.b.metadata())

    def test_shared_document_fails_closed(self):
        from psycopg.types.json import Jsonb

        self.db.execute("CREATE TABLE documents(id text, metadata jsonb)")
        self.db.execute("INSERT INTO documents VALUES('mixed', %s)",
                        (Jsonb({"setrya_subject": [self.a.ref, self.b.ref]}),))
        with self.assertRaisesRegex(ValueError, "shared"):
            PgvectorAdapter(self.db, "documents", metadata_column="metadata").delete(self.a)
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM documents").fetchone()["n"], 1)

    @unittest.skipUnless(installed("langgraph.checkpoint.postgres"), "install LangGraph PostgreSQL")
    def test_postgres_checkpoint_orphan_blobs_and_writes(self):
        from langgraph.checkpoint.postgres import PostgresSaver

        saver = PostgresSaver(self.db)
        saver.setup()
        for person in (self.a, self.b):
            thread = checkpoint_thread(person, "write")
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": "", "checkpoint_id": "fixture"}}
            saver.put_writes(config, [("chart", "test")], "pending")
            self.db.execute("""INSERT INTO checkpoint_blobs(thread_id,checkpoint_ns,channel,version,type,blob)
                               VALUES (%s,'','chart','1','bytes',%s)""", (checkpoint_thread(person, "blob"), b"fixture"))
        self.exercise(LangGraphPostgresCheckpointAdapter(saver),
                      [checkpoint_thread(self.a, s) for s in ("write", "blob")],
                      [checkpoint_thread(self.b, s) for s in ("write", "blob")])

    @unittest.skipUnless(installed("langgraph.store.postgres"), "install LangGraph PostgreSQL")
    def test_postgres_long_term_memory_expired_and_embedded_items(self):
        from langgraph.store.postgres import PostgresStore

        store = PostgresStore(self.db, index={"dims": 2, "embed": lambda texts: [[1., 2.] for _ in texts]})
        store.setup()
        for key, subject in (("a", self.a), ("b", self.a), ("a", self.b)):
            store.put(memory_namespace(subject), key, {"fact": "fictional"})
        nested = (*memory_namespace(self.a), "notes")
        store.put(nested, "a", {"chart": "fixture"})
        namespace = ".".join(memory_namespace(self.a))
        self.db.execute("UPDATE store SET expires_at = NOW() - INTERVAL '1 hour' WHERE prefix = %s", (namespace,))
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM store_vectors").fetchone()["n"], 4)
        self.exercise(LangGraphPostgresMemoryAdapter(store), ["a", "b", memory_item_key(self.a, nested, "a")])
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM store_vectors").fetchone()["n"], 1)
