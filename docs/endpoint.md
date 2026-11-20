# Test your own answer endpoint

Use this starter when you can run a disposable copy of your app, sign in as two synthetic users, and pause a cache-fill task after it reads a private answer. It exercises your app’s normal answer route. To try the bundled LangChain, pgvector, and Celery app first, follow the [live example](answer-cache.md).

For a Celery worker and a LangChain retriever, use the [framework starter](endpoint-framework.md) to supply the worker hooks through Redis.

## Run the starter

From your application checkout, run these commands in a Python 3.11+ virtual environment.

```sh
python -m pip install 'setrya[pytest]'
setrya init endpoint
python -m pytest -q -s tests/test_setrya_endpoint.py
```

The first run fails pytest with `NOT TESTED`, identifies `seed` as the first missing hook, and prints a replay command. Edit `tests/test_setrya_endpoint.py` and rerun the same test as you connect each hook. `-s` shows the evidence path even on a passing run. To generate the starter at a different path, pass `--output tests/test_policy_access.py` to `setrya init endpoint`. The destination must be a new file.

## Connect the test

Set `CASE` to your route label, two distinct caller IDs, one question, and the exact private and public answers you seeded. For this drill, the private answer cites the private source alone or both sources in the order the app returned them. The public answer cites only the public source. Use deterministic synthetic answers and keep both callers signed in for the entire test.

Implement the methods in `Application` using your existing test client, queue controls, and fixtures. For autocomplete, import `EndpointResponse` and `WorkerCapture` from `setrya.endpoint` as return types for `answer()` and `captured()`. The framework passes a fresh `run_id` to the constructor. In `__exit__`, close clients and release or cancel any worker left waiting by an incomplete run.

| Hook | What your app supplies |
| --- | --- |
| `seed()` | Grant both users access, store the synthetic policies, and warm both private answers. |
| `answer(caller, question)` | Call the authenticated endpoint and return the observed HTTP status, route, caller, question fingerprint, answer text, ordered source IDs, cache hit, and cache writer. See the example below. |
| `start_old()` | Dispatch a real cache fill for the user about to lose access. Return the task’s unique attempt ID. |
| `checkpoint()` | Return `None` until the selected task acknowledges that it read the old revision and is paused before the write. Then return `Checkpoint(run_id, attempt, document, revision, name)` from that acknowledgment. |
| `captured()` | Read the worker’s captured input. Return a dictionary with `run_id`, `attempt`, `caller`, `revision`, `question`, `answer`, and ordered `sources`. |
| `revoke()` | Revoke the selected user and evict their entry. Return `Publication(run_id, document, revision)` from the completed change. |
| `release()` | Resume that same task only if it is still held at the checkpoint. |
| `receipt()` | Return `None` while the selected task is pending. Once it finishes, return `Receipt(run_id, attempt, document, revision, outcome, generation, commit)`. The revision belongs to the task; the outcome is `"rejected"` or `"committed"`. A commit needs its index generation and numeric write position. |

Import `Checkpoint`, `Publication`, and `Receipt` from `setrya.adapter`. The case defaults to document `policy`, revisions `granted` and `revoked`, and checkpoint `after-retrieval`. Set those fields explicitly when your app uses other names. [Celery checkpoint example](answer-cache.md#use-the-hooks-in-an-existing-application)

For example, suppose your test client signs in as a fixture user and the endpoint includes serving metadata in its JSON response. Read `caller` from the identity the server authenticated. The question fingerprint is the SHA-256 hex digest of the UTF-8 question text the endpoint served. Copying the method arguments into the evidence would hide requests routed to the wrong user or question.

```python
def answer(self, caller, question):
    response = self.clients[caller].post("/answers", json={"question": question})
    body = response.json()
    return {
        "http_status": response.status_code,
        "route": body.get("route_id"),
        "caller": body.get("authenticated_principal"),
        "query_sha256": body.get("served_question_sha256"),
        "answer": body.get("text"),
        "sources": body.get("source_ids"),
        "cache_hit": body.get("cache_hit"),
        "cache_writer": body.get("writer_task_id"),
        "generation": body.get("served_index_generation"),
        "commit": body.get("served_write_position"),
    }
```

For a hit, the cache entry identifies the task that wrote it. A miss has no cache writer. If a write commits, the read must identify the generation and position of the snapshot that served it. After a rejected write, these two fields can be `None`. [Store visibility signals](store-signals.md)

For a complete working example, see the [disposable approvals app](../tests/fixtures/endpoint_app.py) and the [test that connects it to the generated starter](../tests/test_endpoint_onboarding.py).

## Read the result

While the worker is held, the revoked user must receive the public answer and the retained colleague must receive the private one. After the worker finishes, the guarded endpoint still serves the public answer to the revoked user and a private cache hit to the colleague. This starter checks HTTP 200 responses with those answer bodies. A sign-in error, an unavailable route, or a blanket cache flush cannot establish PASS.

Pytest exits 0 for a passing test and 1 when the drill fails or lacks evidence. To hand off the evidence, copy the entire `endpoint-run` directory printed by pytest. A reviewer can run `setrya replay PATH` after the app shuts down. Replay exits 0 for PASS, 1 for FAIL, 2 for invalid input, and 3 for NOT TESTED. Choose shareable caller IDs and source markers when you prepare a fixture.
