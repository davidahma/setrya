"""PostgreSQL retrieval, caller policy and a cache for one disposable workspace."""
from dataclasses import dataclass
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from psycopg import sql
from psycopg.types.json import Jsonb

from setrya.adapter import Publication, Receipt
from setrya.examples.pgvector_search.app import connection, schema_for
from setrya.integrations.langchain import retrieve_with_evidence, tag_documents
from setrya.store_signals import postgres_wal
from .contract import PRIVATE_TEXT, PUBLIC_TEXT, QUESTION

_DDL = """
CREATE TABLE access_policy (caller text PRIMARY KEY, granted boolean NOT NULL, epoch bigint NOT NULL);
CREATE TABLE catalog (singleton boolean PRIMARY KEY CHECK (singleton), epoch bigint NOT NULL);
INSERT INTO catalog VALUES (true, 0);
CREATE TABLE chunks (marker text PRIMARY KEY, body text NOT NULL, metadata jsonb NOT NULL,
                     private boolean NOT NULL, embedding public.vector(2) NOT NULL);
CREATE FUNCTION bump_catalog() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN UPDATE catalog SET epoch = epoch + 1; RETURN NULL; END $$;
CREATE TRIGGER catalog_changed AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON chunks
    FOR EACH STATEMENT EXECUTE FUNCTION bump_catalog();
CREATE TABLE answers (caller text REFERENCES access_policy, question text, body text NOT NULL,
                      sources text[] NOT NULL, acl_epoch bigint NOT NULL, catalog_epoch bigint NOT NULL,
                      writer text NOT NULL, PRIMARY KEY (caller, question));
CREATE TABLE captures (attempt text PRIMARY KEY, caller text NOT NULL, question text NOT NULL,
                       answer text NOT NULL, sources text[] NOT NULL, revision text NOT NULL);
CREATE TABLE fills (attempt text PRIMARY KEY, revision text NOT NULL, outcome text NOT NULL, write_lsn pg_lsn);
"""


class PolicyRetriever(BaseRetriever):
    """Read the two nearest visible chunks using the application's real permission filter."""
    session: Any
    caller: str

    def _get_relevant_documents(self, query: str) -> list[Document]:
        if query != QUESTION:
            raise ValueError("unknown synthetic question")
        # The deterministic embedding represents the one question in this fixture.
        rows = self.session.execute("""
            SELECT body, metadata FROM chunks JOIN access_policy ON caller = %s
             WHERE granted OR NOT private
             ORDER BY embedding <-> '[1,0]'::public.vector, marker LIMIT 2
        """, (self.caller,)).fetchall()
        return [Document(page_content=row["body"], metadata=row["metadata"]) for row in rows]


@dataclass(frozen=True)
class Capture:
    caller: str
    question: str
    answer: str
    sources: tuple[str, ...]
    acl_epoch: int
    catalog_epoch: int
    granted: bool

    @property
    def revision(self):
        return "granted" if self.granted else "revoked"


def _access(db, caller, *, lock=False):
    query = """SELECT a.granted, a.epoch AS acl_epoch, c.epoch AS catalog_epoch
                 FROM access_policy a CROSS JOIN catalog c WHERE a.caller = %s"""
    return db.execute(query + (" FOR SHARE OF a, c" if lock else ""), (caller,)).fetchone()


def _capture(db, caller, question, access):
    retriever = PolicyRetriever(session=db, caller=caller)
    docs, observed = retrieve_with_evidence(retriever, question, route="answer", scope=caller)
    if not docs or None in observed.passages:
        raise ValueError("missing identified answer sources")
    return Capture(caller, question, " ".join(d.page_content for d in docs), observed.passages, **access)


def _save(db, capture, writer):
    db.execute("""INSERT INTO answers VALUES (%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (caller,question) DO UPDATE SET
                    body=EXCLUDED.body,sources=EXCLUDED.sources,acl_epoch=EXCLUDED.acl_epoch,
                    catalog_epoch=EXCLUDED.catalog_epoch,writer=EXCLUDED.writer""",
               (capture.caller, capture.question, capture.answer, list(capture.sources),
                capture.acl_epoch, capture.catalog_epoch, writer))


class Store:
    def __init__(self, run_id, mode):
        if mode not in ("broken", "guarded"):
            raise ValueError("unknown cache mode")
        self.run_id = run_id
        self.schema = schema_for(run_id) + "_answers"
        self.mode = mode
        self.created = False

    def seed(self):
        with connection(self.schema) as db:
            if db.execute("SELECT to_regtype('public.vector') AS t").fetchone()["t"] is None:
                raise RuntimeError("enable pgvector in the disposable database")
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            db.execute(_DDL)
            db.execute("INSERT INTO access_policy VALUES ('revoked', true, 1), ('retained', true, 1)")
            for name, rev, text, private, vector in (
                ("policy", "A", PRIVATE_TEXT, True, "[1,0]"),
                ("guide", "1", PUBLIC_TEXT, False, "[0,1]"),
            ):
                chunk, = tag_documents([Document(page_content=text)], document=name, revision=rev)
                db.execute("INSERT INTO chunks VALUES (%s,%s,%s,%s,%s::public.vector)",
                           (chunk.metadata["setrya_marker"], chunk.page_content, Jsonb(chunk.metadata), private, vector))
            for caller in ("revoked", "retained"):
                _save(db, _capture(db, caller, QUESTION, _access(db, caller)), "seed")
        self.created = True

    def capture(self, caller, question):
        with connection(self.schema) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return _capture(db, caller, question, _access(db, caller))

    def record_capture(self, capture, attempt):
        with connection(self.schema) as db:
            db.execute("INSERT INTO captures VALUES (%s,%s,%s,%s,%s,%s)", (
                attempt, capture.caller, capture.question, capture.answer, list(capture.sources), capture.revision))

    def captured(self, attempt):
        with connection(self.schema) as db:
            row = db.execute("SELECT * FROM captures WHERE attempt=%s", (attempt,)).fetchone()
        return dict(run_id=self.run_id, **row) if row else None

    def revoke(self):
        with connection(self.schema) as db:
            row = db.execute("""UPDATE access_policy SET granted=false, epoch=epoch+1
                                WHERE caller='revoked' AND granted RETURNING granted""").fetchone()
            if row is None:
                raise RuntimeError("caller already revoked")
            db.execute("DELETE FROM answers WHERE caller='revoked'")
        return Publication(self.run_id, "policy", "revoked")

    def fill(self, capture, attempt):
        with connection(self.schema) as db:
            # Serialize the guard and insertion against both policy and source changes.
            now = _access(db, capture.caller, lock=True)
            current = ((capture.acl_epoch, capture.catalog_epoch, capture.granted)
                       == (now["acl_epoch"], now["catalog_epoch"], now["granted"]))
            if self.mode == "broken" or current:
                _save(db, capture, attempt)
                db.execute("INSERT INTO fills VALUES (%s, %s, 'committed', pg_current_wal_insert_lsn())",
                           (attempt, capture.revision))
            else:
                db.execute("INSERT INTO fills VALUES (%s, %s, 'rejected', NULL)", (attempt, capture.revision))

    def receipt(self, attempt):
        with connection(self.schema) as db:
            row = db.execute("SELECT revision, outcome, write_lsn::text FROM fills WHERE attempt=%s",
                             (attempt,)).fetchone()
        if row is None:
            return None
        token = row["write_lsn"]
        commit = postgres_wal(token, generation=self.run_id).commit if token is not None else None
        return Receipt(self.run_id, attempt, "policy", row["revision"], row["outcome"],
                       self.run_id if commit is not None else None, commit)

    def answer(self, caller, question, attempt):
        with connection(self.schema) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            access = _access(db, caller)
            cached = db.execute("SELECT * FROM answers WHERE caller=%s AND question=%s",
                                (caller, question)).fetchone()
            hit = cached is not None and (self.mode == "broken" or (
                cached["acl_epoch"] == access["acl_epoch"] and cached["catalog_epoch"] == access["catalog_epoch"]
            ))
            if hit and self.mode == "guarded":
                # Verify the actual cited sources even if a stale entry was
                # incorrectly stamped with the current access-policy epoch.
                rows = db.execute("""SELECT marker FROM chunks WHERE marker = ANY(%s)
                                     AND (NOT private OR %s)""", (cached["sources"], access["granted"]))
                hit = bool(cached["sources"]) and {r["marker"] for r in rows} == set(cached["sources"])
            if hit:
                body, sources, writer = cached["body"], cached["sources"], cached["writer"]
            else:
                fresh = _capture(db, caller, question, access)
                body, sources, writer = fresh.answer, list(fresh.sources), None
            # Reading the receipt row in the answer's MVCC snapshot witnesses this
            # specific write. Sampling the primary's current WAL alone could not.
            row = db.execute("SELECT write_lsn::text FROM fills WHERE attempt=%s AND outcome='committed'",
                             (attempt,)).fetchone()
            commit = postgres_wal(row["write_lsn"], generation=self.run_id).commit if row else 0
        return {"caller": caller, "route": "answer", "answer": body, "sources": sources,
                "cache_hit": hit, "cache_writer": writer, "generation": self.run_id, "commit": commit}

    def close(self):
        if self.created:
            with connection(self.schema) as db:
                db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
            self.created = False
