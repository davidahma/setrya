# Test a Celery and LangChain answer endpoint

Use the framework starter to verify two signed-in sessions before connecting Celery or Redis. Create the test in your application checkout.

```sh
python -m pip install 'setrya[pytest]'
setrya init endpoint --framework celery-langchain
python -m pytest -q -s -p setrya.pytest_plugin --setrya-reads-only tests/test_setrya_endpoint.py
```

Connect `open_test_app(run_id)` to a disposable fixture containing two already signed-in users with private access. Give that object an `answer(caller, question)` method that makes the HTTP request and returns its observed status, authenticated caller, route, served question fingerprint and answer text. The [response mapping](endpoint.md#connect-the-worker) shows an example. Configure the expected fixture values in `CASE`. This stage needs neither a worker nor any Redis connection. When both expected answers return, the read check confirms each caller independently and keeps the race verdict at `NOT TESTED`. The printed replay command works after teardown.

### Connect the full race

Install the framework extras and use the same generated test without the reads-only flag.

```sh
python -m pip install 'setrya[celery-langchain]'
python -m pytest -q -s tests/test_setrya_endpoint.py
```

The fixture supplies these hooks as you advance. The controller allocates a Redis worker checkpoint when it reaches task dispatch.

| Fixture member | What the application supplies |
| --- | --- |
| `seed()` | Give both synthetic callers access and warm both private cache entries. |
| `answer(caller, question)` | Extend the two HTTP observations with source and cache metadata. A hit identifies the task that wrote the entry. |
| `test_redis`, `fill_answer` | The disposable Redis client and the bound Celery task for the stale fill. The generated adapter constructs `fill_answer.s(CASE.revoked, CASE.question)`. |
| `revoke()` | Remove the selected caller’s access, evict their entry and return the app’s `Publication` acknowledgment. |

A fixture with Redis serving telemetry can supply `send_answer(caller, question, request_id)` in place of `answer()`. It sends the question through the authenticated HTTP endpoint, includes the opaque request ID in a header and returns the actual HTTP status and answer text. The serving path records the same request as shown below. This integration also works for reads-only startup when Redis is available. To customize the task signature or queue options, the existing `CeleryLangChainEndpoint(client, run_id, case, task=..., seed=..., revoke=..., send=...)` interface remains available in the generated file's `application()` context.

The controller and worker need network access to the same disposable Redis database for the full race. The broker may use a different service. A worker can run in Docker while pytest runs on the developer’s laptop.

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
