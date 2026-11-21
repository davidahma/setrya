"""Run a fictional chart snapshot through a real LangGraph checkpoint and two HTTP sessions."""

import http.client
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
from secrets import token_urlsafe, token_bytes
from shlex import quote
from socketserver import TCPServer
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
from typing import TypedDict

from .worker_fixture import WorkerFixture


ALICE = "Alice · fictional chart A17"
BOB = "Bob · fictional chart B42"
QUESTION = "Which charts are in the saved handoff?"
CALLERS = ("care-reader", "shift-reviewer")
ROUTE = "reopen-handoff"
INVENTORY = '''version = 1

[[systems]]
name = "charts"
owner = "chart-team"
role = "source"
approved_labels = ["restricted"]
retention_days = 0
adapter = "charts"

[[systems]]
name = "checkpoints"
owner = "assistant-team"
role = "checkpoint"
approved_labels = ["restricted"]
retention_days = 0
adapter = "checkpoints"

[[systems]]
name = "memory"
owner = "assistant-team"
role = "memory"
approved_labels = ["restricted"]
retention_days = 0
adapter = "memory"
'''
PROVIDER_INVENTORY = '''
[[systems]]
name = "model-observer"
owner = "assistant-team"
role = "provider"
approved_labels = ["restricted"]
retention_days = 0
adapter = "none"
'''


class ChartState(TypedDict):
    chart: str


def _graph(saver):
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(ChartState)
    graph.add_node("snapshot", lambda state: {"chart": state["chart"]})
    graph.add_edge(START, "snapshot")
    graph.add_edge("snapshot", END)
    return graph.compile(checkpointer=saver)


class _ChartHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        app = self.server.app
        caller = app.tokens.get(self.headers.get("Authorization"))
        if not caller or self.path != "/handoff":
            self.send_error(403)
            return
        question = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["question"]
        charts = app.reopen() if question == QUESTION else []
        body = json.dumps({"route": ROUTE, "caller": caller, "question": question, "charts": charts}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ChartApplication:
    """Each read opens the saved conversations through a fresh SQLite saver and graph."""

    def __init__(self, folder, subject, control, journal, *, guarded):
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.store.memory import InMemoryStore
        from .adapters import LangGraphMemoryAdapter, LangGraphSQLiteCheckpointAdapter, SQLiteAdapter

        self.folder = Path(folder)
        self.subject, self.control, self.journal = subject, control, journal
        self.guarded = guarded
        self.tokens = {"Bearer " + token_urlsafe(24): caller for caller in CALLERS}
        self.source = sqlite3.connect(self.folder / "source.sqlite", check_same_thread=False)
        self.source.execute("CREATE TABLE charts(id TEXT PRIMARY KEY, setrya_subject TEXT NOT NULL, chart TEXT NOT NULL)")
        self.connection = sqlite3.connect(self.folder / "chats.sqlite", check_same_thread=False)
        self.saver = SqliteSaver(self.connection)
        self.memory = InMemoryStore()
        self.graph = _graph(self.saver)
        self.adapters = {
            "charts": SQLiteAdapter(self.source, "charts"),
            "checkpoints": LangGraphSQLiteCheckpointAdapter(self.saver),
            "memory": LangGraphMemoryAdapter(self.memory),
        }

    def __enter__(self):
        # Bind to literal loopback. TCPServer avoids a reverse DNS dependency on startup.
        self.http = TCPServer(("127.0.0.1", 0), _ChartHandler)
        self.http.app = self
        self.server_thread = Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.server_thread.start()
        return self

    def __exit__(self, *_):
        self.http.shutdown()
        self.server_thread.join()
        self.http.server_close()
        self.connection.close()
        self.source.close()

    def seed(self):
        from .hooks import CopyHooks

        for subject, key, text in ((self.subject, "chart-a", ALICE), (self.control, "chart-b", BOB)):
            with self.source:
                self.source.execute("INSERT INTO charts VALUES(?, ?, ?)", (key, subject.ref, text))
            self.journal.record(subject, store="charts", key=key, source="admission", action="stored")
            hooks = CopyHooks(self.journal, subject)
            hooks.put_memory(self.memory, "handoff", {"chart": text}, store="memory", source=key)
            hooks.invoke_graph(self.graph, {"chart": text}, thread="chat", store="checkpoints", source=key)

    def snapshot_job(self, subject, before_write):
        from .hooks import CopyHooks

        key, text = self.source.execute(
            "SELECT id, chart FROM charts WHERE setrya_subject = ?", (subject.ref,)
        ).fetchone()
        before_write()  # The worker holds the old chart locally while deletion runs.
        if self.guarded and not self.source.execute(
            "SELECT 1 FROM charts WHERE setrya_subject = ?", (subject.ref,)
        ).fetchone():
            return
        CopyHooks(self.journal, subject).invoke_graph(
            self.graph, {"chart": text}, thread="chat", store="checkpoints", source=key
        )

    def reopen(self):
        from contextlib import closing
        from langgraph.checkpoint.sqlite import SqliteSaver
        from .adapters import checkpoint_thread

        with closing(sqlite3.connect(self.folder / "chats.sqlite", check_same_thread=False)) as db:
            graph = _graph(SqliteSaver(db))
            result = []
            for subject in (self.subject, self.control):
                config = {"configurable": {"thread_id": checkpoint_thread(subject, "chat")}}
                value = graph.get_state(config).values.get("chart")
                if value:
                    result.append(value)
            return result

    def read(self, subject, caller, question):
        from . import Read

        token = next(token for token, principal in self.tokens.items() if principal == caller)
        client = http.client.HTTPConnection(*self.http.server_address, timeout=3)
        try:
            client.request("POST", "/handoff", json.dumps({"question": question}), {"Authorization": token})
            response = client.getresponse()
            if response.status != 200:
                raise RuntimeError("the authenticated chart endpoint did not return an observation")
            data = json.load(response)
        finally:
            client.close()
        return Read(data["caller"], data["route"], data["question"], ALICE in data["charts"], BOB in data["charts"])


def run_case(output, *, guarded, opaque=False):
    from . import ErasureCase, run_erasure
    from .journal import CopyJournal
    from .subject import Subject

    with TemporaryDirectory(prefix="setrya-chart-app-") as scratch:
        folder = Path(scratch)
        inventory = folder / "inventory.toml"
        inventory.write_text(INVENTORY + (PROVIDER_INVENTORY if opaque else ""), encoding="utf-8")
        secret = token_bytes(32)
        alice = Subject.derive(secret, "chart-lab", "fictional-alice", "restricted")
        bob = Subject.derive(secret, "chart-lab", "fictional-bob", "restricted")
        journal = CopyJournal(folder / "copies.sqlite")
        with ChartApplication(folder, alice, bob, journal, guarded=guarded) as app:
            app.seed()
            if opaque:
                # Synthetic configuration observation. The local drill makes no provider request.
                journal.record(alice, store="model-observer", key="inference-1", source="chart-a", action="sent",
                               details={"evidence_level": "configuration", "provider": "anthropic",
                                        "model": "fixture", "prompt_caching": False, "invocation_logging": None})
            with WorkerFixture(app.snapshot_job, app.read) as held:
                return run_erasure(alice, journal=journal, inventory=inventory, adapters=app.adapters,
                                   application=held, case=ErasureCase(ROUTE, QUESTION, CALLERS), output=Path(output))


def run_demo(output: Path) -> int:
    try:
        import langgraph.checkpoint.sqlite  # noqa: F401
    except ImportError:
        from setrya.drill import InputError
        raise InputError("install the erasure extra with python -m pip install 'setrya[erasure]'") from None
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    print("Alice and Bob are fictional patients. Two care-team members reopen the saved handoff.")
    for mode, expected in (("broken", "FAIL"), ("guarded", "PASS"), ("opaque", "NOT TESTED")):
        receipt = run_case(output / mode, guarded=mode != "broken", opaque=mode == "opaque")
        verdict = receipt["verdict"]
        unknown = receipt['unknown_copies']
        print(f"{mode}: {verdict} · {receipt['time_to_first_verdict_ms']} ms drill duration · "
              f"{unknown} unknown {'copy' if unknown == 1 else 'copies'}")
        evidence = json.loads((output / mode / "erasure-evidence.json").read_bytes())
        if evidence["worker"]["drained"]:
            for read in evidence["reads"]["final"]:
                if read and type(read["subject_present"]) is bool and type(read["control_present"]) is bool:
                    observed = [name for name, present in (("Alice", read["subject_present"]),
                                                           ("Bob", read["control_present"])) if present]
                    print(f"  {read['caller']} reopened the chat after the worker drained: "
                          f"{' and '.join(observed) or 'neither chart'}.")
        print(f"  Replay: setrya replay {quote(str(output / mode))}")
        if verdict != expected:
            return 1
    return 0
