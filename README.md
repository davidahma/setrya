# Setrya

Two colleagues ask an internal AI assistant about a private policy. You revoke one user’s access and clear their cached answer. The assistant now gives that user only public information. Then a background job that started before the revocation puts the private answer back in the cache.

Setrya is an open-source Python SDK and CLI for testing that race before you ship. It records what the worker did and what your app served each user. A teammate can replay the result after the app shuts down.

[![Tests](https://github.com/davidahma/setrya/actions/workflows/ci.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/ci.yml)
[![Offline qualification](https://github.com/davidahma/setrya/actions/workflows/qualification.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/qualification.yml)

[Test your own endpoint](https://github.com/davidahma/setrya/blob/v0.15.0/docs/endpoint.md) · [Record a pytest test](https://github.com/davidahma/setrya/blob/v0.15.0/docs/recording.md) · [Test tenant offboarding](https://github.com/davidahma/setrya/blob/v0.15.0/docs/offboarding.md)

<a id="test-a-late-answer-cache-fill"></a>

## Test access revocation with two users

Install Setrya in a Python 3.11+ environment and replay a recorded access change. The package includes the evidence from a real Celery worker and HTTP endpoint using two synthetic accounts. You can run this offline after installation.

```sh
python -m pip install setrya
setrya demo access
```

| What the endpoint served | User who lost access | Colleague who kept access |
| --- | --- | --- |
| While the task was paused | Public answer | Private answer from cache |
| After the broken worker wrote · **FAIL** | Private answer from the resumed task | Private answer from cache |
| After the guarded worker rejected the write · **PASS** | Public answer | Private answer from cache |

A third replay shows `NOT TESTED` when the serving path supplies too little evidence to identify the caller. To run a live drill against a disposable app, start with your own endpoint below or use the [pgvector example](https://github.com/davidahma/setrya/blob/v0.15.0/docs/answer-cache.md#run-the-worker).

### Test your own endpoint

From your application checkout, generate the pytest test and connect its `answer(caller, question)` hook to your real HTTP route. Start with two signed-in synthetic users who can both read the private answer.

```sh
python -m pip install 'setrya[pytest]'
setrya init endpoint
python -m pytest -q -s -p setrya.pytest_plugin --setrya-reads-only tests/test_setrya_endpoint.py
```

That first run shows which caller, route, question and answer the app confirmed for each request. The race stays `NOT TESTED` until you connect the worker and run the test without `--setrya-reads-only`. If your app uses Celery and LangChain, install `setrya[celery-langchain]` and generate the test with `setrya init endpoint --framework celery-langchain`. [Connect an endpoint](https://github.com/davidahma/setrya/blob/v0.15.0/docs/endpoint.md) · [Connect a Celery worker](https://github.com/davidahma/setrya/blob/v0.15.0/docs/endpoint-framework.md)

<a id="run-the-demo"></a>

## Run the refund-policy demo

Your team shortens the refund window from 30 days to 14. The support agent searches and gets the new policy. Then a worker resumes with the copy it read yesterday. The offline demo runs that race against a local SQLite FTS5 index.

```sh
python -m pip install setrya
setrya demo
```

| Verdict | What the support agent sees | What the evidence shows |
| --- | --- | --- |
| FAIL | 30 days → 14 days → 30 days | The old worker committed. The next search returned the old policy at rank 1. |
| PASS | 30 days → 14 days | The worker rejected its stale write. Final searches found the 14-day policy and the unrelated delivery policy. |
| NOT TESTED | 30 days → 14 days | The old worker committed and search still returned 14 days. The response didn’t establish that the index had seen the write. |

## Use the Python SDK in your tests

A drill names the caller, route, query, and passages that must appear or disappear. Run the bundled refund drill from Python with the core package.

```python
from importlib.resources import as_file, files
from setrya.runner import run

drill = files("setrya.examples").joinpath("support-search", "drill.toml")
with as_file(drill) as path:
    result = run(path)
print(result.verdict.status.label)  # PASS
```

For your own application, an adapter supplies the worker checkpoint, write receipt, and search results. If pytest already drives your worker, install `setrya[pytest]` and use the `setrya_record` fixture to save those observations alongside your assertions. A PASS covers the callers, queries, and schedule you tested. [Adapter guide](https://github.com/davidahma/setrya/blob/v0.15.0/docs/adapters.md) · [Record an existing pytest test](https://github.com/davidahma/setrya/blob/v0.15.0/docs/recording.md) · [Verdict rules](https://github.com/davidahma/setrya/blob/v0.15.0/docs/evidence.md)

## Review the result in a pull request

Run the endpoint test from an application checkout that has both Git revisions.

```sh
setrya run-pr --endpoint --repo . --base origin/main --candidate HEAD \
  --output endpoint-review -- python -m pytest -q tests/test_setrya_endpoint.py
```

The PR check shows what the revoked user and the retained colleague received on each revision, what the delayed worker did, and how to replay the result. A candidate earns `FIX VERIFIED` when its evidence accounts for the read that failed on base. Keep the artifact on failing checks so the reviewer can inspect either run after teardown. For search and indexing drills, pass `--drill` with the trusted drill file. [Endpoint CI recipe](https://github.com/davidahma/setrya/blob/v0.15.0/docs/pr-endpoint.md) · [Review an indexing change](https://github.com/davidahma/setrya/blob/v0.15.0/docs/recording.md#use-it-in-a-pull-request)

## Test tenant offboarding

A customer leaves, but a worker still holds a copy of their support summary. Run `setrya offboarding demo --output offboarding-run` to test whether the summary reappears and whether a retained tenant’s summary stays readable. The CI gate reports what the app served. A separate deletion receipt tracks one request and can still list open items, such as a backup awaiting expiry. [Offboarding guide](https://github.com/davidahma/setrya/blob/v0.15.0/docs/offboarding.md) · [Axiom migration](https://github.com/davidahma/setrya/blob/v0.15.0/docs/offboarding.md#migrate-from-axiom)

## More drills and integrations

- [Selective cache invalidation](https://github.com/davidahma/setrya/blob/v0.15.0/docs/answer-cache.md#invalidate-a-refund-answer-and-keep-a-delivery-hit) checks that a refund update leaves an unrelated delivery answer cached. [Cache answers by source in memory](https://github.com/davidahma/setrya/blob/v0.15.0/docs/local-cache.md) · [Share answers across processes with Redis](https://github.com/davidahma/setrya/blob/v0.15.0/docs/redis-cache.md) · [Answer dependencies](https://github.com/davidahma/setrya/blob/v0.15.0/docs/answer-dependencies.md) · [Cache metrics](https://github.com/davidahma/setrya/blob/v0.15.0/docs/cache-metrics.md)
- [Worker schedules](https://github.com/davidahma/setrya/blob/v0.15.0/docs/scenarios.md) cover retries, duplicate deliveries, and reordered writes. [OpenDesk’s two-user revocation trial](https://github.com/davidahma/setrya/blob/v0.15.0/docs/opendesk-revocation.md) follows a real summary job across a team membership change.
- [Store evidence](https://github.com/davidahma/setrya/blob/v0.15.0/docs/store-signals.md) explains how a search can establish that it saw a particular write. [Route coverage](https://github.com/davidahma/setrya/blob/v0.15.0/docs/coverage.md) identifies changed read paths that lack matching evidence.

[Contributing](https://github.com/davidahma/setrya/blob/v0.15.0/CONTRIBUTING.md) · [Security policy](https://github.com/davidahma/setrya/blob/v0.15.0/SECURITY.md) · [Apache 2.0 license](https://github.com/davidahma/setrya/blob/v0.15.0/LICENSE)
