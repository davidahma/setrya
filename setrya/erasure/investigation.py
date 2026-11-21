"""One shared LangGraph investigation, two authenticated readers and a stale summary writer."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
import http.client
from hashlib import sha256
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
from secrets import token_bytes, token_hex, token_urlsafe
from shlex import quote
from socketserver import TCPServer
import sqlite3
from tempfile import TemporaryDirectory
from threading import RLock, Thread
from typing import TypedDict
from uuid import uuid4

from setrya.drill import InputError
from .canary import SQLiteCanaryScanner
from .journal import CopyJournal
from .lineage import VersionLineage, VersionRef
from .subject import Subject
from .worker_fixture import WorkerFixture

CONVERSATION = "incident-41"
ROUTE = f"/investigations/{CONVERSATION}/ask"
CALLERS = ("analyst-east", "analyst-west")
TARGET_QUESTION = "Which laptop was quarantined?"
CONTROL_QUESTION = "Which credential reset did the investigation confirm?"
TARGET_FACT = "LAPTOP-KITE-73"
CONTROL_FACT = "RESET-FINCH-28"
ALICE_KEY, BOB_KEY = "finding-17", "finding-28"


class InvestigationState(TypedDict):
    conversation: str
    summary: str
    revision: str


def _graph(saver):
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(InvestigationState)
    graph.add_node("save_handoff", lambda state: {"summary": state["summary"]})
    graph.add_edge(START, "save_handoff")
    graph.add_edge("save_handoff", END)
    return graph.compile(checkpointer=saver)


@dataclass(frozen=True)
class Finding:
    key: str
    revision: str
    kind: str
    value: str

    def version(self):
        return VersionRef("source", self.key, self.revision)


def _summarize(findings):
    lines = []
    for finding in findings:
        if finding.kind == "device":
            lines.append("Endpoint isolated: " + finding.value)
        elif finding.kind == "credential":
            lines.append("Recovery confirmed: " + finding.value)
    return "\n".join(lines)


def _answer(note, question):
    prefix = {TARGET_QUESTION: "Endpoint isolated: ", CONTROL_QUESTION: "Recovery confirmed: "}.get(question)
    if prefix:
        for line in note.splitlines():
            if line.startswith(prefix):
                return line[len(prefix):]
    return "No matching finding in this investigation."


class _Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        app = self.server.app
        caller = app.tokens.get(self.headers.get("Authorization"))
        if not caller or self.path != ROUTE:
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError
            request = json.loads(self.rfile.read(length))
            question = request["question"]
            if not isinstance(question, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        data = app.reopen()
        body = json.dumps({"caller": caller, "route": self.path, "conversation": data["conversation"],
                           "question": question, "answer": _answer(data["summary"], question)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Investigation:
    """Application-owned fixture. A thread rewrite regenerates its saved state from surviving inputs."""
    def __init__(self, folder, subject, control, journal, *, guarded):
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.subject, self.control = subject, control
        self.guarded = guarded
        self.folder = Path(folder)
        self.journal = journal
        self.versions = VersionLineage(journal, "fixture-workspace")
        self.marker = "SET-" + token_hex(16)
        self.snapshot_key = "snapshot-" + token_hex(8)
        self.tokens = {"Bearer " + token_urlsafe(32): caller for caller in CALLERS}
        self.db = sqlite3.connect(self.folder / "findings.sqlite", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE findings (id TEXT PRIMARY KEY, subject TEXT, revision TEXT, kind TEXT, value TEXT, raw_text TEXT);
            CREATE TABLE investigations (id TEXT PRIMARY KEY, revision TEXT, summary TEXT);
            CREATE TABLE answers (conversation TEXT PRIMARY KEY, revision TEXT, source_revision TEXT, note TEXT);
            CREATE TABLE snapshots (id TEXT PRIMARY KEY, conversation TEXT, kind TEXT, content TEXT);
        """)
        self.scanner = SQLiteCanaryScanner(self.db, table="snapshots", key="id", content="content", scope_column="conversation")
        self.graph_path = self.folder / "threads.sqlite"
        self.conn = sqlite3.connect(self.graph_path, check_same_thread=False)
        self.saver = SqliteSaver(self.conn)
        self.graph = _graph(self.saver)
        self.config = {"configurable": {"thread_id": CONVERSATION}}
        self.lock = RLock()
        self.worker = WorkerFixture(self._summarize_job, self.read)

    def __enter__(self):
        self.http = TCPServer(("127.0.0.1", 0), _Endpoint)
        self.http.app = self
        self.server_thread = Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.server_thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.worker.__exit__(*exc)
        finally:
            self.http.shutdown()
            self.server_thread.join()
            self.http.server_close()
            self.conn.close()
            self.db.close()

    def _check_subject(self, subject):
        if subject != self.subject:
            raise ValueError("the fixture can erase only its seeded synthetic subject")

    def seed(self):
        with self.lock, self.db:
            for s, key, kind, value in ((self.subject, ALICE_KEY, "device", TARGET_FACT),
                                        (self.control, BOB_KEY, "credential", CONTROL_FACT)):
                text = (f"Quarantined computer {value}. Test correlation {self.marker}." if kind == "device"
                        else f"The credential reset event was {value}.")
                self.db.execute("INSERT INTO findings VALUES (?,?,?,?,?,?)", (key, s.ref, "1", kind, value, text))
                self.versions.record(VersionRef("source", key, "1"), subjects=[s])
                self.journal.record(s, store="source", key=key, source=key, action="stored")
            self.db.execute("""INSERT INTO snapshots(id,conversation,kind,content)
                               SELECT ?,?,'disposable-export',raw_text FROM findings WHERE id=?""",
                            (self.snapshot_key, CONVERSATION, ALICE_KEY))
            # A second workspace carries the same marker. A fixture-scoped scan must leave it alone.
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?)",
                            ("other-workspace", "incident-other", "disposable-export", self.marker))
            self._save_summary(self._findings())

    def _findings(self):
        return tuple(Finding(*row) for row in self.db.execute("SELECT id,revision,kind,value FROM findings ORDER BY id"))

    def _save_summary(self, captured):
        note = _summarize(captured)
        revision = uuid4().hex
        summary = VersionRef("summaries", CONVERSATION, revision)
        self.db.execute("INSERT OR REPLACE INTO investigations VALUES(?,?,?)", (CONVERSATION, revision, note))
        self.versions.record(summary, inputs=[f.version() for f in captured])
        self.graph.invoke({"conversation": CONVERSATION, "summary": note, "revision": revision}, config=self.config)
        self.versions.record(VersionRef("checkpoints", CONVERSATION, revision), inputs=[summary])
        self.db.execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,?)", (CONVERSATION, revision, revision, note))
        self.versions.record(VersionRef("answers", CONVERSATION, revision), inputs=[summary])

    def _summarize_job(self, subject, before_write):
        self._check_subject(subject)
        with self.lock:
            captured = self._findings()
        before_write()
        with self.lock, self.db:
            if self.guarded and {f.version() for f in captured} != {f.version() for f in self._findings()}:
                return
            self._save_summary(captured)

    def hold(self, subject):
        return self.worker.hold(subject)

    def checkpoint(self, subject):
        return self.worker.checkpoint(subject)

    def release_and_drain(self, subject, attempt):
        return self.worker.release_and_drain(subject, attempt)

    def lineage(self, subject):
        return self.versions.affected(subject)

    def scan(self, subject, store):
        self._check_subject(subject)
        if store != "snapshots":
            raise ValueError("undeclared scan boundary")
        with self.lock:
            return self.scanner.find(scope=CONVERSATION, marker=self.marker)

    def erase(self, subject):
        self._check_subject(subject)
        with self.lock, self.db:
            discovered = self.scan(subject, "snapshots")
            # This fixture owns disposable exports. The scanner itself cannot delete or rewrite data.
            for key in discovered:
                self.db.execute("""DELETE FROM snapshots WHERE id=? AND conversation=? AND kind='disposable-export'
                                   AND content IN (SELECT raw_text FROM findings WHERE subject=?)""",
                                (key, CONVERSATION, subject.ref))
            affected = self.lineage(subject)
            self.db.execute("DELETE FROM findings WHERE subject=?", (subject.ref,))
            if any(edge["artifact"]["store"] == "summaries" for edge in affected["derivations"]):
                # Reconstruct this thread from the surviving records. Dropping earlier saver rows
                # prevents get_state_history from reopening the old note in the same conversation.
                self.saver.delete_thread(CONVERSATION)
                self._save_summary(self._findings())

    def _stored_checkpoint(self, key):
        if key != CONVERSATION:
            return None
        state = self.graph.get_state(self.config).values
        if not state:
            return None
        serialized = []
        # Include old committed checkpoints and writes, even an orphaned write invisible to get_state().
        with self.saver.lock:
            for table, payload in (("checkpoints", "checkpoint"), ("writes", "value")):
                rows = self.conn.execute(f"SELECT type,{payload} FROM {table} WHERE thread_id=?", (key,))
                for encoding, blob in rows:
                    value = self.saver.serde.loads_typed((encoding, blob))
                    serialized.append(json.dumps(value, default=str, sort_keys=True))
        return state["revision"], any(TARGET_FACT in text for text in serialized), CONTROL_FACT in state["summary"]

    def inspect(self, subject, store, key):
        from .v3 import Stored

        self._check_subject(subject)
        with self.lock:
            if store == "checkpoints":
                row = self._stored_checkpoint(key)
                if row:
                    revision, target, control = row
                    return Stored(key, True, target, control, revision)
            elif store == "snapshots":
                row = self.db.execute("SELECT content FROM snapshots WHERE id=? AND conversation=?", (key, CONVERSATION)).fetchone()
                if row:
                    text = row[0]
                    # This synthetic snapshot store has no version counter. Identify the exact bytes read.
                    return Stored(key, True, TARGET_FACT in text, CONTROL_FACT in text, sha256(text.encode()).hexdigest())
            elif store in ("source", "summaries", "answers"):
                query = {
                    "source": "SELECT revision,raw_text FROM findings WHERE id=?",
                    "summaries": "SELECT revision,summary FROM investigations WHERE id=?",
                    "answers": "SELECT revision,note FROM answers WHERE conversation=?",
                }[store]
                row = self.db.execute(query, (key,)).fetchone()
                if row:
                    revision, text = row
                    return Stored(key, True, TARGET_FACT in text, CONTROL_FACT in text, revision)
            else:
                raise ValueError("undeclared direct reading")
            return Stored(key, False, False, False, None)

    def reopen(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        with self.lock, closing(sqlite3.connect(self.graph_path, check_same_thread=False)) as conn:
            state = _graph(SqliteSaver(conn)).get_state(self.config).values
            row = self.db.execute("SELECT note FROM answers WHERE conversation=? AND source_revision=?",
                                  (state["conversation"], state["revision"])).fetchone()
            return {"conversation": state["conversation"], "summary": row[0] if row else state["summary"]}

    def read(self, subject, caller, conversation, question):
        from .v3 import Answer

        self._check_subject(subject)
        token = next(token for token, principal in self.tokens.items() if principal == caller)
        client = http.client.HTTPConnection(*self.http.server_address, timeout=3)
        try:
            client.request("POST", f"/investigations/{conversation}/ask", json.dumps({"question": question}),
                           {"Authorization": token})
            response = client.getresponse()
            if response.status != 200:
                raise ValueError("the authenticated investigation endpoint rejected the request")
            observation = json.load(response)
        finally:
            client.close()
        answer = observation["answer"]
        return Answer(observation["caller"], observation["conversation"], observation["route"], observation["question"],
                      TARGET_FACT in answer, CONTROL_FACT in answer)


def shared_case():
    from .v3 import Artifact, SharedCase

    return SharedCase(CONVERSATION, ROUTE, CALLERS, TARGET_QUESTION, CONTROL_QUESTION,
                      (Artifact("source", ALICE_KEY, "delete"), Artifact("source", BOB_KEY, "preserve"),
                       *(Artifact(store, CONVERSATION, "rewrite") for store in ("summaries", "checkpoints", "answers"))),
                      ("snapshots",), require_lineage=True)


def run_case(output, *, guarded=True, mode="late-write"):
    from .v3 import ExternalItem, run_shared

    with TemporaryDirectory(prefix="setrya-investigation-") as tmp:
        folder = Path(tmp)
        secret = token_bytes(32)
        subject = Subject.derive(secret, "fixture-workspace", "person-one", "restricted")
        control = Subject.derive(secret, "fixture-workspace", "person-two", "restricted")
        journal = CopyJournal(folder / "copies.sqlite")
        item = ExternalItem("model-provider", "sample-invocation", "provider-owner", "configuration",
                            "fixture-config-17", follow_up=(date.today() + timedelta(days=7)).isoformat(),
                            subject_ref=subject.ref)
        with Investigation(folder, subject, control, journal, guarded=guarded) as app:
            app.seed()
            return run_shared(subject, case=shared_case(), application=app, mode=mode,
                              external_items=(item,), output=output, journal=journal)


def run_demo(output: Path, *, mode="late-write") -> int:
    try:
        import langgraph.checkpoint.sqlite  # noqa: F401
    except ImportError:
        raise InputError("install the erasure extra with python -m pip install 'setrya[erasure]'") from None
    if mode not in ("late-write", "deletion"):
        raise InputError("expected late-write or deletion mode")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    print("Two analysts reopen the same investigation and ask about a quarantined laptop and a credential reset.")
    lanes = (("broken", "FAIL"), ("guarded", "PASS")) if mode == "late-write" else (("guarded", "PASS"),)
    for lane, expected in lanes:
        receipt = run_case(output / lane, guarded=lane == "guarded", mode=mode)
        print(f"{lane}: technical {receipt['technical_verdict']} · review {receipt['review_status']} · "
              f"drill duration {receipt['drill_duration_ms']} ms")
        evidence = json.loads((output / lane / "erasure-evidence.json").read_bytes())
        for target, control in zip(evidence["final"]["answers"][::2], evidence["final"]["answers"][1::2]):
            if target and control:
                laptop = {True: "returned", False: "absent", None: "unknown"}[target["target_present"]]
                reset = {True: "returned", False: "absent", None: "unknown"}[control["control_present"]]
                print(f"  {target['caller']} reopened {target['conversation']}: laptop {laptop}, credential reset {reset}.")
        for item in receipt["open_items"]:
            print(f"  {item['owner']} to review {item['store']}/{item['key']} by {item['follow_up']}.")
        print(f"  Replay: setrya replay {quote(str(output / lane))}")
        if receipt["technical_verdict"] != expected:
            return 1
    return 0
