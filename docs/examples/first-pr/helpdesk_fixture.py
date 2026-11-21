"""Connect the Setrya starter to this app's SQLite table and Flask routes."""
from contextlib import closing, contextmanager
from hashlib import sha256
import secrets
import sqlite3

from helpdesk import create_app
from setrya.erasure import Subject
from setrya.erasure.judges import AnswerJudges, Marker, TextAnswer
from setrya.erasure.v3 import Artifact, SharedCase, Stored

TARGET, CONTROL = "Harbor-17", "Thursday"
CASE = SharedCase(
    conversation="desk", route="/desk/answer", callers=("duty-agent",),
    target_question="What is the customer's depot code?", control_question="When is the next dispatch?",
    artifacts=(Artifact("notes", "depot-note", "delete"), Artifact("notes", "dispatch-note", "preserve")),
    judges=AnswerJudges(Marker(TARGET), Marker(CONTROL)),
)


class DeskFixture:
    def __init__(self, db, client, headers, subject):
        self.db, self.client, self.headers, self.subject = db, client, headers, subject

    def inspect(self, subject, store, key):
        if subject != self.subject or store != "notes":
            raise ValueError("unexpected fixture scope")
        row = self.db.execute("SELECT body FROM notes WHERE id = ?", (key,)).fetchone()
        if row is None:
            return Stored(key, False, False, False, None)
        body = row[0]
        # These notes contain only the body; its digest identifies this exact revision.
        return Stored(key, True, TARGET in body, CONTROL in body, sha256(body.encode()).hexdigest())

    def read(self, subject, caller, conversation, question):
        if subject != self.subject or conversation != CASE.conversation:
            raise ValueError("unexpected fixture scope")
        reply = self.client.get(CASE.route, query_string={"question": question}, headers=self.headers[caller])
        if reply.status_code != 200:
            raise RuntimeError("answer route rejected the test session")
        return TextAnswer(**reply.get_json())

    def erase(self, subject):
        if subject != self.subject:
            raise ValueError("unexpected test subject")
        reply = self.client.delete("/desk/customers/fixture-customer/notes", headers=self.headers["desk-owner"])
        if reply.status_code != 204:
            raise RuntimeError("deletion request failed")


@contextmanager
def open_fixture():
    subject = Subject.derive(secrets.token_bytes(32), "desk", "fixture-customer", "private")
    sessions = {"Bearer " + secrets.token_hex(16): caller for caller in (*CASE.callers, "desk-owner")}
    headers = {caller: {"Authorization": credential} for credential, caller in sessions.items()}
    with closing(sqlite3.connect(":memory:")) as db:
        app = create_app(db, sessions)
        with db:
            db.executemany("INSERT INTO notes VALUES (?, ?, ?, ?)", [
                ("depot-note", "fixture-customer", CASE.target_question, TARGET),
                ("dispatch-note", "team", CASE.control_question, CONTROL),
            ])
        yield subject, DeskFixture(db, app.test_client(), headers, subject), ()
