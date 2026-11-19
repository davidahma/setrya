"""PostgreSQL retrieval, caller policy and a cache for one disposable workspace."""
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import uuid4

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from psycopg import sql
from psycopg.types.json import Jsonb

from setrya.dependencies import AnswerDependencies
from setrya.adapter import Publication, Receipt
from setrya.examples.pgvector_search.app import connection, schema_for
from setrya.integrations.langchain import retrieve_with_evidence, tag_documents
from setrya.store_signals import postgres_wal
from .contract import DELIVERY_QUERY, DELIVERY_TEXT, NEXT_TEXT, PRIVATE_TEXT, PUBLIC_TEXT, QUESTION, REFUND_QUERY, SCOPES

_DDL = """
CREATE TABLE access_policy (caller text PRIMARY KEY, granted boolean NOT NULL, epoch bigint NOT NULL);
CREATE TABLE catalog (scope text PRIMARY KEY, epoch bigint NOT NULL);
INSERT INTO catalog VALUES ('refund', 0), ('delivery', 0);
CREATE TABLE chunks (marker text PRIMARY KEY, body text NOT NULL, metadata jsonb NOT NULL,
                     private boolean NOT NULL, embedding public.vector(2) NOT NULL,
                     scope text NOT NULL REFERENCES catalog);
CREATE FUNCTION bump_catalog() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        UPDATE catalog SET epoch = epoch + 1;
    ELSIF TG_OP = 'INSERT' THEN
        UPDATE catalog SET epoch = epoch + 1 WHERE scope = NEW.scope;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE catalog SET epoch = epoch + 1 WHERE scope = OLD.scope;
    ELSE
        UPDATE catalog SET epoch = epoch + 1 WHERE scope IN (OLD.scope, NEW.scope);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER catalog_changed AFTER INSERT OR UPDATE OR DELETE ON chunks
    FOR EACH ROW EXECUTE FUNCTION bump_catalog();
CREATE TRIGGER catalog_cleared AFTER TRUNCATE ON chunks
    FOR EACH STATEMENT EXECUTE FUNCTION bump_catalog();
CREATE TABLE answers (caller text REFERENCES access_policy, question text, body text NOT NULL,
                      dependencies jsonb NOT NULL, writer text NOT NULL, PRIMARY KEY (caller, question));
CREATE TABLE captures (attempt text PRIMARY KEY, caller text NOT NULL, question text NOT NULL,
                       answer text NOT NULL, sources text[] NOT NULL, revision text NOT NULL, dependencies jsonb NOT NULL);
CREATE TABLE fills (attempt text PRIMARY KEY, revision text NOT NULL, outcome text NOT NULL, write_lsn pg_lsn);
"""


class PolicyRetriever(BaseRetriever):
    """Read the nearest visible chunks using the application's real permission filter."""
    session: Any
    caller: str

    def _get_relevant_documents(self, query: str) -> list[Document]:
        if query not in SCOPES:
            raise ValueError("unknown synthetic question")
        # The deterministic embedding ranks the synthetic answers within each scope.
        rows = self.session.execute("""
            SELECT body, metadata FROM chunks JOIN access_policy ON caller = %s
             WHERE scope = %s AND (granted OR NOT private)
             ORDER BY embedding <-> '[1,0]'::public.vector, marker LIMIT %s
        """, (self.caller, SCOPES[query], 2 if query == QUESTION else 1)).fetchall()
        return [Document(page_content=row["body"], metadata=row["metadata"]) for row in rows]


@dataclass(frozen=True)
class Capture:
    caller: str
    question: str
    answer: str
    dependencies: AnswerDependencies
    granted: bool

    @property
    def sources(self):
        return self.dependencies.markers

    @property
    def revision(self):
        return "granted" if self.granted else "revoked"


def _access(db, caller, question, *, lock=False):
    query = """SELECT a.granted, a.epoch AS acl_epoch, c.epoch AS catalog_epoch
                 FROM access_policy a CROSS JOIN catalog c WHERE a.caller = %s AND c.scope = %s"""
    return db.execute(query + (" FOR SHARE OF a, c" if lock else ""), (caller, SCOPES[question])).fetchone()


def _capture(db, tenant, caller, question, access):
    retriever = PolicyRetriever(session=db, caller=caller)
    docs, observed = retrieve_with_evidence(retriever, question, route="answer", scope=caller)
    if not docs or None in observed.passages:
        raise ValueError("missing identified answer sources")
    record = AnswerDependencies.record(
        answer_id=uuid4().hex, tenant=tenant, access_scope=caller, access_version=str(access["acl_epoch"]),
        retrieval_scope=SCOPES[question], retrieval_version=str(access["catalog_epoch"]),
        chunks=(d.metadata for d in docs),
    )
    return Capture(caller, question, " ".join(d.page_content for d in docs), record, access["granted"])


def _save(db, capture, writer):
    db.execute("""INSERT INTO answers VALUES (%s,%s,%s,%s,%s)
                  ON CONFLICT (caller,question) DO UPDATE SET
                    body=EXCLUDED.body,dependencies=EXCLUDED.dependencies,writer=EXCLUDED.writer""",
               (capture.caller, capture.question, capture.answer, Jsonb(capture.dependencies.to_dict()), writer))


def _current(record, tenant, caller, question, access):
    return ((record.tenant, record.access_scope, record.access_version, record.retrieval_scope, record.retrieval_version)
            == (tenant, caller, str(access["acl_epoch"]), SCOPES[question], str(access["catalog_epoch"])))


def _ingest(db, name, revision, text, private, vector, scope):
    chunk, = tag_documents([Document(page_content=text)], document=name, revision=revision)
    db.execute("INSERT INTO chunks VALUES (%s,%s,%s,%s,%s::public.vector,%s)",
               (chunk.metadata["setrya_marker"], chunk.page_content, Jsonb(chunk.metadata), private, vector, scope))


class Store:
    def __init__(self, run_id, mode):
        if mode not in ("broken", "guarded", "global"):
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
            _ingest(db, "policy", "A", PRIVATE_TEXT, True, "[0.8,0]", "refund")
            _ingest(db, "guide", "1", PUBLIC_TEXT, False, "[0,1]", "refund")
            _ingest(db, "delivery", "1", DELIVERY_TEXT, False, "[1,0]", "delivery")
            for caller, question in (("revoked", QUESTION), ("retained", QUESTION),
                                     ("retained", REFUND_QUERY), ("retained", DELIVERY_QUERY)):
                captured = _capture(db, self.run_id, caller, question, _access(db, caller, question))
                _save(db, captured, captured.dependencies.answer_id)
        self.created = True

    def capture(self, caller, question):
        with connection(self.schema) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return _capture(db, self.run_id, caller, question, _access(db, caller, question))

    def record_capture(self, capture, attempt):
        with connection(self.schema) as db:
            db.execute("INSERT INTO captures VALUES (%s,%s,%s,%s,%s,%s,%s)", (
                attempt, capture.caller, capture.question, capture.answer, list(capture.sources), capture.revision,
                Jsonb(capture.dependencies.to_dict())))

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

    def supersede_refund(self):
        # Insert a previously uncited source. All original chunks remain readable.
        with connection(self.schema) as db:
            _ingest(db, "new-policy", "B", NEXT_TEXT, True, "[1,0]", "refund")
            if self.mode == "global":
                db.execute("DELETE FROM answers")
            return {r["scope"]: str(r["epoch"]) for r in db.execute("SELECT scope, epoch FROM catalog")}

    def fill(self, capture, attempt):
        with connection(self.schema) as db:
            # Serialize the guard and insertion against both policy and source changes.
            now = _access(db, capture.caller, capture.question, lock=True)
            current = (_current(capture.dependencies, self.run_id, capture.caller, capture.question, now)
                       and capture.granted == now["granted"])
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
            access = _access(db, caller, question)
            cached = db.execute("SELECT * FROM answers WHERE caller=%s AND question=%s",
                                (caller, question)).fetchone()
            record = AnswerDependencies.from_dict(cached["dependencies"]) if cached else None
            hit = record is not None and (self.mode == "broken" or _current(
                record, self.run_id, caller, question, access))
            if hit and self.mode != "broken":
                rows = db.execute("""SELECT marker FROM chunks WHERE scope=%s AND marker = ANY(%s)
                                     AND (NOT private OR %s)""",
                                  (SCOPES[question], list(record.markers), access["granted"]))
                hit = bool(record.markers) and {r["marker"] for r in rows} == set(record.markers)
            if hit:
                body, writer, generation_skipped = cached["body"], cached["writer"], True
            else:
                fresh = _capture(db, self.run_id, caller, question, access)
                body, writer, generation_skipped = fresh.answer, None, False
                record = fresh.dependencies
            # Reading the receipt row in the answer's MVCC snapshot witnesses this
            # specific write. Sampling the primary's current WAL alone could not.
            row = db.execute("SELECT write_lsn::text FROM fills WHERE attempt=%s AND outcome='committed'",
                             (attempt,)).fetchone()
            commit = postgres_wal(row["write_lsn"], generation=self.run_id).commit if row else 0
        return {"caller": caller, "route": "answer", "query_sha256": sha256(question.encode()).hexdigest(),
                "answer": body, "sources": list(record.markers),
                "dependencies": record.to_dict(), "generation_skipped": generation_skipped,
                "cache_hit": hit, "cache_writer": writer, "generation": self.run_id, "commit": commit}

    def close(self):
        if self.created:
            with connection(self.schema) as db:
                db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
            self.created = False
