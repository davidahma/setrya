"""A disposable SaaS workspace with two authenticated readers and a delayed handoff writer."""
from contextlib import ExitStack, closing, contextmanager
from datetime import date, timedelta
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from threading import RLock, Thread
from uuid import uuid4

from setrya.erasure.judges import AnswerJudges, Marker, TextAnswer
from setrya.erasure.subject import Subject
from setrya.erasure.v3 import Artifact, ExternalItem, SharedCase, Stored, run_shared
from setrya.erasure.worker_fixture import WorkerFixture

WORKSPACE = "onboarding-team"
ROUTE = "/workspaces/onboarding-team/handoff/ask"
CALLERS = ("support-lead", "account-manager")
PRIVATE, RETAINED = "cobalt-bridge.example", "October 18"
QUESTIONS = ("Which hostname did the customer use?", "When is the team's next launch?")
SOURCES = (("customer-note", PRIVATE), ("team-calendar", RETAINED))
JUDGES = AnswerJudges(Marker(PRIVATE), Marker(RETAINED), capture="excerpt")
CASE = SharedCase(WORKSPACE, ROUTE, CALLERS, *QUESTIONS,
                  (Artifact("notes", "customer-note", "delete"), Artifact("notes", "team-calendar", "preserve"),
                   Artifact("handoffs", WORKSPACE, "rewrite")), judges=JUDGES)
SIMPLE = SharedCase(WORKSPACE, ROUTE, CALLERS[:1], *QUESTIONS,
                    (Artifact("notes", "customer-note", "delete"),), judges=JUDGES)


class Workspace:
    def __init__(self, connection, subject, *, guarded, shared):
        self.db, self.subject = connection, subject
        self.guarded, self.shared = guarded, shared
        self.lock = RLock()
        self.sessions = {secrets.token_urlsafe(32): caller for caller in CALLERS}
        self.db.executescript("""
            CREATE TABLE notes (key TEXT PRIMARY KEY, revision TEXT, body TEXT);
            CREATE TABLE handoffs (key TEXT PRIMARY KEY, revision TEXT, body TEXT);
        """)
        with self.db:
            self.db.executemany("INSERT INTO notes VALUES (?, '1', ?)", SOURCES)
            if self.shared:
                self._save(self._sources())
        self.worker = WorkerFixture(self._job, self.read)

    def _sources(self):
        return tuple(self.db.execute("SELECT key,revision,body FROM notes ORDER BY key"))

    def _save(self, sources):
        self.db.execute("INSERT OR REPLACE INTO handoffs VALUES (?,?,?)",
                        (WORKSPACE, uuid4().hex, json.dumps({key: body for key, _, body in sources})))

    def _check(self, subject):
        if subject != self.subject:
            raise ValueError("unknown test customer")

    def _job(self, subject, before_write):
        self._check(subject)
        with self.lock:
            snapshot = self._sources()
        before_write()
        with self.lock, self.db:
            if self.guarded and snapshot != self._sources():
                return
            if self.shared:
                self._save(snapshot)
            else:
                self.db.executemany("INSERT OR REPLACE INTO notes VALUES (?,?,?)", snapshot)

    def erase(self, subject):
        self._check(subject)
        with self.lock, self.db:
            self.db.execute("DELETE FROM notes WHERE key='customer-note'")
            if self.shared:
                self._save(self._sources())

    def inspect(self, subject, store, key):
        self._check(subject)
        sql = {"notes": "SELECT revision,body FROM notes WHERE key=?",
               "handoffs": "SELECT revision,body FROM handoffs WHERE key=?"}[store]
        with self.lock:
            row = self.db.execute(sql, (key,)).fetchone()
        if row is None:
            return Stored(key, False, False, False, None)
        revision, body = row
        return Stored(key, True, PRIVATE in body, RETAINED in body, revision)

    def answer(self, question):
        with self.lock:
            if self.shared:
                saved = self.db.execute("SELECT body FROM handoffs WHERE key=?", (WORKSPACE,)).fetchone()
                values = json.loads(saved[0])
            else:
                values = {key: body for key, _, body in self._sources()}
        key = dict(zip(QUESTIONS, ("customer-note", "team-calendar"))).get(question)
        return values.get(key, "No matching note.")

    def read(self, subject, caller, conversation, question):
        self._check(subject)
        session = next(key for key, owner in self.sessions.items() if owner == caller)
        with closing(HTTPConnection(*self.address, timeout=4)) as client:
            client.request("POST", f"/workspaces/{conversation}/handoff/ask", json.dumps({"question": question}),
                           {"Authorization": "Bearer " + session})
            response = client.getresponse()
            if response.status != 200:
                raise ValueError("workspace endpoint rejected the read")
            return TextAnswer(**json.load(response))

    def hold(self, subject):
        return self.worker.hold(subject)

    def checkpoint(self, subject):
        return self.worker.checkpoint(subject)

    def release_and_drain(self, subject, attempt):
        return self.worker.release_and_drain(subject, attempt)

    def cancel_and_drain(self, subject, attempt):
        return self.worker.cancel_and_drain(subject, attempt)


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        app = self.server.application
        scheme, _, credential = self.headers.get("Authorization", "").partition(" ")
        caller = app.sessions.get(credential) if scheme == "Bearer" else None
        if not caller or self.path != ROUTE:
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 4096:
                raise ValueError
            question = json.loads(self.rfile.read(size))["question"]
            if not isinstance(question, str):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            self.send_error(400)
            return
        payload = json.dumps(dict(caller=caller, conversation=WORKSPACE, route=self.path,
                                  question=question, text=app.answer(question))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def open_fixture(*, shared=False, guarded=True):
    """Yield a fresh subject, application and external obligations; all data are synthetic."""
    subject = Subject.derive(secrets.token_bytes(32), WORKSPACE, "customer-17", "private")
    with ExitStack() as stack:
        db = stack.enter_context(closing(sqlite3.connect(":memory:", check_same_thread=False)))
        app = Workspace(db, subject, guarded=guarded, shared=shared)
        server = stack.enter_context(HTTPServer(("127.0.0.1", 0), Endpoint))
        server.application = app
        app.address = server.server_address
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(server.shutdown)
        stack.enter_context(app.worker)
        external = (ExternalItem("provider", "invocation", "platform-team", "configuration", "fixture-settings",
                                 follow_up=(date.today() + timedelta(days=7)).isoformat(), subject_ref=subject.ref),)
        yield subject, app, external if shared else ()


def demo(output, mode):
    from shlex import join
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    print("A customer asked for deletion. Two teammates reopen the same workspace handoff.")
    lanes = (("broken", "FAIL"), ("guarded", "PASS")) if mode == "late-write" else (("guarded", "PASS"),)
    for lane, expected in lanes:
        directory = output / lane
        with open_fixture(shared=True, guarded=lane == "guarded") as (subject, app, items):
            receipt = run_shared(subject, case=CASE, application=app, external_items=items, output=directory,
                                 mode=mode, cancel_hold=app.cancel_and_drain)
        print(f"{receipt['technical_verdict']}  {lane} · {receipt['review_status']}")
        observations = json.loads((directory / "erasure-evidence.json").read_text())["final"]["answers"]
        for target, control in zip(observations[::2], observations[1::2]):
            if target and control:
                words = {True: "returned", False: "absent", None: "unknown"}
                print(f"  {target['caller']} · customer hostname {words[target['target_present']]} · "
                      f"team launch date {words[control['control_present']]}")
        print("  Replay  " + join(["setrya", "replay", str(directory)]))
        if receipt["technical_verdict"] != expected:
            return 1
    return 0
