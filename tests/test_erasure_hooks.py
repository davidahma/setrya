"""Exercise real callback boundaries with synthetic payloads; the provider transports stay local."""
import asyncio
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, SkipTest
from unittest.mock import patch

from setrya.drill import InputError
from setrya.erasure.cache import SubjectAnswerCacheAdapter
from setrya.erasure.hooks import CopyHooks
from setrya.erasure.journal import CopyJournal
from setrya.erasure.subject import Subject
from setrya.local_cache import CacheKey, LocalAnswerCache
from setrya.store_signals import passage_metadata


def check_revisions(case, cache, key):
    a, b = [passage_metadata("chart", revision, "one") for revision in ("A", "B")]
    case.assertFalse(cache.publish(cache.begin(key), "unknown", chunks=[a]))
    case.assertEqual(cache.set_document_revision(key.tenant, "chart", "A"), 0)
    case.assertTrue(cache.publish(cache.begin(key), "first", chunks=[a]))
    case.assertEqual(cache.set_document_revision(key.tenant, "chart", "A"), 0)
    case.assertEqual(cache.get(key).answer, "first")
    case.assertEqual(cache.set_document_revision(key.tenant, "chart", "B"), 1)
    # Retrieval from a stale replica predates a fresh admission.
    case.assertIsNone(cache.fill(key, lambda: [a], lambda _: "late A"))
    case.assertTrue(cache.publish(cache.begin(key), "current", chunks=[b]))
    case.assertEqual(cache.set_document_revision(key.tenant, "chart", None), 1)
    case.assertFalse(cache.publish(cache.begin(key), "deleted", chunks=[b]))
    case.assertIsNone(cache.get(key))


class ErasureHooksTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "copies.sqlite"
        self.journal = CopyJournal(self.path)
        self.subject = Subject.derive(b"t" * 32, "clinic", "fictional-alice", "clinical notes")
        self.hooks = CopyHooks(self.journal, self.subject, fingerprint_key=b"m" * 32)

    def test_pseudonyms_context_propagation_and_parallel_subjects(self):
        ref = self.subject.ref
        self.assertEqual(len(ref), 64)
        self.assertEqual(Subject.derive(b"t" * 32, "clinic", "fictional-alice", "public").ref, ref)
        other = Subject.derive(b"t" * 32, "other", "fictional-alice", "clinical notes")
        self.assertNotEqual(other.ref, ref)
        self.assertNotEqual(Subject.derive(b"o" * 32, "clinic", "fictional-alice", "clinical notes").ref, ref)
        with self.assertRaises(InputError):
            Subject.derive(b"oops", "clinic", "fictional-alice", "label")
        with self.subject.activate():
            self.assertEqual(Subject.current(), self.subject)
            with other.activate():
                self.assertEqual(Subject.current(), other)
            self.assertEqual(Subject.current(), self.subject)
        with self.assertRaises(InputError):
            Subject.current()
        with self.assertRaises(InputError):
            other.headers(self.subject.metadata())
        with self.assertRaises(InputError):
            self.subject.langgraph_config("chat", {"configurable": {"thread_id": "someone-else"}})
        config = self.subject.langgraph_config("chat", {"tags": ["trial"], "metadata": {"app": "test"}})
        self.assertEqual(Subject.from_metadata(config["metadata"]), self.subject)
        self.assertEqual(config["tags"], ["trial"])
        self.assertEqual(config["configurable"]["thread_id"], f"setrya:{ref}:chat")
        self.assertNotIn("fictional-alice", str(config))
        self.assertNotIn("clinic:", repr(self.subject))
        async def bound(subject):
            with subject.activate():
                await asyncio.sleep(0)
                return Subject.current()
        async def run():
            return await asyncio.gather(bound(self.subject), bound(other))
        self.assertEqual(asyncio.run(run()), [self.subject, other])

    def test_otel_wire_propagation_and_copy_span_event(self):
        try:
            from opentelemetry import trace
            from opentelemetry.baggage.propagation import W3CBaggagePropagator
        except ImportError:
            raise SkipTest("OpenTelemetry API required")
        wire = {}
        propagator = W3CBaggagePropagator()
        propagator.inject(wire, context=self.subject.otel_context())
        self.assertNotIn("fictional-alice", str(wire))
        self.assertEqual(Subject.from_baggage(propagator.extract(wire)), self.subject)
        journal = CopyJournal(self.path, export_trace_events=True)
        with patch.object(trace, "get_current_span") as current:
            journal.record(self.subject, store="cache", key="k", action="derived", source="chart")
        event, attributes = current.return_value.add_event.call_args.args
        self.assertEqual(event, "setrya.copy.v1")
        self.assertEqual(Subject.from_metadata(attributes), self.subject)
        self.assertEqual(attributes["source"], "chart")

    def test_real_celery_task_reads_propagated_headers(self):
        try:
            from celery import Celery
        except ImportError:
            raise SkipTest("Celery required")
        celery = Celery("subject-trial", broker="memory://")
        self.addCleanup(celery.close)
        @celery.task(bind=True)
        def receive(task):
            with Subject.from_metadata(task.request.headers).activate():
                return Subject.current().ref
        self.assertEqual(receive.apply(headers=self.subject.headers({"trace": "one"})).get(), self.subject.ref)

    def test_journal_separates_history_subjects_and_locations(self):
        other = Subject.derive(b"t" * 32, "clinic", "fictional-bob", "routine")
        for subject in (self.subject, other, self.subject):
            self.journal.record(subject, store="charts", key="row-1", source="import", action="stored")
        self.assertEqual(len(self.journal.copies(self.subject)), 1)
        self.assertEqual(len(CopyJournal(self.path).history(self.subject)), 2)
        self.assertEqual(self.journal.history(other)[0].classification, "routine")
        with self.assertRaises(InputError):
            self.journal.history(self.subject, max_bytes=1)
        with self.assertRaises(InputError):
            self.journal.record(other, store="charts", key="row-1", source="source", action="sent",
                                details={"prompt": "never store free text"})
        claim = self.journal.record(other, store="aws", key="call", source="chart", action="sent",
                                    details={"provider": "bedrock"})
        self.assertEqual(claim.details["evidence_level"], "configuration")
        with self.assertRaises(InputError):
            self.journal.record(other, store="aws", key="call", source="chart", action="sent",
                                details={"provider": "bedrock", "evidence_level": "direct"})
        self.assertNotIn(b"fictional-alice", self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_provider_boundaries_preserve_uncertain_sends_and_only_store_configuration(self):
        inputs = {"model": "claude-test", "messages": [{"role": "user", "content": "synthetic private text"}],
                  "max_tokens": 8, "system": [{"type": "text", "text": "private", "cache_control": {"type": "ephemeral"}}]}
        calls = []
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **r: calls.append(r) or "result"))
        self.assertEqual(self.hooks.anthropic(client, inputs, key="request-1", store="anthropic", source="chart",
                                             sources=["chunk-a"]), "result")
        self.assertEqual(calls, [inputs])
        with self.assertRaises(InputError):
            self.hooks.anthropic(client, inputs, key="bad-config", store="anthropic", source="chart", prompt_caching=False)
        def transport_timeout(**kwargs):
            raise TimeoutError
        client = SimpleNamespace(meta=SimpleNamespace(region_name="ca-central-1"), converse=transport_timeout)
        with self.assertRaises(TimeoutError):
            self.hooks.bedrock(client, {"modelId": "test-model", "messages": []}, store="aws-model", key="call-2",
                               source="chart", invocation_logging=True)
        first, second = self.journal.copies(self.subject)
        self.assertTrue(first.details["prompt_caching"])
        self.assertIsNone(first.details["invocation_logging"])
        self.assertEqual(first.details["sources"], ["chunk-a"])
        self.assertEqual(first.details["components"], ["messages", "system"])
        self.assertEqual(first.details["evidence_level"], "configuration")
        self.assertEqual(second.details["stage"], "attempted")
        self.assertEqual(second.details["region"], "ca-central-1")
        self.assertTrue(second.details["invocation_logging"])
        self.assertNotIn(b"synthetic private text", self.path.read_bytes())
        self.assertEqual(len(first.details["payload_hmac"]), 64)
        other = Subject.derive(b"t"*32, "clinic", "fictional-bob", "restricted")
        other_hooks = CopyHooks(self.journal, other, fingerprint_key=b"m"*32)
        self.assertNotEqual(other_hooks._fingerprint(inputs)["payload_hmac"], first.details["payload_hmac"])

    def test_mcp_records_input_and_result_and_preserves_returned_object(self):
        class Session:
            async def call_tool(self, name, arguments):
                return {"content": [{"text": arguments["value"]}]}
        result = asyncio.run(self.hooks.call_tool(Session(), "inspect", {"value": "synthetic secret"},
                                                  key="tool-1", store="mcp", source="chart"))
        self.assertEqual(result["content"][0]["text"], "synthetic secret")
        rows = self.journal.copies(self.subject)
        self.assertEqual([r.key for r in rows], ["tool-1/input", "tool-1/result"])
        self.assertNotEqual(rows[0].details["payload_hmac"], rows[1].details["payload_hmac"])
        self.assertNotIn(b"synthetic secret", self.path.read_bytes())

    def test_langchain_store_retriever_and_chat_model(self):
        try:
            from langchain_core.documents import Document
            from langchain_core.embeddings import Embeddings
            from langchain_core.retrievers import BaseRetriever
            from langchain_core.language_models.fake_chat_models import FakeListChatModel
            from langchain_core.vectorstores import InMemoryVectorStore
        except ImportError:
            raise SkipTest("LangChain required")
        from setrya.integrations.langchain import tag_documents
        class Embedding(Embeddings):
            def embed_documents(self, texts):
                return [[1.0, 1.0] for _ in texts]
            def embed_query(self, text):
                return [1.0, 1.0]
        db = InMemoryVectorStore(embedding=Embedding())
        class ScopedRetriever(BaseRetriever):
            def _get_relevant_documents(self, query, *, run_manager):
                return db.get_by_ids(["vector-1"])
        docs = tag_documents([Document(page_content="synthetic private note")], document="chart", revision="A",
                             subject=self.subject)
        self.assertEqual(self.hooks.add_documents(db, docs, ids=["vector-1"], store="vectors"), ["vector-1"])
        result, _ = self.hooks.retrieve(ScopedRetriever(), "private note", route="ask", scope="care",
                                        store="vectors")
        self.assertEqual(result[0].page_content, "synthetic private note")
        model = FakeListChatModel(responses=["answer"])
        observer = self.hooks.langchain_callback(store="model", source="vector-1", provider="bedrock", model="fixture",
                                                 region="us-east-1", sources=["vector-1"])
        self.assertEqual(model.invoke("synthetic prompt", config={"callbacks": [observer]}).content, "answer")
        self.assertEqual([r.action for r in self.journal.history(self.subject)],
                         ["stored", "stored", "retrieved", "sent"])
        self.assertNotIn(b"synthetic", self.path.read_bytes())

    def test_cache_has_direct_subject_read_and_delete(self):
        strict = LocalAnswerCache(require_current_sources=True)
        key = CacheKey("clinic", "doctor", self.subject.ref, "handoff")
        check_revisions(self, strict, key)
        strict.set_document_revision("clinic", "chart", "C")
        fresh = passage_metadata("chart", "C", "one", subject=self.subject)
        answer = self.hooks.fill_cache(strict, key, lambda: [fresh], lambda _: "chart snapshot",
                                       store="answers", source="chart")
        self.assertEqual(answer.answer, "chart snapshot")
        adapter = SubjectAnswerCacheAdapter(strict, tenant="clinic")
        self.assertEqual(adapter.find(self.subject), {"clinic:doctor:handoff"})
        row, = self.journal.copies(self.subject)
        self.assertEqual(row.key, "clinic:doctor:handoff")
        self.assertEqual(row.details["sources"], [fresh["setrya_marker"]])
        adapter.delete(self.subject)
        self.assertFalse(adapter.verify_absent(self.subject))
        self.assertIsNone(strict.get(key))

    def test_native_provider_clients_with_local_transports(self):
        try:
            import boto3
            from botocore.stub import Stubber
            from anthropic import Anthropic
        except ImportError:
            raise SkipTest("Anthropic and boto3 SDKs required")
        with closing(boto3.client("bedrock-runtime", region_name="us-east-2", aws_access_key_id="fixture",
                                  aws_secret_access_key="unused")) as bedrock, Stubber(bedrock) as stub:
            request = {"modelId": "trial", "messages": [{"role": "user", "content": [{"text": "synthetic"}]}]}
            reply = {"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                     "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                     "metrics": {"latencyMs": 1}}
            stub.add_response("converse", reply, request)
            actual = self.hooks.bedrock(bedrock, request, key="req-2", store="bedrock", source="chart",
                                        prompt_caching=False, invocation_logging=True)
            self.assertEqual(actual["output"], reply["output"])
            stub.assert_no_pending_responses()
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                calls.append((self.path, json.loads(self.rfile.read(int(self.headers["content-length"])))))
                body = json.dumps({
                    "id": "msg-local", "type": "message", "role": "assistant", "model": "trial-model",
                    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1}
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                with Anthropic(api_key="unused", base_url=f"http://127.0.0.1:{server.server_port}",
                               timeout=3.0, max_retries=0) as client:
                    response = self.hooks.anthropic(client, {"model": "trial-model", "max_tokens": 8,
                                                    "messages": [{"role": "user", "content": "synthetic"}]},
                                                    key="req-3", store="anthropic", source="chart")
                    self.assertEqual(response.content[0].text, "ok")
            finally:
                server.shutdown()
                thread.join(timeout=3)
        self.assertEqual(calls[0][0], "/v1/messages")
        ant, aws = self.journal.copies(self.subject)
        self.assertIsNone(ant.details["region"])
        self.assertEqual(aws.details["region"], "us-east-2")
        self.assertNotIn(b"synthetic", self.path.read_bytes())

    def test_native_mcp_client_and_tool_over_memory_transport(self):
        try:
            from mcp.server.fastmcp import FastMCP
            from mcp.shared.memory import create_connected_server_and_client_session
        except ImportError:
            raise SkipTest("MCP SDK required")
        server = FastMCP("local-trial")
        @server.tool()
        def transform(note: str) -> str:
            return note.upper()
        async def run():
            async with create_connected_server_and_client_session(server) as client:
                return await self.hooks.call_tool(client, "transform", {"note": "fictional-note"},
                                                  key="tool-1", store="mcp", source="chart")
        result = asyncio.run(run())
        self.assertIn("FICTIONAL-NOTE", [part.text for part in result.content])
        self.assertEqual([r.details["direction"] for r in self.journal.copies(self.subject)], ["input", "result"])
        self.assertNotIn(b"fictional-note", self.path.read_bytes().lower())
