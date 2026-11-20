# Setrya

Setrya tests whether a background job or a reopened AI chat can bring deleted data back. Run a deletion against a test subject in your LangGraph app, read the result as two users, and give a reviewer a receipt they can replay. Setrya also tests access revocation, tenant offboarding, and search updates.

[![Tests](https://github.com/davidahma/setrya/actions/workflows/ci.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/ci.yml)
[![Offline qualification](https://github.com/davidahma/setrya/actions/workflows/qualification.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/qualification.yml)

[Erase a test subject](#erase-a-test-subject) · [Replay an access change](#replay-an-access-change) · [Review a tenant deletion](#review-a-tenant-deletion) · [Test your own endpoint](#test-your-own-endpoint)

## Erase a test subject

Alice’s chart was deleted. A worker still holds an old snapshot. When two care-team members reopen the saved handoff, can either of them see Alice again? Run the fictional example on a laptop with Python 3.11+.

```sh
python -m pip install 'setrya[erasure]'
setrya demo erasure --output alice
setrya replay alice/broken
```

| Run | What the two users see | Receipt |
| --- | --- | --- |
| Broken worker | Alice returns in the reopened chat. | `FAIL` |
| Guarded worker | Alice stays gone. Both users can still read Bob’s chart. | `PASS` |
| Provider copy | The app is clear. The provider’s storage cannot be inspected. | `NOT TESTED` · one unknown copy |

Run `setrya init erasure` to connect your own fixture, worker and read path. The receipt lists the copies observed in the journal or discovered in declared stores. A reviewer can check the signature and replay it without your app. [Starter](https://github.com/davidahma/setrya/blob/v0.16.0/docs/erasure-starter.md) · [Evidence and signing](https://github.com/davidahma/setrya/blob/v0.16.0/docs/erasure.md) · [Download the signed example](https://github.com/davidahma/setrya/releases/download/v0.16.0/subject-erasure.zip)

<a id="test-a-late-answer-cache-fill"></a>
<a id="test-access-revocation-with-two-users"></a>

## Replay an access change

Two colleagues ask an internal AI assistant about the staff refund policy. You revoke one person’s access and clear their cached answer. They ask again and get the public policy. Then a background job that read the private policy before revocation finishes.

In a Python 3.11+ environment, install the SDK and CLI and replay the recorded responses. The demo uses two synthetic accounts and runs offline after installation.

```sh
python -m pip install setrya
setrya demo access
```

| What happened | Person who lost access | Colleague who kept access |
| --- | --- | --- |
| The worker was still paused | Public policy · 14 days | Staff policy · 45 days |
| The worker wrote its old answer · **FAIL** | Staff policy · 45 days | Staff policy · 45 days |
| The guarded worker rejected the write · **PASS** | Public policy · 14 days | Staff policy · 45 days |

A third recording returns `NOT TESTED` because the endpoint evidence doesn’t identify who made the request. A pass covers the callers, question, and timing the test exercised. [See what a reviewer gets in a pull request](#review-the-result-in-a-pull-request).

<a id="test-tenant-offboarding"></a>

## Review a tenant deletion

A customer leaves. Your app deletes their ticket and summary, but a worker still has the ticket it was summarizing. Run the live example against a local SQLite app with fictional tenants. Choose a fresh output directory.

```sh
setrya offboarding demo --output offboarding-run
```

The broken worker puts the summary back where a support agent can read it. The fixed worker discards the stale write. The test also checks that the agent can still read the other tenant’s summary.

The demo also creates a receipt for a separate staging deletion request. It checks the source, stored summary, and support endpoint after deletion and again after the worker finishes. All six checks pass. A provider reports deletion, but a backup is still on a retention schedule. You can see both results in `offboarding-run/receipt/receipt.json`.

```text
Technical verdict PASS
Review status OPEN_ITEMS
```

A reviewer can verify the receipt against the original request, job evidence, and attachments offline. [Offboarding guide](https://github.com/davidahma/setrya/blob/v0.15.0/docs/offboarding.md) · [Download the example receipt and evidence](https://github.com/davidahma/setrya/releases/download/v0.15.0/offboarding-example.zip)

## Test your own endpoint

Start in a disposable checkout of your application with two signed-in synthetic users.

```sh
python -m pip install 'setrya[pytest]'
setrya init endpoint
```

Connect the generated `answer(caller, question)` hook to your HTTP route and set the expected answers. Run the first check with both sessions before wiring up the worker.

```sh
python -m pytest -q -s -p setrya.pytest_plugin --setrya-reads-only tests/test_setrya_endpoint.py
```

This first check exits 1 with `NOT TESTED`. Once you connect the worker and the access change, run the test without `--setrya-reads-only`. [Endpoint guide](https://github.com/davidahma/setrya/blob/v0.15.0/docs/endpoint.md) · [Celery and LangChain starter](https://github.com/davidahma/setrya/blob/v0.15.0/docs/endpoint-framework.md)

## Review the result in a pull request

Run the connected test on two Git revisions. Setrya reports `FIX VERIFIED` when the old worker exposed the private answer on the base commit and the same case passes for both users on the candidate. A teammate can replay either result after the app shuts down.

```sh
setrya run-pr --endpoint --repo . --base origin/main --candidate HEAD \
  --output endpoint-review -- python -m pytest -q tests/test_setrya_endpoint.py
```

In the approvals example, the delayed worker restored a private answer for the revoked reviewer. After the fix, the reviewer got public guidance and the owner still got the private answer. Open `review.md` in the download and run its **Review again** command to reproduce `FIX VERIFIED` offline. [Read the FIX VERIFIED report](https://github.com/davidahma/setrya/blob/main/docs/examples/endpoint-fix/review.md) · [Download both runs and the two fixture commits](https://raw.githubusercontent.com/davidahma/setrya/main/docs/examples/endpoint-fix/endpoint-fix.zip)

Routine PRs can pass on both commits. [Download the two-pass example](https://github.com/davidahma/setrya/releases/download/v0.15.0/endpoint-review.zip) · [GitHub Actions recipe](https://github.com/davidahma/setrya/blob/main/docs/pr-endpoint.md)

<a id="run-the-demo"></a>
<a id="run-the-refund-policy-demo"></a>
<a id="use-the-python-sdk-in-your-tests"></a>
<a id="more-drills-and-integrations"></a>

## Search, caching, and the Python SDK

- **Search freshness.** Run `setrya demo` to see an old indexing job bring back a 30-day refund policy after its 14-day replacement was already searchable. The live demo uses SQLite FTS5. [Test other worker schedules](https://github.com/davidahma/setrya/blob/v0.15.0/docs/scenarios.md) · [Try pgvector](https://github.com/davidahma/setrya/blob/v0.15.0/docs/pgvector.md)
- **Answer caching.** Track which source revisions went into an answer and invalidate affected entries in memory or Redis. [Local cache](https://github.com/davidahma/setrya/blob/v0.15.0/docs/local-cache.md) · [Redis cache](https://github.com/davidahma/setrya/blob/v0.15.0/docs/redis-cache.md)
- **Existing tests.** Record a worker’s actions and search results with the `setrya_record` pytest fixture. A coverage map can flag changed routes and jobs that lack matching evidence. [Record a test](https://github.com/davidahma/setrya/blob/v0.15.0/docs/recording.md) · [Check route coverage](https://github.com/davidahma/setrya/blob/v0.15.0/docs/coverage.md)

[Contributing](https://github.com/davidahma/setrya/blob/v0.15.0/CONTRIBUTING.md) · [Security policy](https://github.com/davidahma/setrya/blob/v0.15.0/SECURITY.md) · [Apache 2.0 license](https://github.com/davidahma/setrya/blob/v0.15.0/LICENSE)
