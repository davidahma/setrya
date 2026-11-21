"""Async application trial with independent checkpoint, long-term store and authenticated ASGI reads."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from typing import TypedDict
import unittest

from setrya.erasure import Drain, Hold, Subject
from setrya.erasure.adapters import checkpoint_thread, memory_namespace
from setrya.erasure.asyncio import AsyncPostgresCheckpointAdapter, AsyncPostgresMemoryAdapter, arun_shared
from setrya.erasure.journal_merge import CollectedJournal, WorkerJournal

try:
    HAS_GRAPH = find_spec("langgraph.checkpoint.postgres.aio") and find_spec("langgraph.graph")
except ModuleNotFoundError:
    HAS_GRAPH = False


class Finding(TypedDict):
    report: str


CASE_ID = "investigation-202"
ROUTE = "/case/202"
PEOPLE = ("first-reviewer", "second-reviewer")
QUESTIONS = ("Which device was quarantined?", "Which login was reset?")
TARGET = "device-9312"
CONTROL = "account-4021"
CONFIG = {"configurable": {"thread_id": CASE_ID}}


def tag(value):
    return sha256(value.encode()).hexdigest() if value is not None else None


class Investigation:
    """One shared graph thread and one shared cached answer, each with an independently readable history."""
    def __init__(self, db, alice, bob):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore
        from langgraph.graph import END, START, StateGraph
        self.saver, self.store = AsyncPostgresSaver(db), AsyncPostgresStore(db)
        builder = StateGraph(Finding)
        builder.add_node("summary", lambda st: {"report": st["report"]})
        builder.add_edge(START, "summary")
        builder.add_edge("summary", END)
        self.graph = builder.compile(checkpointer=self.saver)
        self.src = AsyncPostgresMemoryAdapter(self.store)
        self.alice, self.bob = alice, bob
        self.tokens = {c: token_hex(16) for c in PEOPLE}
        self.worker = None
        self.writers = {}

    async def seed(self):
        await self.saver.setup()
        await self.store.setup()
        await self.store.aput(memory_namespace(self.alice), "finding-a", {"text": TARGET})
        await self.store.aput(memory_namespace(self.bob), "finding-b", {"text": CONTROL})
        await self.rebuild(TARGET + " " + CONTROL)

    async def rebuild(self, text):
        # Replacing the latest checkpoint alone can leave a readable historical snapshot.
        await self.saver.adelete_thread(CASE_ID)
        await self.graph.ainvoke({"report": text}, CONFIG)
        await self.store.aput(("investigations", CASE_ID), "cached-summary", {"text": text})
        if TARGET in text and self.writers:
            self.writers["worker"].record(self.alice, store="cache", key="cached-summary",
                                          source="finding-a", action="derived")

    async def inspect(self, subject, store, key):
        from setrya.erasure.v3 import Stored
        if store == "checkpoints":
            snapshots = [s.values.get("report", "") async for s in self.graph.aget_state_history(CONFIG)]
            value = " ".join(snapshots) if snapshots else None
        elif store == "cache":
            row = await self.store.aget(("investigations", CASE_ID), key)
            value = row.value["text"] if row else None
        else:
            person = self.alice if key == "finding-a" else self.bob
            row = await self.store.aget(memory_namespace(person), key)
            value = row.value["text"] if row else None
        return Stored(key, value is not None, TARGET in (value or ""), CONTROL in (value or ""), tag(value))

    async def erase(self, subject):
        await self.src.delete(subject)
        row = await self.store.aget(memory_namespace(self.bob), "finding-b")
        await self.rebuild(row.value["text"])

    async def hold(self, subject):
        self.ready, self.release = asyncio.Event(), asyncio.Event()

        async def work():
            before = await self.store.aget(("investigations", CASE_ID), "cached-summary")
            self.ready.set()
            await asyncio.wait_for(self.release.wait(), 15)
            if not self.guard or await self.src.find(subject):
                await self.rebuild(before.value["text"])
        self.worker = asyncio.create_task(work())
        await asyncio.wait_for(self.ready.wait(), 5)
        return Hold("summary-worker")

    async def checkpoint(self, subject):
        if self.worker and not self.worker.done() and not self.release.is_set():
            return Hold("summary-worker")

    async def release_and_drain(self, subject, attempt):
        self.release.set()
        await self.worker
        return Drain(attempt, True)

    async def endpoint(self, scope, receive, send):
        """ASGI request auth and the actual graph/answer read used by both callers."""
        headers = dict(scope["headers"])
        caller = next(c for c, token in self.tokens.items() if headers.get(b"authorization") == token.encode())
        question = json.loads((await receive())["body"])["question"]
        case_id = scope["path_params"]["case"]
        state = await self.graph.aget_state({"configurable": {"thread_id": case_id}})
        cached = await self.store.aget(("investigations", case_id), "cached-summary")
        answer = state.values.get("report", "") + " " + (cached.value["text"] if cached else "")
        wanted = TARGET if question == QUESTIONS[0] else CONTROL
        body = {"caller": caller, "route": scope["path"], "conversation": case_id, "question": question,
                "answer": wanted if wanted in answer else "unknown"}
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})

    async def read(self, subject, caller, conversation, question):
        from setrya.erasure.v3 import Answer
        async def receive():
            return {"type": "http.request", "body": json.dumps({"question": question}).encode()}
        messages = []
        async def send(message):
            messages.append(message)
        await self.endpoint({"type": "http", "method": "POST", "path": ROUTE, "path_params": {"case": conversation},
                             "headers": [(b"authorization", self.tokens[caller].encode())]}, receive, send)
        assert messages[0]["status"] == 200
        result = json.loads(messages[1]["body"])
        if TARGET in result["answer"] and self.writers:
            self.writers["api"].record(subject, store="checkpoints", key=CASE_ID,
                                       source="finding-a", action="retrieved")
        return Answer(result["caller"], result["conversation"], result["route"], result["question"],
                      TARGET in result["answer"], CONTROL in result["answer"])

    async def collect_journals(self, subject):
        for producer, journal in self.writers.items():
            path = self.collector.path.parent / producer
            digest = await asyncio.to_thread(journal.seal, subject, path)
            await asyncio.to_thread(self.collector.ingest, path, producer=producer, expected_sha256=digest)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_hook_can_use_last_default_executor_worker_and_request_context(self):
        from setrya.erasure.asyncio import _run

        marker = ContextVar("marker")
        marker.set("request")
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))

        class Reader:
            async def read(self):
                return await asyncio.to_thread(marker.get)

        def runner(subject, *, application):
            return application.read()

        result = await asyncio.wait_for(_run(runner, None, application=Reader()), timeout=2)
        self.assertEqual(result, "request")

    async def test_cancel_during_baseline_releases_worker_without_erasing(self):
        from setrya.erasure.v3 import Artifact, SharedCase
        reading, released = asyncio.Event(), asyncio.Event()
        case = SharedCase(CASE_ID, ROUTE, PEOPLE, *QUESTIONS, (Artifact("notes", CASE_ID, "rewrite"),))

        class Paused:
            async def hold(self, subject):
                return Hold("attempt")

            async def release_and_drain(self, subject, attempt):
                released.set()
                return Drain(attempt, True)

            async def checkpoint(self, subject):
                return Hold("attempt")

            async def read(self, *args):
                reading.set()
                await asyncio.Event().wait()

            async def inspect(self, *args):
                raise AssertionError("cancelled stage performed I/O")

            async def erase(self, subject):
                raise AssertionError("erased after cancellation")

        with TemporaryDirectory() as scratch:
            task = asyncio.create_task(arun_shared(Subject("1" * 64, "private"), case=case, application=Paused(),
                                                   output=Path(scratch) / "run"))
            await asyncio.wait_for(reading.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(released.is_set())


@unittest.skipUnless(os.getenv("SETRYA_PGVECTOR_DSN") and HAS_GRAPH, "configure a disposable LangGraph PostgreSQL service")
class AsyncPostgresTrial(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.db = await psycopg.AsyncConnection.connect(os.environ["SETRYA_PGVECTOR_DSN"], autocommit=True, row_factory=dict_row)
        self.addAsyncCleanup(self.db.close)
        schema = "erasure_async_" + token_hex(8)
        await self.db.execute("CREATE SCHEMA " + schema)
        self.addAsyncCleanup(self.db.execute, "DROP SCHEMA " + schema + " CASCADE")
        await self.db.execute("SET search_path TO " + schema + ", public")
        self.a, self.b = Subject("1" * 64, "restricted"), Subject("2" * 64, "restricted")
        self.app = Investigation(self.db, self.a, self.b)
        await self.app.seed()

    async def test_shared_investigation_and_distributed_evidence(self):
        from setrya.erasure.v3 import Artifact, SharedCase
        case = SharedCase(CASE_ID, ROUTE, PEOPLE, *QUESTIONS, (
            Artifact("sources", "finding-a", "delete"), Artifact("sources", "finding-b", "preserve"),
            Artifact("checkpoints", CASE_ID, "rewrite"), Artifact("cache", "cached-summary", "rewrite")))
        for mode, guarded, expected in (("late-write", False, "FAIL"), ("late-write", True, "PASS"),
                                        ("deletion", True, "PASS"), ("deletion", True, "NOT TESTED")):
            with self.subTest(mode=mode, outcome=expected), TemporaryDirectory() as scratch:
                self.app.guard = guarded
                out = Path(scratch)
                self.app.collector = CollectedJournal(out / "review.db", run_id="case")
                self.app.writers = {p: WorkerJournal(out / (p + ".db"), run_id="case", producer=p)
                                    for p in ("api", "worker")}
                await self.app.seed()
                producers = ["worker", "api"] + (["index"] if expected == "NOT TESTED" else [])
                receipt = await arun_shared(self.a, case=case, application=self.app, mode=mode, output=out / "evidence",
                                            run_id="case", journal=self.app.collector, expected_producers=producers)
                self.assertEqual(receipt["technical_verdict"], expected, receipt)
                self.assertGreater(len(self.app.collector.history(self.a)), 0)
                self.assertEqual(await self.app.src.find(self.b), {"finding-b"})
                self.assertFalse(await self.app.src.verify_absent(self.a))

    async def test_orphaned_checkpoint_rows_and_retained_subject(self):
        adapter = AsyncPostgresCheckpointAdapter(self.app.saver)
        for p in (self.a, self.b):
            thread = checkpoint_thread(p, "exclusive")
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": "", "checkpoint_id": "fixture"}}
            await self.app.saver.aput_writes(config, [("fact","synthetic")], "pending")
        self.assertEqual(await adapter.find(self.a), {checkpoint_thread(self.a, "exclusive")})
        await adapter.delete(self.a)
        self.assertEqual(await adapter.verify_absent(self.a), set())
        self.assertEqual(await adapter.verify_absent(self.b), {checkpoint_thread(self.b,"exclusive")})
