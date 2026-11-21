"""Independent adopter's disposable dispatch app: an old manifest can repopulate long-term memory."""

import os
from contextlib import contextmanager, closing
import sqlite3
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from langgraph.store.memory import InMemoryStore
from setrya.erasure import Subject
from setrya.erasure.model import ErasureCase, Read
from setrya.erasure.adapters import LangGraphMemoryAdapter, LangGraphSQLiteCheckpointAdapter, memory_namespace
from setrya.erasure.hooks import CopyHooks


CASE = ErasureCase("dispatch", "Which docks need pickups?", ("coordinator", "dispatcher"))
SUBJECT_DOCK = "Synthetic dock at Linden"
CONTROL_DOCK = "Synthetic dock at Bay"
SESSIONS = {"token-A": "coordinator", "token-B": "dispatcher"}


class Manifest(TypedDict):
    dock: str


def _snapshot(state):
    return {"dock": state["dock"]}


class Dispatch:
    def __init__(self, checkpoint, memory, journal, subject, control):
        self.checkpoint, self.memory, self.journal = checkpoint, memory, journal
        self.stops = [subject, control]
        builder = StateGraph(Manifest)
        builder.add_node("capture", _snapshot)
        builder.add_edge(START, "capture")
        builder.add_edge("capture", END)
        self.graph = builder.compile(checkpointer=checkpoint)

    def stops_for_session(self, token, question):
        principal = SESSIONS[token]
        if question != CASE.question:
            raise ValueError("unknown query")
        docks = [row.value["dock"] for stop in self.stops for row in self.memory.search(memory_namespace(stop))]
        return {"route": "dispatch", "query": question, "principal": principal, "stops": docks}


@contextmanager
def open_fixture(subject, journal):
    control = Subject.derive(os.urandom(32), "dispatchlab", "other-stop", "restricted")
    with closing(sqlite3.connect(":memory:", check_same_thread=False)) as conn:
        saver = SqliteSaver(conn)
        memory = InMemoryStore()
        app = Dispatch(saver, memory, journal, subject, control)
        for stop, dock in ((subject, SUBJECT_DOCK), (control, CONTROL_DOCK)):
            hooks = CopyHooks(journal, stop)
            hooks.invoke_graph(app.graph, {"dock": dock}, thread="manifest", store="checkpoints", source="schedule")
            hooks.put_memory(memory, "route", {"dock": dock}, store="memory", source="manifest")
        yield app, {"checkpoints": LangGraphSQLiteCheckpointAdapter(saver), "memory": LangGraphMemoryAdapter(memory)}


def snapshot_job(app, subject, before_write):
    checkpoint = subject.langgraph_config("manifest")
    captured = app.graph.get_state(checkpoint).values["dock"]
    before_write()
    if os.environ.get("DISPATCH_GUARD") == "enabled" and not app.graph.get_state(checkpoint).values:
        return
    CopyHooks(app.journal, subject).put_memory(app.memory, "route", {"dock": captured}, store="memory", source="manifest")


def read_chat(app, subject, caller, question):
    credentials = {"coordinator": "token-A", "dispatcher": "token-B"}
    result = app.stops_for_session(credentials[caller], question)
    return Read(result["principal"], result["route"], result["query"],
                SUBJECT_DOCK in result["stops"], CONTROL_DOCK in result["stops"])
