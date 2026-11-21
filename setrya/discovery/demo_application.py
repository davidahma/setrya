"""Disposable catalog trial app using the public v0.17 investigation fixture on SQLite."""

from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from setrya.erasure.investigation import (
    ALICE_KEY, BOB_KEY, CONTROL_FACT, CONVERSATION, TARGET_FACT, Finding, Investigation, _graph, _summarize,
)
from setrya.erasure.hooks import CopyHooks
from setrya.erasure.lineage import VersionRef
from setrya.erasure.v3 import Stored


CACHE_STORE = "analyst-cache"
CACHE_KEY = "handoff-41"


class CatalogInvestigation(Investigation):
    """An analyst handoff cache receives the same shared summary as the assistant."""

    def __init__(self, folder, subject, control, journal, *, guarded):
        super().__init__(folder, subject, control, journal, guarded=guarded)
        self.db.execute("ATTACH DATABASE ? AS analyst", (str(Path(folder) / "analyst.sqlite"),))
        self.db.execute("CREATE TABLE analyst.handoffs (id TEXT PRIMARY KEY, revision TEXT, note TEXT)")

    def seed(self):
        with self.lock, self.db:
            for subject, key, kind, value in (
                (self.subject, ALICE_KEY, "device", TARGET_FACT),
                (self.control, BOB_KEY, "credential", CONTROL_FACT),
            ):
                self.db.execute("INSERT INTO findings VALUES (?,?,?,?,?,?)",
                                (key, subject.ref, "1", kind, value, value))
                self.versions.record(VersionRef("source", key, "1"), subjects=[subject])
                self.journal.record(subject, store="source", key=key, source=key, action="stored")
            self._classify_source()
            self._save_summary(self._findings())

    def _classify_source(self):
        version, text = self.db.execute("SELECT revision,raw_text FROM findings WHERE id=?", (ALICE_KEY,)).fetchone()
        request = {"model": "fixture-classifier-v1", "messages": [{"role": "user", "content": text}]}

        def classify(*, model, messages):
            # A local stand-in consumes the actual submitted text. No provider account is contacted.
            return "device" if TARGET_FACT in messages[0]["content"] else "unknown"

        client = SimpleNamespace(messages=SimpleNamespace(create=classify))
        kind = CopyHooks(self.journal, self.subject).anthropic(
            client, request, store="model-provider", key="sample-invocation", source=ALICE_KEY,
            sources=[f"{ALICE_KEY}@{version}"], prompt_caching=False,
        )
        self.db.execute("UPDATE findings SET kind=? WHERE id=?", (kind, ALICE_KEY))
        self.versions.record(VersionRef("model-provider", "sample-invocation", "1"),
                             inputs=[VersionRef("source", ALICE_KEY, version)])

    def _findings(self):
        rows = self.db.execute("SELECT id,revision,kind,value FROM findings WHERE id IN (?,?) ORDER BY id",
                               (ALICE_KEY, BOB_KEY))
        return tuple(Finding(*row) for row in rows)

    def erase_approved(self, subject, source, actions):
        from langgraph.checkpoint.sqlite import SqliteSaver

        self._check_subject(subject)
        required = {("source", ALICE_KEY, "delete"), ("source", BOB_KEY, "preserve"),
                    ("summaries", CONVERSATION, "rewrite"), ("answers", CONVERSATION, "rewrite"),
                    ("checkpoints", CONVERSATION, "rewrite"), (CACHE_STORE, CACHE_KEY, "rewrite")}
        if (source["store"], source["key"], source["action"]) != ("source", ALICE_KEY, "delete"):
            return False
        if not required.issubset({(a["store"], a["key"], a["action"]) for a in actions}):
            return False

        revision = uuid4().hex
        with self.lock, closing(sqlite3.connect(":memory:", check_same_thread=False)) as rebuilt:
            self.db.execute("ATTACH DATABASE ? AS history", (str(self.graph_path),))
            try:
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    deleted = self.db.execute("DELETE FROM findings WHERE id=? AND subject=? AND revision=?",
                                              (ALICE_KEY, subject.ref, source["revision"]))
                    if deleted.rowcount != 1:
                        return False
                    retained = self._findings()
                    if len(retained) != 1 or retained[0].key != BOB_KEY:
                        raise ValueError("The retained investigation source is missing.")
                    note = _summarize(retained)
                    # Build the new LangGraph thread in isolation. Its saver commits internally.
                    # Install the resulting rows with the source edit, summary and both caches.
                    _graph(SqliteSaver(rebuilt)).invoke(
                        {"conversation": CONVERSATION, "summary": note, "revision": revision}, config=self.config,
                    )
                    for table in ("checkpoints", "writes"):
                        self.db.execute(f"DELETE FROM history.{table} WHERE thread_id=?", (CONVERSATION,))
                        rows = rebuilt.execute(f"SELECT * FROM {table} WHERE thread_id=?", (CONVERSATION,))
                        columns = ",".join(column[0] for column in rows.description)
                        slots = ",".join("?" for _ in rows.description)
                        self.db.executemany(f"INSERT INTO history.{table}({columns}) VALUES({slots})", rows)
                    self.db.execute("UPDATE investigations SET revision=?,summary=? WHERE id=?",
                                    (revision, note, CONVERSATION))
                    self.db.execute("UPDATE answers SET revision=?,source_revision=?,note=? WHERE conversation=?",
                                    (revision, revision, note, CONVERSATION))
                    self.db.execute("UPDATE analyst.handoffs SET revision=?,note=? WHERE id=?", (revision, note, CACHE_KEY))
            finally:
                self.db.execute("DETACH DATABASE history")
            summary = VersionRef("summaries", CONVERSATION, revision)
            self.versions.record(summary, inputs=[f.version() for f in retained])
            for store, key in (("checkpoints", CONVERSATION), ("answers", CONVERSATION), (CACHE_STORE, CACHE_KEY)):
                self.versions.record(VersionRef(store, key, revision), inputs=[summary])
        return True

    def _save_summary(self, captured):
        super()._save_summary(captured)
        revision, note = self.db.execute(
            "SELECT revision,summary FROM investigations WHERE id=?", (CONVERSATION,),
        ).fetchone()
        self.db.execute("INSERT OR REPLACE INTO analyst.handoffs VALUES(?,?,?)",
                        (CACHE_KEY, revision, note))
        self.versions.record(VersionRef(CACHE_STORE, CACHE_KEY, revision),
                             inputs=[VersionRef("summaries", CONVERSATION, revision)])

    def inspect(self, subject, store, key):
        if store != CACHE_STORE:
            return super().inspect(subject, store, key)
        self._check_subject(subject)
        with self.lock:
            row = self.db.execute("SELECT revision,note FROM analyst.handoffs WHERE id=?", (key,)).fetchone()
        if row is None:
            return Stored(key, False, False, False, None)
        revision, note = row
        return Stored(key, True, TARGET_FACT in note, CONTROL_FACT in note, revision)
