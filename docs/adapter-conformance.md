# Qualify a store or queue adapter

An adapter is ready to try on a release when a reviewer can distinguish a stale response, a verified fix, and a clean response that lacks evidence. Setrya ships the same Python assertions its own pgvector, Elasticsearch and Celery fixtures run in CI. They use the core install. Install the dependencies for whichever adapter you exercise.

The maintained store examples are SQLite FTS5, pgvector and Elasticsearch. The answer application shows LangChain retrieval and a Celery worker. Pinecone, Qdrant, Turbopuffer, Redis, LlamaIndex, RQ and Temporal are candidates for future adapters. A new integration can use these assertions to demonstrate its own supported behavior.

## Store adapters

Give the suite three configurations of one disposable adapter. Use identical routes, callers, queries, expected passages and time limits. Vary the application or the visibility signal through adapter options. This example runs from a checkout with the [pgvector fixture](pgvector.md) running and its DSN set.

```python
from dataclasses import replace
from pathlib import Path

from setrya.conformance import assert_store_contract
from setrya.drill import Drill

guarded = Drill.load(Path("setrya/examples/pgvector-search/drill.toml"))
reports = assert_store_contract(
    broken=replace(guarded, options={"mode": "broken"}),
    guarded=guarded,
    opaque=replace(guarded, options={"mode": "opaque"}),
    output="store-evidence",
)
```

| Configuration | Evidence required |
| --- | --- |
| Broken | The first decisive forbidden response occurs after release. A subsequent read by the declared caller confirms a forbidden passage after the worker commits. |
| Guarded | Replay awards PASS at the selected evidence tier. The default is `write-confirmed`. |
| Opaque | The worker commits. The final caller reads are clean, but the adapter cannot establish visibility of that write. Replay returns NOT TESTED. |

Use a fresh output directory. Review the saved journals with `setrya replay store-evidence/opaque.json`. The suite also supports `expected_tier="observed-after-refresh"` for an adapter that acknowledges a refresh after commit. The Elasticsearch tests exercise both tiers and the missing-signal control. [How visibility is established](store-signals.md) · [Live Elasticsearch tests](../tests/test_elasticsearch_store.py)

## Queue adapters

Call `assert_queue_contract` from the test that owns the worker, broker and teardown. Bind the five callbacks to real dispatch, worker checkpoint, application change, release and terminal result. The checkpoint and receipt readers must return promptly and return `None` while waiting. The dispatched task must still be held after the application acknowledges the change.

```python
from setrya.adapter import Publication
from setrya.conformance import assert_queue_contract

receipt = assert_queue_contract(
    dispatch=app.start_old, checkpoint=app.checkpoint, publish=app.revoke,
    release=app.release, receipt=app.receipt,
    change=Publication(run_id, "policy", "revoked"),
    old_revision="granted", checkpoint_name="after-retrieval",
    outcome="rejected", timeout=30,
)
```

Here `app` is the running answer-cache fixture. The suite matches the worker's captured revision and task ID to its final receipt. Use a released worker's actual outcome, either `committed` or `rejected`. A timeout or identity mismatch fails the test. Keep cleanup in the fixture so a failed assertion can also terminate a held worker. [Celery example and native conformance test](../tests/test_adapter_conformance.py) · [Integrate a checkpoint](answer-cache.md#use-the-hooks-in-an-existing-application)
