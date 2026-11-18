"""A small search app with a source, SQLite queue, worker, and FTS5 index."""

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

CHECKPOINT = "after_source_read"
ATTEMPT = "old-worker-1"
ROUTE = "support-search"
SCOPE = "support-agent"
MODES = {"fixed", "broken", "opaque", "lag-exposed", "early-stale", "missing-identity", "missing-checkpoint"}

REFUND_A = "Return an order within 30 days of purchase."
REFUND_B = "Return an order within 14 days of purchase."
DELIVERY = "Track your delivery once a shipping confirmation arrives."
OLD_CHUNKS = (REFUND_A, "Keep the order receipt.", "Return the original packaging.",
              "Include the original card.", "The return policy includes the warehouse express clause.")
NEW_CHUNKS = (REFUND_B + " The updated return policy covers warehouse returns.",
              "Keep the order receipt.", "Return the original packaging.")
TERMS = re.compile(r"[A-Za-z0-9]+")
SEARCH_TERMS = {"return", "policy", "order", "delivery", "track", "warehouse", "express", "clause"}


@contextmanager
def connect(path: Path):
    db = sqlite3.connect(path, timeout=1.0)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def replace_chunks(db, doc: str, revision: str, body: tuple, origin: str, *, keep_retired=False):
    if keep_retired:
        db.execute("DELETE FROM search_index WHERE doc = ? AND CAST(slot AS INTEGER) <= ?", (doc, len(body)))
    else:
        db.execute("DELETE FROM search_index WHERE doc = ?", (doc,))
    db.executemany("INSERT INTO search_index(doc, slot, marker, origin, body) VALUES (?, ?, ?, ?, ?)", [
        (doc, slot, f"{doc}@{revision}" if slot == 1 else f"{doc}@{revision}.{slot}", origin, text)
        for slot, text in enumerate(body, 1)
    ])


def snapshot(db):
    db.execute("DELETE FROM cached_index")
    db.execute("INSERT INTO cached_index SELECT * FROM search_index")
    db.execute("UPDATE state SET cached_commit = write_commit")


class App:
    def __init__(self, directory: Path, run_id: str, mode: str, scenario: str):
        if mode not in MODES or scenario not in {"refund", "retired"}:
            raise ValueError("unsupported fixture mode or scenario")
        self.directory = directory
        self.path = directory / "support.sqlite"
        self.run_id = run_id
        self.mode = mode
        self.scenario = scenario
        self.old = OLD_CHUNKS if scenario == "retired" else (REFUND_A,)
        self.new = NEW_CHUNKS if scenario == "retired" else (REFUND_B,)
        self.process = None
        self.published = False
        self.released = False
        self.refund_reads = 0
        self.lag_reads = 0

    def seed(self):
        with connect(self.path) as db:
            db.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE source (doc TEXT PRIMARY KEY, revision TEXT, chunks TEXT);
                CREATE TABLE state (write_commit INTEGER, cached_commit INTEGER);
                INSERT INTO state VALUES (1, 1);
                CREATE TABLE queue (attempt TEXT PRIMARY KEY, status TEXT, revision TEXT, commit_seq INTEGER);
                CREATE VIRTUAL TABLE search_index USING fts5(doc UNINDEXED, slot UNINDEXED,
                    marker UNINDEXED, origin UNINDEXED, body);
                CREATE VIRTUAL TABLE cached_index USING fts5(doc UNINDEXED, slot UNINDEXED,
                    marker UNINDEXED, origin UNINDEXED, body);
            """)
            db.executemany("INSERT INTO source VALUES (?, ?, ?)", [
                ("refund", "A", json.dumps(self.old)), ("delivery", "1", json.dumps((DELIVERY,))),
            ])
            replace_chunks(db, "refund", "A", self.old, "seed")
            replace_chunks(db, "delivery", "1", (DELIVERY,), "seed")
            snapshot(db)

    def start_old(self) -> str:
        with connect(self.path) as db:
            db.execute("INSERT INTO queue VALUES (?, 'queued', NULL, NULL)", (ATTEMPT,))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "examples.support_search", "worker", str(self.directory),
             self.run_id, self.mode, self.scenario],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return ATTEMPT

    def checkpoint(self) -> dict | None:
        row = self.job()
        if self.mode == "missing-checkpoint" or row["status"] != "held":
            return None
        return {**self.identity(row["revision"]), "name": CHECKPOINT}

    def publish(self):
        with connect(self.path) as db:
            db.execute("UPDATE source SET revision = 'B', chunks = ? WHERE doc = 'refund'", (json.dumps(self.new),))
            replace_chunks(db, "refund", "B", self.new, "publish",
                           keep_retired=self.scenario == "retired" and self.mode == "broken")
            db.execute("UPDATE state SET write_commit = write_commit + 1")
            if self.mode in {"opaque", "lag-exposed"}:
                snapshot(db)
        self.published = True
        return {"run_id": self.run_id, "document": "refund", "revision": "B"}

    def release(self):
        with connect(self.path) as db:
            edited = db.execute("UPDATE queue SET status = 'released' WHERE attempt = ? AND status = 'held'",
                                (ATTEMPT,)).rowcount
            if edited != 1:
                raise RuntimeError("release requires a held worker")
        self.released = True

    def receipt(self) -> dict | None:
        row = self.job()
        if row["status"] not in {"committed", "rejected"}:
            return None
        return {**self.identity(row["revision"]), "outcome": row["status"],
                "generation": self.run_id if row["status"] == "committed" else None,
                "commit": row["commit_seq"]}

    def job(self):
        if self.process and self.process.poll() not in {None, 0}:
            raise RuntimeError("fixture worker exited")
        with connect(self.path) as db:
            return db.execute("SELECT status, revision, commit_seq FROM queue WHERE attempt = ?", (ATTEMPT,)).fetchone()

    def identity(self, revision):
        return {"run_id": self.run_id, "attempt": ATTEMPT, "document": "refund", "revision": revision}

    def search(self, question: str, route: str, scope: str, top_k: int) -> dict:
        if route != ROUTE or scope != SCOPE or not 1 <= top_k <= 100:
            raise ValueError("invalid search route, scope or top_k")
        terms = sorted(set(TERMS.findall(question.lower())) & SEARCH_TERMS)
        hide_identity = self.released and self.mode == "missing-identity"

        with connect(self.path) as db:
            # Start the read transaction before taking both the watermark and the FTS snapshot.
            db.execute("BEGIN")
            state = db.execute("SELECT * FROM state").fetchone()
            cached = self._use_cache(terms, state)
            index = "cached_index" if cached else "search_index"
            rows = db.execute(
                f"SELECT marker, origin, body FROM {index} WHERE {index} MATCH ? ORDER BY rank, rowid LIMIT ?",
                (" OR ".join(terms), top_k),
            ).fetchall() if terms else []
        return {
            "route": route, "scope": scope,
            "hits": [{"id": None if hide_identity else row["marker"],
                      "origin": row["origin"], "text": row["body"]} for row in rows],
            "view": None if self.mode == "opaque" else {
                "generation": self.run_id, "commit": state["cached_commit" if cached else "write_commit"],
                "route": route, "scope": scope,
            },
        }

    def _use_cache(self, terms, state) -> bool:
        if self.mode == "early-stale" and self.published and not self.released and "return" in terms:
            self.refund_reads += 1
            return self.refund_reads == 2
        if self.mode == "opaque" and self.published:
            return True
        if self.mode == "lag-exposed" and state["write_commit"] > state["cached_commit"]:
            self.lag_reads += 1
            return self.lag_reads <= 2
        return False

    def close(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)


def work(directory: Path, mode: str, scenario: str):
    path = directory / "support.sqlite"
    with connect(path) as db:
        # The worker materializes its source snapshot before it acknowledges the checkpoint.
        revision, stored_chunks = db.execute("SELECT revision, chunks FROM source WHERE doc = 'refund'").fetchone()
        body = tuple(json.loads(stored_chunks))
        db.execute("UPDATE queue SET status = 'held', revision = ? WHERE attempt = ? AND status = 'queued'",
                   (revision, ATTEMPT))

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        with connect(path) as db:
            status = db.execute("SELECT status FROM queue WHERE attempt = ?", (ATTEMPT,)).fetchone()[0]
        if status == "released":
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("old worker exceeded its release deadline")

    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT revision FROM source WHERE doc = 'refund'").fetchone()[0]
        broken = scenario != "retired" and mode in {"broken", "opaque", "lag-exposed"}
        if revision != current and not broken:
            db.execute("UPDATE queue SET status = 'rejected' WHERE attempt = ?", (ATTEMPT,))
            return
        replace_chunks(db, "refund", revision, body, ATTEMPT)
        db.execute("UPDATE state SET write_commit = write_commit + 1")
        (commit,) = db.execute("SELECT write_commit FROM state").fetchone()
        db.execute("UPDATE queue SET status = 'committed', commit_seq = ? WHERE attempt = ?", (commit, ATTEMPT))
