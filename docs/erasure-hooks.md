# Record a test subject's copies

Bind the **person whose data the application handles** to each operation. The signed-in caller can be someone else. For a note about Alice viewed by a clinician, use Alice's subject reference and the clinician's normal authorization. Keep the drill in a disposable tenant.

## Carry the subject from request to worker

```python
import os
from setrya.erasure import CopyJournal, Subject
from setrya.erasure.hooks import CopyHooks

subject = Subject.derive(
    bytes.fromhex(os.environ["SETRYA_SUBJECT_KEY"]),
    tenant=tenant_id, subject_id=test_person_id, classification="clinical notes",
)
journal = CopyJournal("copies.sqlite")
with subject.activate():
    summarize.apply_async(args=[opaque_chart_key], headers=subject.headers())
```

Create a persistent secret with at least 32 random bytes. Keep it with the application. Rotation changes the references used to find existing copies. The reference is a tenant-scoped HMAC. It is pseudonymous and remains linkable inside the tenant. Use opaque system, copy and source IDs. Put the customer's handling label in `classification` as a plain string. The hooks never need a name, chart text or email in these fields.

In a Celery bound task, restore the subject with `Subject.from_metadata(self.request.headers)`. Bind it with `with subject.activate():` to make `Subject.current()` available in nested functions and async tasks. Accept these fields only from trusted application code. An inbound client header does not establish identity or authorization.

## Observe the writes and reads

```python
hooks = CopyHooks(journal, subject)
chunks = tag_documents(split_docs, document="chart-17", revision="3", subject=subject)
hooks.add_documents(vectors, chunks, ids=chunk_ids, store="vectors")
docs, search = hooks.retrieve(
    scoped_retriever, question, store="vectors", route="ask", scope="care-team"
)
answer = hooks.invoke_graph(
    graph, {"messages": messages}, thread="chat-17", store="checkpoints", source="chart-17"
)
hooks.put_memory(memory, "snapshot-17", snapshot, store="memory", source="chart-17")
```

Import `tag_documents` from `setrya.integrations.langchain`. For another vector library, `passage_metadata(document, revision, chunk, subject=subject)` supplies the same fields. `add_documents` records the requested physical IDs before the write and checks the IDs returned by the store. `retrieve` records subject-tagged results and the actual rank evidence returned by the retriever. Supply `key=lambda doc: ...` if `Document.id` differs from the adapter's physical key.

`invoke_graph` adds the reference and classification to runnable metadata and selects a dedicated thread, `setrya:<ref>:chat-17`. The journal records the thread as one deletion unit covering its saved checkpoints. `put_memory` writes a flat key in the namespace `("setrya", subject.ref)`. Use `subject.langgraph_config("chat-17", config)` when reopening a thread. The subject-specific thread and memory layout lets the deletion adapter leave another person's chat intact. Give shared multi-person conversations a dedicated storage policy or a custom adapter before including them in a drill.

For the SDK's local or Redis answer cache, use a subject-exclusive retrieval scope and keep the actual caller's access scope. `SubjectAnswerCacheAdapter` enumerates stored answers and cancels outstanding fills on deletion.

```python
from setrya.erasure.cache import SubjectAnswerCacheAdapter
from setrya.local_cache import CacheKey, LocalAnswerCache

cache = LocalAnswerCache(require_current_sources=True)
cache.set_document_revision("clinic", "chart-17", "3")  # at the ingestion boundary
key = CacheKey("clinic", "care-team", subject.ref, "handoff")
hit = hooks.fill_cache(cache, key, retrieve, generate, store="answers", source="chart-17",
                       metadata=lambda doc: doc.metadata)
adapters["answers"] = SubjectAnswerCacheAdapter(cache, tenant="clinic")
```

The hooked fill requires every generation input to carry the same subject and classification. Reserve that retrieval scope for hooked calls. Use `set_document_revision(tenant, document, None)` when the document is deleted. Register a change on the authoritative write path in commit order. Publication and the version check run under the same cache lock or Redis script. A generated answer that cites a stale or unregistered revision returns `None` in strict mode. An unrelated document change leaves the entry available. A coalesced fill runs generation once for concurrent misses. The [local cache](local-cache.md) and [shared Redis cache](redis-cache.md) guides cover process boundaries and invalidation.

## Model calls and MCP tools

Wrap the client that receives the provider request. The hooks pass through its return value. Supply immutable source or chunk IDs for the data actually included in that request.

```python
# Optional separate private key enables fingerprints of the supplied SDK arguments.
hooks = CopyHooks(journal, subject, fingerprint_key=payload_audit_key)
result = hooks.bedrock(
    runtime_client, converse_request, store="bedrock", key=request_id, source="chart-17",
    sources=chunk_markers, invocation_logging=logging_enabled,
)
message = hooks.anthropic(
    anthropic_client, messages_create_request, store="anthropic", key=request_id,
    source="chart-17", sources=chunk_markers, prompt_caching=caching_enabled,
)
tool_result = await hooks.call_tool(
    mcp_session, "summarize_chart", arguments, store="summary-tool", key=tool_call_id, source="chart-17"
)
```

Bedrock defaults to `converse`. Pass `operation="invoke_model"` for an InvokeModel request. The record includes the requested model and the Bedrock client's configured region. A cross-region inference profile can route processing elsewhere. Anthropic Messages requests leave the region unknown. For a text-only Bedrock call, read the control-plane client in the same account and region with `config = bedrock_client.get_model_invocation_logging_configuration()`. Pass `config.get("loggingConfig", {}).get("textDataDeliveryEnabled")` as `invocation_logging`. An absent field or missing permission leaves this setting unknown. The journal excludes S3 destinations, log group names and role ARNs. [AWS logging configuration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GetModelInvocationLoggingConfiguration.html)

Set `invocation_logging` from the deployment's current configuration. Set `prompt_caching` from an observed setting or let the hook detect a cache directive in the request. Omitted values stay unknown. Provider records include the supplied source references, payload components and, when requested, a subject-scoped keyed fingerprint of the SDK arguments. This allows the application to compare a candidate payload without placing the payload in the evidence file.

All provider observations are **configuration evidence**. A network timeout leaves an attempt in the journal. Returning a provider response confirms only that the call returned. Storage, internal copies, retention expiry and deletion at the provider require separate evidence. Allocate a distinct opaque key for every provider invocation or tool call. MCP records input and result as two locations. A disconnect leaves the result uncertain. Register the external tool's system in the inventory.

When LangChain owns the SDK call, pass `hooks.langchain_callback(store="bedrock", source="chart-17", provider="bedrock", model=model_id, region=region, sources=chunk_markers)` in `config={"callbacks": [callback]}`. This observes the LangChain model inputs before the provider adapter serializes them. Use the client wrapper when the exact SDK arguments matter.

## Local journal and tracing

`journal.history(subject)` returns each event, including attempted writes and returned calls. `journal.copies(subject)` returns the latest observation at each store and key. Direct adapter scans can also discover locations that had no journal entry. A read-only provider, a missing hook, a payload copied to an untagged row, and an unlisted system each limit what a receipt can establish. Declare the actual storage and observation boundaries in the inventory. For application-owned writes, call `journal.record(subject, store="charts", key=opaque_key, source=source_key, action="stored")` at that boundary.

To propagate the reference and label over an approved tracing link, inject `subject.otel_context()` with the OpenTelemetry W3C baggage propagator. Restore it with `Subject.from_baggage(extracted_context)`. Keep baggage inside the intended trust boundary. By default the journal writes only to the local SQLite file. `CopyJournal(path, export_trace_events=True)` also emits a `setrya.copy.v1` event on the current span. It contains the two subject fields, store, copy key, source, action, observed time and allowlisted details. Enable it only for a collector approved for this classification. A dropped or sampled span leaves the durable local journal intact.

## Collect journals across worker processes

Give each expected producer a stable name, its own SQLite file, and the same fresh run ID and tenant-keyed test subject. `WorkerJournal` accepts the instrumentation used with `CopyJournal`. Keep the file on durable storage throughout the drill. Its event identity combines the run, producer and a sequence assigned by SQLite. A restarted worker must reopen its original file.

```python
from setrya.erasure.journal_merge import CollectedJournal, WorkerJournal

worker = WorkerJournal("worker.sqlite", run_id=run_id, producer="summarizer")
worker.record(subject, store="memory", key="incident-41", source="finding-a", action="derived")
# Seal after this worker has finished every operation for the subject, including late writes.
digest = worker.seal(subject, "summarizer.json")
```

Transfer each sealed JSON packet over your authenticated CI artifact channel. Bind the producer name to the channel that supplied the packet. A SHA-256 received separately from that producer can also be checked. The packet contains pseudonymous identifiers and source metadata. Keep private runs in authorized storage. Once sealed, SQLite rejects additional writes for the subject. A producer that saw no copies can explicitly seal an empty packet.

```python
collector = CollectedJournal("review.sqlite", run_id=run_id)
collector.ingest("summarizer.json", producer="summarizer", expected_sha256=digest)
collector.ingest("api.json", producer="api")
coverage = collector.coverage(subject, run_id=run_id, expected_producers=("api", "summarizer"))
```

Pass `journal=collector`, `run_id=run_id`, and `expected_producers=("api", "summarizer")` to `run_shared` or `arun_shared`. For a live drill, use the optional `application.collect_journals(subject)` hook to flush, seal, and import packets **after the final endpoint and storage reads**. The checker can report a missing producer even when every direct read looked clear. Redelivering an identical packet is safe. Conflicting event identities or differing final packets from the same producer raise an error. The receiver trusts the authorized exporter to have instrumented its writes.

A `WorkerJournal` constructed with `export_trace_events=True` includes the durable event identity in its OpenTelemetry `setrya.copy.v1` events. Pass a trusted event’s attributes to `collector.ingest_trace(attributes, producer="summarizer")` to record an observed copy. Only a sealed producer packet completes its coverage slot. A trace from an undeclared producer opens a coverage gap even if it records a previously observed key. Historical trace events lacking a run, producer or sequence cannot complete a distributed drill.
