"""Disposable SQLite search projection and delayed delivery workers for scenario drills."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Event

from .adapter import Checkpoint, Publication, Receipt, SearchResult


class Application:
    def __init__(self, context):
        options = context.drill.options
        mode = options.get("mode", "fixed")
        if options.keys() - {"mode"} or mode not in ("fixed", "broken"):
            raise ValueError("the SQLite scenario fixture accepts mode fixed or broken")
        self.broken = mode == "broken"
        self.context = context
        self.path = context.directory.resolve() / "index.db"
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.workers = []

    def seed(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE state(epoch INTEGER, marker TEXT);
                INSERT INTO state VALUES (1, 'subject@A');
                CREATE TABLE grants(caller TEXT PRIMARY KEY);
                INSERT INTO grants VALUES ('support'), ('owner');
                CREATE TABLE projection(caller TEXT, document TEXT, marker TEXT, PRIMARY KEY(caller, document));
                INSERT INTO projection SELECT caller, 'subject', 'subject@A' FROM grants;
                INSERT INTO projection SELECT caller, 'control', 'retained@A' FROM grants;
                CREATE TABLE messages(id TEXT PRIMARY KEY, epoch INTEGER, marker TEXT);
            """)

    def publish(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE state SET epoch = epoch + 1")
            if self.context.scenario_kind == "update":
                revision = "updated"
                db.execute("UPDATE state SET marker = 'subject@B'")
                db.execute("UPDATE projection SET marker = 'subject@B' WHERE document = 'subject'")
            elif self.context.scenario_kind == "delete":
                revision = "deleted"
                db.execute("UPDATE state SET marker = NULL")
                db.execute("DELETE FROM projection WHERE document = 'subject'")
            elif self.context.scenario_kind == "revoke":
                revision = "revoked"
                db.execute("DELETE FROM grants WHERE caller = 'support'")
                db.execute("DELETE FROM projection WHERE caller = 'support' AND document = 'subject'")
            else:
                raise ValueError("unknown SQLite fixture mutation")
        return Publication(self.context.run_id, "subject", revision)

    def delivery(self, message_id, attempt):
        worker = _Delivery(self, message_id, attempt)
        self.workers.append(worker)
        return worker

    def close(self):
        for worker in self.workers:
            worker.abort.set()
            worker.resume.set()
        self.pool.shutdown()


class _Delivery:
    def __init__(self, app, message, attempt):
        self.app, self.message, self.attempt = app, message, attempt
        self.captured, self.resume, self.abort = Event(), Event(), Event()

    def start_old(self):
        self.future = self.app.pool.submit(self.work)
        return self.attempt

    def work(self):
        with closing(sqlite3.connect(self.app.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO messages SELECT ?,epoch,marker FROM state", (self.message,))
            self.consumed, epoch, marker = db.execute(
                "SELECT id,epoch,marker FROM messages WHERE id = ?", (self.message,)
            ).fetchone()
            self.document, self.revision = marker.split("@")
            readers = list(db.execute("SELECT caller FROM grants"))
        self.captured.set()
        self.resume.wait()
        if self.abort.is_set():
            return "rejected"
        with closing(sqlite3.connect(self.app.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not self.app.broken and epoch != db.execute("SELECT epoch FROM state").fetchone()[0]:
                return "rejected"
            db.executemany("INSERT OR REPLACE INTO projection VALUES (?, 'subject', ?)",
                           [(caller, marker) for caller, in readers])
        return "committed"

    def checkpoint(self):
        if self.future.done():
            self.future.result()
            return None
        if self.captured.is_set() and not self.resume.is_set():
            return Checkpoint(self.app.context.run_id, self.attempt, self.document, self.revision, "before-write")

    def message_id(self):
        return self.consumed

    def retryable_failure(self):
        self.abort.set()
        self.resume.set()
        self.future.result(timeout=5)
        return self.receipt()

    def release(self):
        self.resume.set()
        self.future.result(timeout=5)

    def receipt(self):
        if self.future.done():
            outcome = self.future.result()
            return Receipt(self.app.context.run_id, self.attempt, self.document, self.revision,
                           outcome, "projection" if outcome == "committed" else None)

    def search(self, probe):
        # The observer has a separate read-only connection and returns only this caller's projection.
        with closing(sqlite3.connect(self.app.path.as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT marker FROM projection WHERE caller=? AND document=? LIMIT ?",
                              (probe.rules.scope, probe.question, probe.rules.top_k)).fetchall()
        return SearchResult(tuple(marker for marker, in rows), route="search", scope=probe.rules.scope)
