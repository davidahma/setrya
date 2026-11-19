"""Two SQLite jobs update one search projection. The second rebuilds it from current state."""

from contextlib import closing
from dataclasses import replace
import sqlite3

from setrya.adapter import ReadView, SearchResult
from setrya.scenario_fixture import Application as SQLiteApplication, _Delivery


GUARD_CAPTURED_WRITE = True


class Rebuild(_Delivery):
    def work(self):
        outcome = super().work()
        if outcome == "committed":
            with closing(sqlite3.connect(self.app.path)) as db, db:
                db.execute("UPDATE projection SET marker=(SELECT marker FROM state) WHERE document='subject'")
                # This example has one rebuild, so position 1 identifies its completed transaction.
                db.execute("PRAGMA user_version=1")
        return outcome

    def receipt(self):
        receipt = super().receipt()
        return replace(receipt, commit=1) if receipt and receipt.outcome == "committed" else receipt

    def search(self, probe):
        with closing(sqlite3.connect(self.app.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            position = db.execute("PRAGMA user_version").fetchone()[0]
            hits = db.execute("SELECT marker FROM projection WHERE caller=? AND document=? LIMIT ?",
                              (probe.rules.scope, probe.question, probe.rules.top_k)).fetchall()
        return SearchResult(tuple(marker for marker, in hits), ReadView("projection", position, "search",
                            probe.rules.scope), route="search", scope=probe.rules.scope)


class Application(SQLiteApplication):
    def __init__(self, context):
        super().__init__(context)
        self.broken = not GUARD_CAPTURED_WRITE

    def delivery(self, message_id, attempt):
        if message_id == "job-b":
            worker = Rebuild(self, message_id, attempt)
            self.workers.append(worker)
            return worker
        return super().delivery(message_id, attempt)
