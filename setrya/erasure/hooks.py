"""Observe copies at the calls that read, write or transmit subject-linked data."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from setrya.dependencies import ChunkDependency
from setrya.drill import InputError
from .journal import CopyJournal, identifier
from .subject import Subject, keyed_digest


def _payload_bytes(value):
    # Keep bytes distinct from every JSON object, including an object whose
    # keys happen to resemble a binary encoding. SDK requests may carry images.
    def typed(item):
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if isinstance(item, (bytes, bytearray)):
            return ["bytes", item.hex()]
        if isinstance(item, Mapping):
            return ["object", [[key, typed(item[key])] for key in sorted(item)]]
        if isinstance(item, (list, tuple)):
            return ["array", [typed(val) for val in item]]
        return ["scalar", item]
    return json.dumps(typed(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _cache_hint(request):
    """Inspect supported SDK cache control fields; a missing directive leaves account settings unknown."""
    def blocks(value):
        return (v for v in value if isinstance(v, Mapping)) if isinstance(value, (tuple, list)) else ()

    def directive(block):
        control = block.get("cache_control")
        point = block.get("cachePoint")
        return (isinstance(control, Mapping) and control.get("type") == "ephemeral"
                or isinstance(point, Mapping) and point.get("type") == "default")

    body = request.get("body", request)
    if isinstance(body, (str, bytes)):
        try:
            body = json.loads(body)
        except (ValueError, UnicodeError):
            return None
    if not isinstance(body, Mapping):
        return None
    candidates = [body, *blocks(body.get("system")), *blocks(body.get("tools"))]
    tool_config = body.get("toolConfig")
    if isinstance(tool_config, Mapping):
        candidates.extend(blocks(tool_config.get("tools")))
    for message in blocks(body.get("messages")):
        candidates.extend(blocks(message.get("content")))
    return True if any(directive(block) for block in candidates) else None


class CopyHooks:
    """One journal and subject per trusted request, job or graph invocation."""

    def __init__(self, journal: CopyJournal, subject: Subject, *, fingerprint_key: bytes | None = None):
        if not isinstance(subject, Subject):
            raise InputError("expected a Subject")
        if fingerprint_key is not None and (not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32):
            raise InputError("use a private fingerprint key of at least 32 bytes")
        self.journal = journal
        self.subject = subject
        self._fingerprint_key = fingerprint_key

    def _record(self, store, key, source, action, **details):
        return self.journal.record(self.subject, store=store, key=key, source=source, action=action, details=details)

    def _fingerprint(self, value):
        if self._fingerprint_key is None:
            return {}
        return {"payload_hmac": keyed_digest(self._fingerprint_key, b"setrya.copy.payload.v1",
                                             self.subject.ref.encode() + b"\x00" + _payload_bytes(value))}

    def _attempt(self, store, key, source, action, **details):
        # A failed transport or process death may still leave a copy. Journal
        # before handing bytes to a different durability or trust boundary.
        self._record(store, key, source, action, stage="attempted", **details)

    def invoke_graph(self, graph, inputs, *, thread: str, store: str, source: str, config: Mapping | None = None):
        config = self.subject.langgraph_config(thread, config)
        key = config["configurable"]["thread_id"]
        self._attempt(store, key, source, "stored")
        with self.subject.activate():
            value = graph.invoke(inputs, config=config)
        self._record(store, key, source, "stored", stage="returned")
        return value

    def put_memory(self, memory, key: str, value: dict, *, store: str, source: str, **options):
        from .adapters import memory_namespace
        self._attempt(store, key, source, "stored")
        value = memory.put(memory_namespace(self.subject), key, value, **options)
        self._record(store, key, source, "stored", stage="returned")
        return value

    def add_documents(self, vector_store, documents, *, ids, store: str, **kwargs):
        """Write tagged LangChain documents to explicit adapter-visible physical IDs."""
        documents, ids = list(documents), list(ids)
        if len(documents) != len(ids) or len(set(ids)) != len(ids):
            raise InputError("supply one distinct vector store ID per document")
        tagged = []
        markers = []
        for doc, key in zip(documents, ids):
            identifier(key, "vector store ID")
            marker = ChunkDependency.from_metadata(doc.metadata).setrya_marker
            tagged.append(doc.model_copy(update={"metadata": self.subject.headers(doc.metadata)}))
            markers.append(marker)
        for key, marker in zip(ids, markers):
            self._attempt(store, key, marker, "stored")
        actual = list(vector_store.add_documents(tagged, ids=ids, **kwargs))
        if len(actual) != len(ids):
            raise InputError("vector store returned an unexpected number of written IDs")
        for key, marker in zip(actual, markers):
            self._record(store, key, marker, "stored", stage="returned")
        if actual != ids:
            raise InputError("vector store replaced the supplied physical IDs; inspect the journal")
        return actual

    def retrieve(self, retriever, query, *, store: str, route: str, scope: str, key=None, config=None, **kwargs):
        """Observe only passages tagged with this data subject, independently of caller identity."""
        from setrya.integrations.langchain import retrieve_with_evidence
        documents, evidence = retrieve_with_evidence(retriever, query, route=route, scope=scope, config=config, **kwargs)
        for doc in documents:
            if doc.metadata.get("setrya_subject") != self.subject.ref:
                continue
            metadata_subject = Subject.from_metadata(doc.metadata)
            marker = ChunkDependency.from_metadata(doc.metadata).setrya_marker
            physical = doc.id if key is None else key(doc)
            self.journal.record(metadata_subject, store=store, key=physical, action="retrieved", source=marker)
        return documents, evidence

    def fill_cache(self, cache, cache_key, retrieve, generate, *, store: str, source: str, **kwargs):
        """Record a possible answer location before fill and its exact dependencies when available."""
        from .cache import answer_location
        key = answer_location(cache_key, self.subject)
        self._attempt(store, key, source, "derived")
        answer = cache.fill(cache_key, retrieve, generate, **kwargs)
        if answer is not None:
            self._record(store, key, source, "derived", stage="returned", sources=answer.dependencies.markers)
        return answer

    def _provider(self, call, request, *, provider, model, region, store, key, source, invocation_logging,
                  prompt_caching, sources, operation):
        if not isinstance(request, Mapping):
            raise InputError("provider request must be a mapping of SDK arguments")
        requested_cache = _cache_hint(request)
        if requested_cache is True and prompt_caching is False:
            raise InputError("request contains a cache directive but prompt_caching was declared off")
        details = dict(provider=provider, model=model, region=region, invocation_logging=invocation_logging,
                       prompt_caching=True if requested_cache else prompt_caching, evidence_level="configuration",
                       sources=list(sources), operation=operation,
                       components=sorted(name for name in ("messages", "system", "tools", "body", "toolConfig", "tool_choice")
                                         if name in request), **self._fingerprint(request))
        self._attempt(store, key, source, "sent", **details)
        result = call(**request)
        self._record(store, key, source, "sent", stage="returned", **details)
        return result

    def bedrock(self, client, request: Mapping[str, Any], *, store: str, key: str, source: str,
                invocation_logging: bool | None = None, prompt_caching: bool | None = None,
                sources=(), operation: str = "converse"):
        """Call a boto3 Bedrock Runtime client. Resolve the destination region from the client."""
        if operation not in {"converse", "invoke_model", "converse_stream", "invoke_model_with_response_stream"}:
            raise InputError("unsupported Bedrock operation")
        model = identifier(request.get("modelId"), "modelId")
        region = getattr(getattr(client, "meta", None), "region_name", None)
        return self._provider(getattr(client, operation), request, provider="bedrock", model=model, region=region,
                              store=store, key=key, source=source, invocation_logging=invocation_logging,
                              prompt_caching=prompt_caching, sources=sources, operation=operation)

    def anthropic(self, client, request: Mapping[str, Any], *, store: str, key: str, source: str,
                  invocation_logging: bool | None = None, prompt_caching: bool | None = None, sources=()):
        """Call the Anthropic Messages API with the supplied request and record its configured exposure."""
        model = identifier(request.get("model"), "model")
        return self._provider(client.messages.create, request, provider="anthropic", model=model, region=None,
                              store=store, key=key, source=source, invocation_logging=invocation_logging,
                              prompt_caching=prompt_caching, sources=sources, operation="messages.create")

    def langchain_callback(self, *, store: str, source: str, provider: str, model: str,
                           region: str | None = None, invocation_logging: bool | None = None,
                           prompt_caching: bool | None = None, sources=()):
        """Attach at an application chat/LLM boundary when the framework owns the SDK call."""
        from langchain_core.callbacks import BaseCallbackHandler
        if provider not in {"anthropic", "bedrock"}:
            raise InputError("expected the Bedrock or Anthropic provider")
        details = dict(provider=provider, model=model, region=region, invocation_logging=invocation_logging,
                       prompt_caching=prompt_caching, evidence_level="configuration", sources=list(sources),
                       operation="langchain.start")
        hooks = self

        class ModelCopyCallback(BaseCallbackHandler):
            raise_error = True
            run_inline = True

            def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
                payload = [[msg.model_dump(mode="json") for msg in group] for group in messages]
                hooks._attempt(store, str(run_id), source, "sent", **details, components=["messages"],
                               **hooks._fingerprint(payload))

            def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
                hooks._attempt(store, str(run_id), source, "sent", **details, components=["prompts"],
                               **hooks._fingerprint(prompts))

        return ModelCopyCallback()

    async def call_tool(self, client, name: str, arguments: dict, *, store: str, key: str, source: str, **kwargs):
        """Wrap one MCP ClientSession.call_tool, recording the input and returned result separately."""
        identifier(name, "tool name")
        self._attempt(store, key + "/input", source, "sent", tool=name, direction="input",
                      **self._fingerprint(arguments))
        self._attempt(store, key + "/result", source, "derived", tool=name, direction="result")
        result = await client.call_tool(name, arguments, **kwargs)
        self._record(store, key + "/result", source, "derived", tool=name, direction="result", stage="returned",
                     **self._fingerprint(result))
        return result
