# Test a Celery and LangChain answer endpoint

The framework starter controls a bound Celery task through Redis. The test can run on a developer’s laptop while the worker runs in Docker. Both need network access to the same disposable Redis database. The broker may use a different service. The generated test runs the same two-user checks and produces the same offline evidence as the [generic endpoint starter](endpoint.md).

```sh
python -m pip install 'setrya[celery-langchain]'
setrya init endpoint --framework celery-langchain
python -m pytest -q -s tests/test_setrya_endpoint.py
```

On the first run, pytest reports `NOT TESTED` and identifies `application()`. Connect `open_test_app(run_id)` to your disposable fixture. Supply a Redis client, the app’s normal bound cache-fill task signature, and three callbacks.

| Callback | What the application does |
| --- | --- |
| `seed()` | Give both synthetic callers access and warm both private cache entries. |
| `revoke()` | Remove the selected caller’s access, evict their entry and return the app’s `Publication` acknowledgment. |
| `send_answer(caller, question, request_id)` | Send the question as that caller through the real HTTP endpoint. Pass the opaque test request ID in a header. Return the observed HTTP status and answer text. |

Set `CASE` in the generated file to the two callers, route, question, expected answers, and source markers used by the fixture. Use the existing [LangChain tagging helper](answer-cache.md#use-the-hooks-in-an-existing-application) to attach document, revision and chunk identity at ingestion. Select the same task Celery normally dispatches with your application’s arguments and queue options. Setrya reserves a task ID and sends the run ID in the `setrya_test_run` message header.

To inspect a completed run before wiring your app, unpack the [release evidence](https://github.com/davidahma/setrya/releases/download/v0.14.0/celery-langchain-endpoint.zip). From that directory run `setrya replay broken`, `setrya replay guarded` or `setrya replay unobserved`. They show a stale answer, a rejected late fill and a request missing serving evidence, respectively.

## Hold the actual task at its write

Instrument a dedicated test worker. In this example, `documents` are the final LangChain documents supplied to generation, `answer` is the text the worker prepared, and `write` comes from the real cache’s write decision. Pass Redis connection details through the worker’s trusted test configuration.

```python
from setrya.adapter import Receipt
from setrya.integrations.celery_langchain import RedisCeleryCheckpoint

# Inside a bound Celery task, after retrieval and generation.
gate = RedisCeleryCheckpoint.for_task(test_redis, self)
if gate is not None:
    gate.hold(document=document_id, revision=observed_access_revision,
              name="after-retrieval", caller=effective_caller, question=served_question,
              answer=answer, documents=documents)

write = cache.write_captured_answer(capture)
if gate is not None:
    gate.finish(Receipt(gate.run_id, self.request.id, write.document,
                        write.captured_revision, write.outcome, write.generation, write.commit))
```

The test observes the captured caller, question, answer and ordered source IDs before it changes access. Redis stores fingerprints for the synthetic question and answer. A Redis key expiry limits the hold. If the hold expires or the test closes a held gate, the waiting task exits before the write. Redis serializes the release and expiry, so containers and clients do not compare their host clocks. An ordinary delivery without the test header receives no gate.

## Keep evidence fields out of the public response

In the test deployment’s serving path, call `record_served` after cache lookup or generation, using the actual authenticated caller, executed question, returned answer and dependencies. The public response can still contain just the answer text. For a hit, read the sources and writer ID from the entry that supplied that answer.

```python
from setrya.integrations.celery_langchain import record_served

ticket = request.headers.get("X-Setrya-Request")
if ticket:
    record_served(test_redis, ticket, route=route_id, caller=authenticated_user.id,
                  question=executed_question, answer=response_text, sources=served_source_ids,
                  cache_hit=was_hit, cache_writer=writer_id,
                  generation=read_generation, commit=read_position)
return {"text": response_text}
```

The SDK reserves a fresh ID before each HTTP call, checks that the observed response text matches the serving record, then consumes the record once. A missing, duplicate, expired, or mismatched record leaves the read unverified. A committed late write also needs the generation and position observed by the actual read. A rejected write does not require a read position.

Use Redis 6.2 or newer with expiry enabled and a test-only namespace accessible to your controller, worker and endpoint. The helpers use temporary `setrya:celery:*` and `setrya:served:*` keys. Ordinary production workers and clients should have no access to the test database. If the endpoint cannot observe its cache source or the worker cannot pause before publication, the result remains `NOT TESTED`. [Working HTTP endpoint and worker](../tests/fixtures/celery_endpoint_app.py) · [Cross-process test](../tests/test_celery_langchain.py)
