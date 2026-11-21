"""SDK-side runs use local transports. The Langfuse test uses a real self-hosted server."""
import asyncio
import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from setrya.drill import InputError
from setrya.erasure.hooks import CopyHooks
from setrya.erasure.journal import CopyJournal
from setrya.erasure.subject import Subject
from setrya.integrations.agent_harnesses import (
    ClaudeTranscriptCanaryScanner, LangfuseCanaryScanner, OpenAISessionCanaryScanner, claude_tool_hooks,
    langfuse_observation, openai_response, run_openai_agent,
)

MARKER = "fixture:lost-source-6732"


class HarnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.subject = Subject.derive(b"s" * 32, "workspace", "fixture", "restricted")
        self.journal = CopyJournal(self.root / "journal.sqlite")
        self.hooks = CopyHooks(self.journal, self.subject)

    @unittest.skipUnless(importlib.util.find_spec("agents"), "install agent-openai")
    def test_native_runner_local_session_and_responses_request(self):
        import httpx
        from agents import Agent, Model, ModelResponse, RunConfig, SQLiteSession, Usage
        from openai import AsyncOpenAI, OpenAI
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        class FixtureModel(Model):
            async def get_response(self, *args, **kwargs):
                message = ResponseOutputMessage(id="msg_fixture", type="message", role="assistant", status="completed",
                                                content=[ResponseOutputText(text=MARKER, annotations=[], type="output_text")])
                return ModelResponse(output=[message], usage=Usage(), response_id="resp_fixture")

            async def stream_response(self, *args, **kwargs):
                raise AssertionError("fixture uses a non-streaming run")

        path = self.root / "sessions.db"
        session = SQLiteSession("workspace-chat", path)
        self.addCleanup(session.close)
        result = asyncio.run(run_openai_agent(self.hooks, Agent(name="support", model=FixtureModel()), "Which issue?",
                                               session=session, store="chat", source="ticket",
                                               run_config=RunConfig(tracing_disabled=True)))
        self.assertEqual(result.final_output, MARKER)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        scan = OpenAISessionCanaryScanner(db)
        self.assertEqual(len(scan.find(scope="workspace-chat", marker=MARKER)), 1)
        asyncio.run(session.clear_session())
        self.assertEqual(scan.find(scope="workspace-chat", marker=MARKER), set())

        def transport(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.assertEqual(json.loads(request.content)["input"], MARKER)
            return httpx.Response(200, json={"id": "resp_1", "object": "response", "model": "fixture-model",
                                             "output": [], "created_at": 1, "parallel_tool_calls": False,
                                             "tool_choice": "none", "tools": [], "status": "completed"})

        client = OpenAI(api_key="fixture", http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        self.addCleanup(client.close)
        response = openai_response(self.hooks, client, {"model": "fixture-model", "input": MARKER},
                                   store="provider", key="request", source="ticket")
        self.assertEqual(response.id, "resp_1")
        copy = next(row for row in self.journal.copies(self.subject) if row.store == "provider")
        self.assertEqual(copy.details["components"], ["input"])
        self.assertEqual(copy.details["evidence_level"], "configuration")
        self.assertNotIn(MARKER, repr(copy))

        async_client = AsyncOpenAI(api_key="fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        try:
            with self.assertRaisesRegex(InputError, "synchronous"):
                openai_response(self.hooks, async_client, {"model": "fixture-model", "input": MARKER},
                                store="provider", key="async-request", source="ticket")
            self.assertFalse(any(record.key == "async-request" for record in self.journal.copies(self.subject)))
        finally:
            asyncio.run(async_client.close())

    @unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"), "install agent-claude")
    def test_native_claude_client_dispatches_both_hooks(self):
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Transport
        session = str(uuid4())
        hooks = claude_tool_hooks(self.hooks, session=session, store="tools", source="ticket")

        class FixtureTransport(Transport):
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.results = asyncio.Queue()

            async def connect(self):
                pass

            async def close(self):
                pass

            async def end_input(self):
                pass

            def is_ready(self):
                return True

            async def read_messages(self):
                while True:
                    yield await self.incoming.get()

            async def write(self, data):
                msg = json.loads(data)
                if msg["type"] == "control_request":
                    self.callbacks = msg["request"]["hooks"]
                    await self.incoming.put({"type": "control_response", "response": {"subtype": "success",
                                             "request_id": msg["request_id"], "response": {}}})
                elif msg["type"] == "control_response":
                    await self.results.put(msg["response"])

            async def emit(self, name):
                callback = self.callbacks[name][0]["hookCallbackIds"][0]
                payload = {"session_id": session, "hook_event_name": name, "tool_name": "read_ticket",
                           "tool_input": {"ticket": "fixture"}, "tool_response": MARKER}
                await self.incoming.put({"type": "control_request", "request_id": name, "request": {
                    "subtype": "hook_callback", "callback_id": callback, "tool_use_id": "tool-1", "input": payload}})
                return await asyncio.wait_for(self.results.get(), timeout=5)

        async def run():
            t = FixtureTransport()
            async with ClaudeSDKClient(options=ClaudeAgentOptions(hooks=hooks), transport=t):
                for name in hooks:
                    self.assertEqual((await t.emit(name))["subtype"], "success")

        asyncio.run(run())
        self.assertEqual({row.key for row in self.journal.copies(self.subject)},
                         {session + "/tool-1/input", session + "/tool-1/result"})
        self.assertNotIn(MARKER, repr(self.journal.copies(self.subject)))
        transcript = self.root / (session + ".jsonl")
        transcript.write_text(json.dumps({"type": "assistant", "sessionId": session, "message": {"content": MARKER}}))
        scanner = ClaudeTranscriptCanaryScanner(self.root)
        self.assertEqual(scanner.find(scope=session, marker=MARKER), {session})
        transcript.write_text(json.dumps({"type": "assistant", "sessionId": session, "message": {"content": "public"}}))
        self.assertEqual(scanner.find(scope=session, marker=MARKER), set())
        transcript.unlink()
        with self.assertRaises(FileNotFoundError):
            scanner.find(scope=session, marker=MARKER)

    @unittest.skipUnless(os.getenv("SETRYA_LANGFUSE_URL") and importlib.util.find_spec("langfuse"), "configure local Langfuse")
    def test_live_trace_io_discovery_and_deletion(self):
        from langfuse import Langfuse
        client = Langfuse(base_url=os.environ["SETRYA_LANGFUSE_URL"], public_key="pk-lf-setrya-fixture",
                          secret_key="sk-lf-setrya-fixture")
        session = str(uuid4())

        def emit(scope, output):
            with langfuse_observation(self.hooks, client, session=scope, store="traces", source="ticket",
                                      name="fixture-summary", input="summarize", output=output) as span:
                return f"{span.trace_id}/{span.id}"

        control = emit(session, "public")
        stale = emit(session, {"summary": MARKER})
        other = emit(str(uuid4()), MARKER)
        client.flush()
        self.addCleanup(client.api.trace.delete, control.split("/")[0])
        self.addCleanup(client.api.trace.delete, other.split("/")[0])
        scan = LangfuseCanaryScanner(client, project_id="setrya-fixture", control=control)

        def wait_for(expected):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    if scan.find(scope=session, marker=MARKER) == expected:
                        return
                except InputError as exc:
                    if "retained Langfuse observation is missing" not in str(exc):
                        raise
                time.sleep(1)
            self.fail("Langfuse did not reach the expected state with the control present")

        wait_for({stale})
        client.api.trace.delete(stale.split("/")[0])
        wait_for(set())
        self.assertEqual({row.key for row in self.journal.copies(self.subject)}, {control, stale, other})
        with self.assertRaisesRegex(InputError, "retained"):
            scan.find(scope="unknown-session", marker=MARKER)
