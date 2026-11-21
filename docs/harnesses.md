# Agent and trace harnesses

A ticket can disappear from a search result while its text survives in an agent session, tool result or trace. Configure the fixture to search the *workspace or conversation* that could contain the text. Searching by the deleted subject's tag would miss the copy that lost its tag.

Install only the integrations your fixture uses.

| Fixture | Install | Direct reads | Configuration or observed calls | Unknown unless the fixture checks it |
| --- | --- | --- | --- | --- |
| LangGraph | `setrya[erasure]`; add `setrya[erasure-postgres]` for PostgreSQL | Full-saver checkpoint payloads, pending writes, memory values in declared namespaces | The application's graph, memory and model-call hooks record observed boundaries | Other threads and namespaces, semantic matches in embeddings, encrypted payloads |
| OpenAI Agents / Responses | `setrya[agent-openai]` | Message rows in a declared SQLiteSession database | Native Runner session pointer and synchronous Responses SDK request. Logging and cache settings are declared or unknown | Hosted Conversations, provider storage and tracing, other session backends |
| Claude Agent SDK | `setrya[agent-claude]` | JSON records in one named local session transcript | Native pre-tool and post-tool callbacks record tool input and result pointers | Child-agent transcripts, tool-owned stores, provider retention and caches |
| Langfuse | `setrya[traces-langfuse]` | Observation inputs and outputs returned by Observations v2 in the declared project and session | The span hook records its trace and observation IDs before export | Masked, truncated, sampled or unexported fields, metadata, media and backups |

The integrations use local Python objects. The base package imports without an agent SDK. For provider calls, the journal stores opaque pointers, model and configured settings. Supplying a private fingerprint key also records a keyed digest. Setrya does not copy prompts into the journal.

## Wire a scanner into a case

For example, an app writes answer text to a pgvector table with `workspace`, `id` and `body` columns. Supply a dedicated observer connection with `autocommit=True`. Register `"passages"` in the case's `scan_stores` and use the case's disposable workspace for both reads.

```python
from setrya.erasure.canary import PostgresCanaryScanner

passages = PostgresCanaryScanner(
    observer, table="passages", key="id", content="body", scope_column="workspace"
)

def scan(self, subject, store):
    if store == "passages":
        return passages.find(scope=self.fixture_workspace, marker=self.synthetic_marker)
    raise KeyError(store)
```

Seed the marker into a copy *before* running the case. Each scanner returns the exact matching keys. The fixture's `inspect` reads those keys and its erasure operation owns any mutation. A missing baseline, unsupported encoding, incomplete read or exceeding the scan budget leaves that boundary unverified. Finish writes, drain exports and keep a retained control readable during the test. An exact-marker miss says nothing about a paraphrase.

| Store | Scanner | Read boundary |
| --- | --- | --- |
| SQLite | `SQLiteCanaryScanner` | Literal text in one explicitly named table column and scope |
| PostgreSQL / pgvector | `PostgresCanaryScanner` | All rows in one scoped text column, including rows with no embedding. Install `setrya[pgvector]`. |
| Elasticsearch | `ElasticsearchCanaryScanner(client, index, scope_field="workspace", content_field="body")` | One concrete index, exact keyword scope. Refresh and traverse a point-in-time view of the declared source field. Install `setrya[erasure-elasticsearch]`. |
| Redis | `RedisCanaryScanner(client, prefix="answers")` | String values under `answers:<scope>:<key>` on a standalone Redis 7+ primary. Use a dedicated DB of at most 4,096 keys and 16 MiB of scoped text. An incompatible key type stops the scan. Install `setrya[redis]`. |
| SDK answer caches | `AnswerCacheCanaryScanner(cache, tenant="fixture")` | Published answers in one retrieval scope of a local or Redis SDK cache |
| LangGraph checkpoints | `SQLiteCheckpointCanaryScanner(connection)`, `PostgresCheckpointCanaryScanner(connection)` | All full-saver checkpoint versions and pending writes for one thread, including orphan PostgreSQL blobs. Searches plain marker bytes in supported serialized data. |
| LangGraph memory | `LangGraphMemoryCanaryScanner(store, namespace=("teams",))`, `PostgresMemoryCanaryScanner(connection, namespace=("teams",))` | In-memory items or persisted JSON values in the declared namespace subtree. Direct PostgreSQL reads include expired rows. |

The LangGraph scanners live in `setrya.erasure.langgraph_canary`; the other store scanners live in `setrya.erasure.canary`. PostgreSQL scanners use a synchronous observer connection even when the app writes through an async saver.

## Attach an agent or trace hook

Import these helpers from `setrya.integrations.agent_harnesses`. Use a `CopyHooks` instance bound to the fixture's journal and subject.

- **OpenAI Agents.** Call `await run_openai_agent(hooks, agent, inputs, session=session, store="chat", source="ticket")` with the app's native, file-backed `SQLiteSession`. Point `OpenAISessionCanaryScanner(connection)` at that database and scan its session ID. The result contains message row IDs. For code that calls Responses directly, `openai_response(hooks, client, request, store="provider", key=request_id, source="ticket")` calls the synchronous SDK and records the attempted and returned request. A returned request is configuration-level provider evidence. [Agents SDK sessions](https://openai.github.io/openai-agents-python/sessions/)
- **Claude Agent SDK.** Attach `claude_tool_hooks(hooks, session=session_id, store="tools", source="ticket")` to `ClaudeAgentOptions(hooks=...)`. If the app already has hooks, append the matchers to the corresponding event lists. Call `ClaudeTranscriptCanaryScanner(project_transcripts).find(scope=session_id, marker=marker)` for each declared transcript. A missing file is an unread boundary. [Claude Agent SDK hooks](https://platform.claude.com/docs/en/agent-sdk/hooks)
- **Langfuse.** Open `with langfuse_observation(hooks, client, session=session_id, store="traces", source="ticket", name="summary", input=payload) as span:` around the operation and call `span.update(output=result)`. Flush the exporter and wait for ingestion. Give `LangfuseCanaryScanner(client, project_id=project_id, control="trace-id/observation-id")` the ID of a separate retained observation in the same session. The reader follows every cursor and checks the control. An empty or incorrectly scoped response cannot clear the trace copy. [Langfuse Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)

CI runs the native OpenAI runner with a deterministic local model, the Responses SDK with an in-process HTTP transport, and the native Claude SDK with a local control-protocol transport. It writes and deletes real traces on a disposable Langfuse server, then rereads the session. Run `python scripts/verify-agent-harnesses.py` from a checkout with Docker and the three agent/trace extras plus `setrya[pytest]`. This test uses synthetic data and needs no model-provider credentials. [Pinned Langfuse server](https://github.com/langfuse/langfuse/tree/fe82bdc24d3bbaac919b20d8fe8708920a9a81a8)
