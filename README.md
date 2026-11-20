# Setrya

Two colleagues ask an internal AI assistant about a private policy. You revoke one user’s access and clear their cached answer. The assistant now gives that user only public information. Then a background job that started before the revocation puts the private answer back in the cache.

Setrya is an open-source Python SDK and CLI for testing that race before you ship. It records what the worker did and what your app served each user. A teammate can replay the result after the app shuts down.

[![Tests](https://github.com/davidahma/setrya/actions/workflows/ci.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/ci.yml)
[![Offline qualification](https://github.com/davidahma/setrya/actions/workflows/qualification.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/qualification.yml)

[Python SDK](#use-the-python-sdk-in-your-tests) · [Pull request review](#review-an-indexing-change-in-a-pull-request) · [Tenant offboarding](#test-tenant-offboarding)

<a id="test-a-late-answer-cache-fill"></a>

## Test access revocation with two users

A Celery task reads synthetic passages through LangChain and pgvector. Setrya pauses it before it writes to the cache. The app revokes access and evicts the user’s entry. Setrya asks the same question through the app’s authenticated HTTP endpoint as each user, first while the task is paused and again after it finishes.

| What the endpoint served | User who lost access | Colleague who kept access |
| --- | --- | --- |
| While the task was paused, in both runs | Public answer only | Private answer from cache |
| After the broken worker wrote its answer · **FAIL** | Private answer from the resumed worker’s cache entry | Private answer from cache |
| After the guarded worker rejected the write · **PASS** | Public answer only | Private answer from cache |

Replay both published runs in a Python 3.11+ virtual environment. The bundle records the task’s write outcome and whether each response contained private material. Replay needs no running services. [Setrya on PyPI](https://pypi.org/project/setrya/)

```sh
python -m pip install setrya
curl -fL -o late-cache-fill.zip https://github.com/davidahma/setrya/releases/download/v0.12.1/late-cache-fill.zip
unzip -q late-cache-fill.zip -d late-cache-fill
setrya replay late-cache-fill/broken   # FAIL, exit 1
setrya replay late-cache-fill/guarded  # PASS, exit 0
```

To drive the live HTTP endpoint yourself, use the `answer-cache` extra and a disposable pgvector database. The fixture builds answers without a model API key. [Run the live drill](docs/answer-cache.md#run-the-worker) · [Use the hooks in your app](docs/answer-cache.md#use-the-hooks-in-an-existing-application)

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

For your own application, an adapter supplies the worker checkpoint, write receipt, and search results. If pytest already drives your worker, install `setrya[pytest]` and use the `setrya_record` fixture to save those observations alongside your assertions. A PASS covers the callers, queries, and schedule you tested. [Adapter guide](docs/adapters.md) · [Record an existing pytest test](docs/recording.md) · [Verdict rules](docs/evidence.md)

## Review an indexing change in a pull request

Run your test on the base and candidate commits. From your application checkout, with both refs available and the pytest extra installed, use your own drill path and pytest test ID.

```sh
setrya run-pr --repo . --base origin/main --candidate HEAD \
  --drill tests/refund.toml --output refund-review \
  -- python -m pytest -p setrya.pytest_plugin -q tests/test_index.py::test_refund_update
```

In the refund example, the base returns the forbidden 30-day policy at rank 1. On the candidate, the worker rejects the write and the same caller gets 14 days. The reviewer sees `FIX VERIFIED` with both observations. Keep the output as a CI artifact even when the test fails. [Review the example evidence](https://github.com/davidahma/setrya/releases/download/v0.12.1/recorded-pytest-review.zip) · [CI recipe](docs/recording.md#use-it-in-a-pull-request)

## Test tenant offboarding

A customer leaves, but a worker still holds a copy of their support summary. Run `setrya offboarding demo --output offboarding-run` to test whether the summary reappears and whether a retained tenant’s summary stays readable. The CI gate reports what the app served. A separate deletion receipt tracks one request and can still list open items, such as a backup awaiting expiry. [Offboarding guide](docs/offboarding.md) · [Axiom migration](docs/offboarding.md#migrate-from-axiom)

## More drills and integrations

- [Selective cache invalidation](docs/answer-cache.md#invalidate-a-refund-answer-and-keep-a-delivery-hit) checks that a refund update leaves an unrelated delivery answer cached. [In-memory cache](docs/local-cache.md) · [Answer dependencies](docs/answer-dependencies.md) · [Cache metrics](docs/cache-metrics.md)
- [Worker schedules](docs/scenarios.md) cover retries, duplicate deliveries, and reordered writes. [OpenDesk’s two-user revocation trial](docs/opendesk-revocation.md) follows a real summary job across a team membership change.
- [Store evidence](docs/store-signals.md) explains how a search can establish that it saw a particular write. [Route coverage](docs/coverage.md) identifies changed read paths that lack matching evidence.

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Apache 2.0 license](LICENSE)
