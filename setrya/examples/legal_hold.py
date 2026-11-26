"""Check selective deletion alongside a held record in a disposable SQLite application."""
from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_bytes
from shlex import join
import sqlite3

from setrya.erasure import Artifact, Maintenance, SharedCase, Stored, Subject, run_shared
from setrya.erasure.judges import AnswerJudges, Marker, ReviewedAnswers, TextAnswer
from setrya.receipts import summary


_TARGET = "Customer's disposable note: amber-sandbox.example"
_CONTROL = "Incident evidence: retained packet 17"
_GONE = "No matching record."
_QUESTIONS = {"What hostname was in the customer note?": "customer-note",
              "Which packet is under legal hold?": "held-packet"}
LANES = {"guarded": "PASS", "lost-hold": "FAIL", "restored-hold": "FAIL", "resurfaced-target": "FAIL"}

CASE = SharedCase(
    "incident-register", "/incident-register/ask", ("incident-reviewer",), *_QUESTIONS,
    (Artifact("records", "customer-note", "delete"), Artifact("records", "held-packet", "preserve"),
     Artifact("views", "consolidated", "rewrite"), Artifact("views", "summary", "rewrite")),
    judges=AnswerJudges(
        (Marker(_TARGET), ReviewedAnswers((_TARGET,), (_CONTROL, _GONE))),
        (Marker(_CONTROL), ReviewedAnswers((_CONTROL,), (_TARGET, _GONE))),
    ),
    maintenance_steps=("consolidate", "summarize"),
)


class Application:
    def __init__(self, database, subject, lane):
        self.db, self.subject, self.lane = database, subject, lane
        self.db.executescript("""
            CREATE TABLE records (key TEXT PRIMARY KEY, body TEXT NOT NULL, held INTEGER NOT NULL);
            CREATE TABLE views (key TEXT PRIMARY KEY, body TEXT NOT NULL, revision INTEGER NOT NULL);
        """)
        with self.db:
            self.db.executemany("INSERT INTO records VALUES (?, ?, ?)",
                                (("customer-note", _TARGET, 0), ("held-packet", _CONTROL, 1)))
            self._consolidate()
            self._summarize()
        self.stale_summary = self._view("summary")

    def _assert_subject(self, subject):
        if subject != self.subject:
            raise ValueError("Unexpected fixture subject.")

    def _publish(self, key, body):
        self.db.execute("""
            INSERT INTO views VALUES (?, ?, 1)
            ON CONFLICT (key) DO UPDATE SET body = excluded.body, revision = views.revision + 1
        """, (key, body))

    def _view(self, key):
        return self.db.execute("SELECT body FROM views WHERE key = ?", (key,)).fetchone()[0]

    def _consolidate(self):
        rows = self.db.execute("SELECT key, body FROM records ORDER BY key")
        self._publish("consolidated", json.dumps(dict(rows)))

    def _summarize(self):
        # The serving projection reads the consolidated view, not the source table.
        consolidated = json.loads(self._view("consolidated"))
        self._publish("summary", json.dumps({answer_key: consolidated[answer_key]
                                             for answer_key in _QUESTIONS.values() if answer_key in consolidated}))

    def erase(self, subject):
        self._assert_subject(subject)
        with self.db:
            self.db.execute("DELETE FROM records WHERE held = 0")
            if self.lane == "lost-hold":
                self.db.execute("DELETE FROM records WHERE key = ?", ("held-packet",))
            self._consolidate()
            self._summarize()

    def maintain(self, subject, step):
        self._assert_subject(subject)
        with self.db:
            if step == "consolidate":
                self._consolidate()
                if self.lane == "restored-hold":
                    self.db.execute("DELETE FROM records WHERE key = ?", ("held-packet",))
            elif step == "summarize":
                if self.lane == "restored-hold":
                    self.db.execute("INSERT INTO records VALUES (?, ?, 1)", ("held-packet", _CONTROL))
                if self.lane == "resurfaced-target":
                    self._publish("summary", self.stale_summary)
                else:
                    self._summarize()
            else:
                raise ValueError("Unexpected fixture maintenance step.")
        return Maintenance(step)

    def inspect(self, subject, store, key):
        self._assert_subject(subject)
        if store == "records":
            row = self.db.execute("SELECT body, held FROM records WHERE key = ?", (key,)).fetchone()
        elif store == "views":
            row = self.db.execute("SELECT body, revision FROM views WHERE key = ?", (key,)).fetchone()
        else:
            raise ValueError("Unknown fixture store.")
        if row is None:
            return Stored(key, False, False, False, None)
        revision = sha256(json.dumps(row).encode()).hexdigest()
        return Stored(key, True, _TARGET in row[0], _CONTROL in row[0], revision)

    def read(self, subject, caller, conversation, question):
        self._assert_subject(subject)
        if caller not in CASE.callers or conversation != CASE.conversation or question not in _QUESTIONS:
            raise ValueError("Unknown fixture read.")
        text = json.loads(self._view("summary")).get(_QUESTIONS[question], _GONE)
        return TextAnswer(caller, conversation, CASE.route, question, text)


@contextmanager
def open_fixture(lane="guarded"):
    if lane not in LANES:
        raise ValueError("Unknown legal hold fixture lane.")
    subject = Subject.derive(token_bytes(32), CASE.conversation, "synthetic-request", "private")
    with closing(sqlite3.connect(":memory:")) as database:
        yield subject, Application(database, subject, lane)


def demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for lane, expected in LANES.items():
        destination = output / lane
        with open_fixture(lane) as (subject, application):
            receipt = run_shared(subject, case=CASE, application=application, output=destination, mode="deletion")
        print(f"{lane} · {summary(receipt)}")
        print("Replay  " + join(["setrya", "replay", str(destination)]))
        if receipt["technical_verdict"] != expected:
            return 1
    return 0
