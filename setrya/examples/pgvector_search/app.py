"""Disposable queue, versioned source records and real pgvector retrieval."""

from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys
from time import monotonic, sleep

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from setrya.examples.support_search.app import ATTEMPT, CHECKPOINT, DELIVERY, REFUND_A, REFUND_B, ROUTE, SCOPE
from setrya.store_signals import postgres_wal
from . import MODULE

_ENV = "SETRYA_PGVECTOR_DSN"
_RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
_OLD = (("refund@A", REFUND_A, "[1,0,0]"),)
_NEW = (("refund@B", REFUND_B, "[1,0.05,0]"),)
_REUPLOAD = _NEW + (
    ("refund@B.2", "Include the original order receipt with your return.", "[1,0.13,0]"),
    ("refund@B.3", "Refunds go to the original payment method.", "[1,0.23,0]"),
)
_DELIVERY = (("delivery@1", DELIVERY, "[0,1,0]"),)

_DDL = """
CREATE TABLE source (document text PRIMARY KEY, revision text NOT NULL, upload_id text UNIQUE NOT NULL,
                     passages jsonb NOT NULL);
CREATE TABLE queue (attempt text PRIMARY KEY, status text NOT NULL, revision text, write_lsn pg_lsn);
CREATE TABLE vector_index (upload_id text, slot integer, marker text, body text, origin text,
                           embedding public.vector(3) NOT NULL, PRIMARY KEY (upload_id, slot));
CREATE TABLE delayed_view (LIKE vector_index INCLUDING ALL);
"""


def schema_for(run_id):
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid fixture run ID")
    return f"setrya_{run_id}"


def connection_settings():
    params = conninfo_to_dict(os.environ.get(_ENV, ""))
    if (not {"host", "dbname", "user"} <= params.keys()
            or params.keys() - {"host", "port", "dbname", "user", "password", "sslmode"}
            or params["host"] != "127.0.0.1"):
        raise RuntimeError(f"{_ENV} must identify a disposable PostgreSQL database on 127.0.0.1")
    return params


@contextmanager
def connection(schema):
    try:
        with psycopg.connect(
            **connection_settings(), hostaddr="127.0.0.1", connect_timeout=2, row_factory=dict_row,
            options=f"-c search_path={schema},public -c statement_timeout=2000 -c lock_timeout=1000",
        ) as db:
            yield db
    except psycopg.Error as error:
        # The HTTP boundary exposes a fixed diagnostic and no driver error body.
        raise RuntimeError("PostgreSQL fixture operation failed") from error


def _source(db, document, revision, upload, passages):
    db.execute("INSERT INTO source VALUES (%s, %s, %s, %s::jsonb)",
               (document, revision, upload, json.dumps(passages)))


def _index(db, upload, passages, origin):
    db.execute("DELETE FROM vector_index WHERE upload_id = %s", (upload,))
    with db.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO vector_index VALUES (%s, %s, %s, %s, %s, %s::public.vector)",
            [(upload, slot, marker, body, origin, embedding)
             for slot, (marker, body, embedding) in enumerate(passages, start=1)],
        )


class App:
    def __init__(self, directory, run_id, mode, scenario):
        self.directory = directory
        self.run_id = run_id
        self.schema = schema_for(run_id)
        self.mode = mode
        self.scenario = scenario
        self.published = False
        self.created = False
        self.process = None

    def seed(self):
        with connection(self.schema) as db:
            if db.execute("SELECT to_regtype('public.vector') AS extension").fetchone()["extension"] is None:
                raise RuntimeError("Enable vector in the disposable database before running the fixture")
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            db.execute(_DDL)
            _source(db, "refund", "A", "upload-a", _OLD)
            _source(db, "delivery", "1", "delivery", _DELIVERY)
            _index(db, "upload-a", _OLD, "seed")
            _index(db, "delivery", _DELIVERY, "seed")
        self.created = True

    def start_old(self):
        with connection(self.schema) as db:
            db.execute("INSERT INTO queue (attempt, status) VALUES (%s, 'queued')", (ATTEMPT,))
        self.process = subprocess.Popen(
            [sys.executable, "-m", MODULE, "worker", str(self.directory),
             self.run_id, self.mode, self.scenario],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return ATTEMPT

    def _job(self):
        if self.process and self.process.poll() not in {None, 0}:
            raise RuntimeError("PostgreSQL fixture worker exited")
        with connection(self.schema) as db:
            return db.execute("SELECT status, revision, write_lsn::text FROM queue WHERE attempt = %s", (ATTEMPT,)).fetchone()

    def _identity(self, revision):
        return {"run_id": self.run_id, "document": "refund", "revision": revision}

    def checkpoint(self):
        job = self._job()
        if job["status"] == "held":
            return {**self._identity(job["revision"]), "attempt": ATTEMPT, "name": CHECKPOINT}
        return None

    def publish(self):
        with connection(self.schema) as db:
            if self.scenario == "reupload":
                db.execute("DELETE FROM source WHERE document = 'refund'")
                db.execute("DELETE FROM vector_index WHERE upload_id = 'upload-a'")
                _source(db, "refund", "B", "upload-b", _REUPLOAD)
                _index(db, "upload-b", _REUPLOAD, "publish")
            else:
                db.execute("UPDATE source SET revision = 'B', passages = %s::jsonb WHERE document = 'refund'",
                           (json.dumps(_NEW),))
                _index(db, "upload-a", _NEW, "publish")
            if self.mode == "opaque":
                db.execute("INSERT INTO delayed_view SELECT * FROM vector_index")
            published = db.execute("SELECT document, revision FROM source WHERE document = 'refund'").fetchone()
        self.published = True
        return {"run_id": self.run_id, **published}

    def release(self):
        with connection(self.schema) as db:
            changed = db.execute("UPDATE queue SET status = 'released' WHERE attempt = %s AND status = 'held'",
                                 (ATTEMPT,)).rowcount
            if changed != 1:
                raise RuntimeError("release requires a held worker")

    def receipt(self):
        job = self._job()
        if job["status"] not in {"committed", "rejected"}:
            return None
        return {**self._identity(job["revision"]), "attempt": ATTEMPT, "outcome": job["status"],
                "generation": self.run_id if job["status"] == "committed" else None,
                "commit": postgres_wal(job["write_lsn"], generation=self.run_id).commit if job["write_lsn"] else None}

    def search(self, question, route, scope, top_k):
        if route != ROUTE or scope != SCOPE or not 1 <= top_k <= 100:
            raise ValueError("invalid search route, scope or top_k")
        words = set(re.findall(r"[a-z]+", question.lower()))
        embedding = "[0,1,0]" if "delivery" in words else "[1,0,0]" if words & {"return", "refund"} else "[0,0,1]"
        delayed = self.mode == "opaque" and self.published
        index = sql.Identifier("delayed_view" if delayed else "vector_index")
        with connection(self.schema) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # This row is published only after the index write commits. Reading it
            # and the hits in one snapshot excludes replica lag and concurrent writes.
            job = db.execute("SELECT write_lsn::text FROM queue WHERE attempt = %s AND status = 'committed'",
                             (ATTEMPT,)).fetchone()
            position = postgres_wal(job["write_lsn"], generation=self.run_id) if job else None
            # The top-k limit applies before the join that filters deleted uploads.
            rows = db.execute(sql.SQL("""
                WITH candidates AS MATERIALIZED (
                    SELECT upload_id, slot, marker, body, origin,
                           embedding <-> %(query)s::public.vector AS distance
                    FROM {}
                    WHERE embedding <-> %(query)s::public.vector < 0.5
                    ORDER BY distance, upload_id, slot LIMIT %(depth)s
                )
                SELECT marker AS id, body AS text, origin
                FROM candidates JOIN source USING (upload_id)
                ORDER BY distance, upload_id, slot
            """).format(index), {"query": embedding, "depth": top_k}).fetchall()
        return {
            "route": route, "scope": scope, "hits": rows,
            "view": None if delayed or position is None else {"generation": position.generation, "commit": position.commit,
                                                             "route": route, "scope": scope},
        }

    def close(self):
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
        finally:
            if self.created:
                with connection(self.schema) as db:
                    db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
                self.created = False


def work(run_id, mode):
    schema = schema_for(run_id)
    with connection(schema) as db:
        snapshot = db.execute("SELECT * FROM source WHERE document = 'refund'").fetchone()
        db.execute("UPDATE queue SET status = 'held', revision = %s WHERE attempt = %s AND status = 'queued'",
                   (snapshot["revision"], ATTEMPT))

    deadline = monotonic() + 15
    while monotonic() < deadline:
        with connection(schema) as db:
            job = db.execute("SELECT status FROM queue WHERE attempt = %s", (ATTEMPT,)).fetchone()
        if job["status"] == "released":
            break
        sleep(0.02)
    else:
        raise RuntimeError("old PostgreSQL worker exceeded its release deadline")

    with connection(schema) as db:
        current = db.execute("SELECT revision, upload_id FROM source WHERE document = 'refund' FOR UPDATE").fetchone()
        if mode == "fixed" and (
            current is None or (current["revision"], current["upload_id"]) != (snapshot["revision"], snapshot["upload_id"])
        ):
            db.execute("UPDATE queue SET status = 'rejected' WHERE attempt = %s", (ATTEMPT,))
            return
        _index(db, snapshot["upload_id"], snapshot["passages"], ATTEMPT)
    # A position sampled inside the write transaction could precede its commit
    # record. Publish the durable job receipt in a subsequent transaction.
    with connection(schema) as db:
        db.execute("UPDATE queue SET status = 'committed', write_lsn = pg_current_wal_insert_lsn() WHERE attempt = %s",
                   (ATTEMPT,))
