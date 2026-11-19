# Can an old task put a private answer back in cache?

Two users can read a private refund policy. A Celery task retrieves its passages through LangChain and pauses before filling one user's answer cache. While the task is held, the app revokes that user's access and evicts their entry. Setrya checks the answer, releases the task, waits for its outcome, then sends the same question to the same HTTP endpoint as both users.

| Configuration | Revoked user after the task finishes | Retained colleague | Verdict |
| --- | --- | --- | --- |
| Broken | A cache hit returns the private answer that the task captured before revocation. | The private answer is still available. | FAIL |
| Guarded | The endpoint returns the public answer. | A cache hit still returns the private answer. | PASS |

The answer is deterministic so the drill can check the response body as well as the ordered source IDs. It uses synthetic text, actual PostgreSQL vector ranking, a separate Celery worker process and an authenticated loopback HTTP endpoint. You can run it without model credentials.

## Run the worker

From a checkout or unpacked source release, create a virtual environment and start the disposable database. These commands use Python 3.11+ and Docker Compose.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[answer-cache]'
docker compose -f setrya/examples/pgvector-search/compose.yml up -d --wait
docker compose -f setrya/examples/pgvector-search/compose.yml exec -T postgres \
  psql -U setrya -d setrya_fixture -c 'CREATE EXTENSION IF NOT EXISTS vector'
export SETRYA_PGVECTOR_DSN='postgresql://setrya:setrya-local@127.0.0.1:55432/setrya_fixture'
```

Run each mode in a fresh output directory. The broken check deliberately exits 1.

```sh
setrya answer-cache --mode broken --output broken
setrya answer-cache --mode guarded --output guarded
```

The fixture creates a schema for each run. The worker uses a private local filesystem queue, and the HTTP app resolves a bearer credential to one of the two fixture users. When finished, stop the disposable service:

```sh
docker compose -f setrya/examples/pgvector-search/compose.yml down
```

## Review the evidence after teardown

With core Setrya installed, replay the complete directory. You can also unpack the [release evidence](https://github.com/davidahma/setrya/releases/download/v0.11.0/late-cache-fill.zip) and run the same commands.

```sh
setrya replay broken
setrya replay guarded
```

Look for the revoked caller's answer while the task is held and after it finishes. A private answer after a previously clean read exposes the regression. The colleague's private **cache hit** verifies that the retained path continued to serve cached answers. A cache flush that makes every request miss cannot establish this result.

The worker records its actual input before pausing. Replay matches its task ID, caller, query fingerprint and captured sources to the evidence. In the broken run, the cache entry identifies that same task as its writer, and the endpoint observes its write in the same PostgreSQL snapshot used to serve the answer. A missing capture or write witness leaves the corresponding claim unverified.

The bundle pairs the normal Setrya worker journal (`run.json`) with the answer observations (`answer-cache.json`). Use the directory for the answer verdict. The journal alone covers the worker and passage observations. A PASS applies to the question, both callers and execution schedule in the bundle.

## Use the hooks in an existing application

**At ingestion**, tag chunks after splitting one immutable document revision. Existing metadata, content and LangChain document IDs survive. Persist the returned metadata with each vector.

```python
from setrya.integrations.langchain import tag_documents

chunks = tag_documents(split_documents, document="refund-policy", revision="B")
store.add_documents(chunks)
```

Chunk numbers start at zero on each call. For ingestion in multiple batches use a stable unique key from your source, for example `chunk_id=lambda doc, ordinal: doc.metadata["section_id"]`. Give different revisions distinct revision labels.

**On retrieval**, wrap the retriever that serves the authenticated caller. The wrapper invokes it once and returns the retrieved documents plus their ordered Setrya observation. If your app authorizes, truncates or reranks results downstream, call `observe_documents` on that final list instead. Untagged or inconsistent chunks retain their rank as unknown passages.

```python
from setrya.integrations.langchain import retrieve_with_evidence

documents, observed = retrieve_with_evidence(
    caller_retriever, question, route="answer", scope=authenticated_scope
)
```

This observes retrieval. A cache hit can bypass the retriever entirely. To test caching, record the actual answer endpoint's response, including hit or miss, and the source revisions stored with the answer.

**In the Celery task**, pause after reading the inputs and before publishing the result. The controller creates a fresh `CeleryCheckpoint` for a chosen task ID and dispatches the bound task with that ID. The worker opens the same test-owned file. Here `self` is the bound task and `captured_revision` comes from the data it actually read.

```python
from setrya.integrations.celery import CeleryCheckpoint

# In the test, before dispatch.
gate = CeleryCheckpoint.create(gate_path, run_id=run_id, task_id=task_id)
fill_answer.apply_async(task_id=task_id)

# Inside the bound task, after retrieval.
CeleryCheckpoint(gate_path).hold(
    self, run_id=run_id, document="refund-policy",
    revision=captured_revision, name="after-retrieval"
)
```

The test waits for `gate.checkpoint()` to acknowledge capture, applies the change, queries the caller, then calls `gate.release()` and waits for the application's write result. The worker and controller need the same local filesystem and clock. Configure the run ID and checkpoint path in the test worker before it starts. The task receives its ID from Celery. [Complete example worker](../setrya/examples/answer_cache/worker.py) · [Celery bound tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html#bound-tasks) · [LangChain retrievers](https://reference.langchain.com/python/langchain-core/retrievers/BaseRetriever)

## What the example cache checks

The guarded app stores the caller, question, source chunk IDs, and the access and catalog versions with each answer. It checks the versions before writing a late fill. On a cache hit it also checks that the caller can still read every cited chunk. Revocation evicts the affected caller’s entry. In this single-workspace fixture, a chunk insertion, update, or deletion advances the whole catalog version, so a change can invalidate unrelated entries. A physically present obsolete row produces a miss. The app owns those checks. The reusable Setrya hooks capture what it did. The sample uses a deterministic answer derived from its selected passages. Assessing the quality of generated prose or guaranteeing freshness for every query needs additional tests.
