# Setrya

Setrya is an open-source Python SDK and CLI for testing what an application can retrieve after a document changes, a tenant leaves, or a caller loses access.

Your team shortens the refund window from 30 days to 14. The support agent searches and gets the new policy. Then a worker resumes with the copy it read yesterday. In a Setrya regression test, you release that worker on purpose and search again as the same agent. If the 30-day policy comes back, the report identifies the passage and its rank. A teammate can replay the failure after the app shuts down.

A delayed job can also bring back a departed customer’s summary. Setrya can check for that in CI and, for a specific deletion request, produce a receipt showing what the application returned and what still needs review.

[![Tests](https://github.com/davidahma/setrya/actions/workflows/ci.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/ci.yml)
[![Offline qualification](https://github.com/davidahma/setrya/actions/workflows/qualification.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/qualification.yml)

[Run the search demo](#run-the-demo) · [Test a cached answer](#test-a-late-answer-cache-fill) · [Test tenant offboarding](#test-tenant-offboarding) · [Use the Python SDK](#use-the-python-sdk-in-your-tests) · [Review a fix in CI](#review-an-indexing-change-in-a-pull-request)

## Run the demo

Install Setrya in a Python 3.11+ virtual environment with SQLite FTS5 support. The demo runs offline against a local index of synthetic policies. [Setrya on PyPI](https://pypi.org/project/setrya/)

```sh
pip install setrya
setrya demo
```

| Verdict | What the support agent retrieves | What happened |
| --- | --- | --- |
| FAIL | 30 days → 14 days → 30 days | The old worker committed. The next search returned the old policy at rank 1. |
| PASS | 30 days → 14 days | The worker rejected its stale write. Final searches found the 14-day policy and the unrelated delivery policy. |
| NOT TESTED | 30 days → 14 days | The old worker committed. Search still returned 14 days. The response didn’t identify a snapshot that included the old write. |

## Test a late answer-cache fill

A Celery task retrieves a private policy and pauses before writing the answer to cache. One user loses access. The application evicts that user's entry. Can the paused task put the private answer back?

The LangChain and pgvector example runs the task and calls the application's HTTP answer endpoint as the revoked user and a colleague. In the broken configuration, the revoked user receives a private cache hit after the worker resumes. The guarded configuration returns the public answer to that user, and the colleague can still get the private answer from cache.

To try the evidence first, [install core Setrya](#run-the-demo) and unpack the [late-cache-fill bundle](https://github.com/davidahma/setrya/releases/download/v0.12.1/late-cache-fill.zip). From the directory containing `broken/` and `guarded/`, run:

```sh
setrya replay broken
setrya replay guarded
```

The first command exits 1 and the second exits 0. To run the worker yourself, use the `answer-cache` extra and a disposable pgvector database. The ingestion, retrieval, and worker checkpoint helpers can also be used separately in an existing test. [Run the live drill and adapt the hooks](docs/answer-cache.md)

A second drill asks what happens to the rest of the cache. A worker already holds yesterday’s refund answer when a better refund policy enters the index. The guarded app rejects that fill, serves the updated policy, and keeps the same cached delivery answer for the same user. The broken app returns correct delivery text but forces another generation. Run `setrya answer-cache --case selective --mode guarded --output scoped` against the fixture, or download both runs and replay them offline. [Selective invalidation drill](docs/answer-cache.md#invalidate-a-refund-answer-and-keep-a-delivery-hit) · [Evidence bundle](https://github.com/davidahma/setrya/releases/download/v0.12.1/selective-cache.zip)

To try the cache without services, use the single-process Python example. It records the ordered chunks supplied to generation and checks the caller and retrieval scope before accepting a fill. Local counters distinguish measured cache hits, skipped generations and stale answers among checked requests. Store and queue adapters can run the same conformance assertions as the bundled integrations. [In-memory cache](docs/local-cache.md) · [Answer dependency SDK](docs/answer-dependencies.md) · [Metrics and Prometheus](docs/cache-metrics.md) · [Adapter tests](docs/adapter-conformance.md)

## Exercise retries, duplicate deliveries, and access changes

Use a scenario to hold work before a change and choose the order in which deliveries finish. For a permission change, the revoked caller must lose the passage while the authorized caller can still read it. Save the run so a reviewer can replay it after the application stops.

```sh
python3 -m setrya scenario revoke --schedule all --output access-run
python3 -m setrya replay access-run
```

The same commands accept `update` and `delete`. If one worker sees the access change and a later search by a second worker returns the revoked passage, replay names both observations. Run the same scenario on two app commits and give the directories to `setrya review` for a PR decision. [Schedules and adapter contract](docs/scenarios.md) · [Evidence from real stores](docs/store-signals.md)

In the OpenDesk trial, an actual summary job starts before one agent loses team membership and finishes afterward. Both agents stay in the workspace. With a workspace-readable team, both still find the new summary. With a private team, only the retained colleague does. The two deployments run the same OpenDesk binary. [Reproduce the release decision](docs/opendesk-revocation.md) · [Download both caller traces](https://github.com/davidahma/setrya/releases/download/v0.12.1/opendesk-revocation-review.zip)

## Test tenant offboarding

Acme leaves. The support app deletes its ticket and summary. Then a worker finishes writing the summary it read before deletion. Run the installed offboarding example in a new output directory.

```sh
python3 -m setrya offboarding demo --output offboarding-run
```

The **CI gate** checks whether the support agent can still read Acme’s summary and whether it can still read another tenant’s. The broken worker produces FAIL, the guarded worker produces PASS, and a missing baseline produces NOT TESTED.

The **deletion receipt** belongs to one request issued before the job starts. In a separate run, the app passes its technical checks and a provider reports deletion. The receipt still says `OPEN_ITEMS` because the backup operator supplied an expiry schedule. A reviewer can verify that result offline against the original request and exhibits. [Run the gate, issue a request, and replay a receipt](docs/offboarding.md) · [Download the synthetic example](https://github.com/davidahma/setrya/releases/download/v0.12.1/offboarding-example.zip)

Already using Axiom? Setrya can run its SQLite observer and pinned OpenDesk trial, validate historical demo reports, and verify deletion receipts. A reviewer can download the single-file offboarding CLI and replay a case offline. [Axiom command map](docs/offboarding.md#migrate-from-axiom) · [Portable CLI and evidence](https://github.com/davidahma/setrya/releases/tag/v0.12.1)

## Use the Python SDK in your tests

Install the release in your project’s virtual environment. Omit `[pytest]` for the core SDK, which uses only the Python standard library.

```sh
python -m pip install 'setrya[pytest]'
```

A *drill* is a TOML file that names the document revisions, the caller and search route, and the passage IDs each query must return or exclude. The refund drill checks the top three hits for the support agent. A second query checks that the delivery policy stays findable. [Example drill](setrya/examples/recorded-search/drill.toml)

The installed package includes a refund drill you can run from Python.

```python
from importlib.resources import as_file, files
from setrya.runner import run

drill = files("setrya.examples").joinpath("support-search", "drill.toml")
with as_file(drill) as path:
    result = run(path)
print(result.verdict.status.label)  # PASS
```

### Keep your existing pytest test

Keep your setup and assertions. The `setrya_record` fixture records what they observe. Here, your `open_app` fixture starts a disposable app for this run. Its methods wait for the worker, return a checkpoint and write receipt, and search as the support agent. [Working pytest example](setrya/examples/recorded_search/existing_test.py) · [Observation types](docs/adapters.md#adapter-methods)

```python
def test_refund_update(open_app, setrya_record):
    with setrya_record("tests/refund.toml") as rec, open_app(rec.run_id) as app:
        check_refund_update(app, rec)


def check_refund_update(app, rec):
    app.seed()
    rec.seeded()
    rec.search("refund-window", app.search)
    rec.search("delivery", app.search)

    attempt = app.start_old()
    rec.started(attempt)
    rec.held(app.wait_for_checkpoint(attempt))
    rec.published(app.publish())
    fresh = rec.search("refund-window", app.search)
    assert (fresh.route, fresh.scope) == ("support-search", "support-agent")
    assert "refund@B" in fresh.passages

    app.release()
    rec.released(attempt)
    rec.receipt(app.wait_for_receipt(attempt))
    rec.search("refund-window", app.search)
    rec.search("delivery", app.search)
```

Copy the example drill to `tests/refund.toml` and run `python -m pytest -p setrya.pytest_plugin`. This example makes the replacement searchable when publication returns. If your index refreshes asynchronously, record each search while you wait for the new revision, or let Setrya schedule the searches through an adapter.

On exit the fixture saves a JSON report. A FAIL or NOT TESTED verdict fails pytest. If your own assertion raises first, you keep its traceback and the observations up to that point.

### Use Recorder outside pytest

Use `Recorder` around the same test code. Your test decides how to act on the verdict.

```python
from setrya.checker import Status
from setrya.recorder import Recorder


def verify_refund(open_app):
    with Recorder("tests/refund.toml", test_id="refund-worker", report="run.json") as rec:
        with open_app(rec.run_id) as app:
            check_refund_update(app, rec)

    verdict = rec.result.verdict
    if verdict.status is not Status.PASS:
        raise AssertionError(f"{verdict.status.label}: {verdict.detail}")
```

## Connect your own search

If you want Setrya to pause the worker and schedule the searches, give the drill a Python adapter. From a Setrya source checkout, run the bundled adapter against its HTTP endpoint and save the result. The Markdown summary names the tested revision and gives the reviewer a replay command.

```sh
python3 -m setrya check setrya/examples/support-search/drill.toml --report run.json --summary summary.md
python3 -m setrya replay run.json
```

In pytest, the `setrya_check` fixture runs the whole adapter-driven drill and requires a PASS. For your app, the adapter must identify the revision the worker read, acknowledge publication of its replacement, and query the route as the intended caller. If the old write commits, each final search needs evidence that its index snapshot includes that commit. A PASS covers the configured queries, ranks, caller, and time windows. For an AI agent, that tells you which passages its retrieval tool returned. Checking the answer it wrote takes a separate assertion. [Adapter guide](docs/adapters.md) · [Verdict rules](docs/evidence.md)

## Review an indexing change in a pull request

The bundled example runs the same pytest test on two Git commits, one with a broken worker and one with the fix. It captures both test exits and writes the comparison to `review-run/review.md`. With Git and the pytest extra installed, choose a new output directory for each run.

```sh
python -m setrya.examples.recorded_search.verify --output review-run
setrya replay review-run/candidate.json
```

The summary reports `FIX VERIFIED` and shows what changed on the caller’s final search.

```text
Before FAIL · event 11 · refund-window · final · refund@A
  Expected refund@B. Observed forbidden refund@A at rank 1.
After PASS · event 11 · refund-window · final · refund@B
  Index rejected the old write. All final probes found their required passages.
```

The example also saves `opaque.json`, which replays as NOT TESTED even though the test found the 14-day policy. You can download all three reports and the summary from the release. [Sample review bundle](https://github.com/davidahma/setrya/releases/download/v0.12.1/recorded-pytest-review.zip)

To run your own test on both application revisions, install the pytest extra, fetch both refs, and run this from the application checkout:

```sh
setrya run-pr --repo . --base origin/main --candidate HEAD \
  --drill tests/refund.toml --output refund-review \
  -- python -m pytest -p setrya.pytest_plugin -q tests/test_index.py::test_refund_update
```

Setrya checks out each commit in a temporary directory and records the test exit beside its report. Keep the command's exit as a CI gate and upload the output directory even on failure. For a two-worker fix, the comparison can point to a search between the first write and the second worker's release. For route coverage, give `setrya coverage` a repository-owned map and the candidate evidence. A new agent-summary route can then name its owner in the PR even when the existing support-search test passes. [Map routes and jobs](docs/coverage.md) · [Two-worker review bundle](https://github.com/davidahma/setrya/releases/download/v0.12.1/worker-order-review.zip)

In a pipeline that already captures both lanes, give `setrya review` two search reports or two matching scenario directories, the expected commit IDs, and the observed exits. A scenario review lists the schedules, identifies the decisive delivery, and gives the reviewer a replay command. [Review a multi-worker run](docs/scenarios.md#compare-the-base-and-candidate-in-ci) · [Download the two-commit scenario review](https://github.com/davidahma/setrya/releases/download/v0.12.1/scenario-review.zip)

For recorded tests, keep both reports with the PR. Attach the JSON and Markdown even when the test fails. For a downloaded report, use the installed `setrya replay` command. Replay recomputes the verdict without loading the adapter. The report stores passage IDs and question fingerprints and omits raw queries and passage text. Choose identifiers you can share. [CI recipe and review command](docs/recording.md#use-it-in-a-pull-request) · [GitHub Actions example](.github/workflows/ci.yml)

For a single report, `setrya diagnose` explains a missing observation. For a pair without CI commit and exit information, `setrya compare` checks whether the recorded evidence establishes a fix. Checks, driven scenarios and offline gate replay exit 0 for PASS, 1 for FAIL, 2 for an input or execution error, and 3 for NOT TESTED. [Evidence and comparison](docs/evidence.md#compare-two-saved-runs)

## More search regressions

- **A deleted paragraph still appears in search.** The refund document shrinks from five chunks to three. A query for the retired clause catches a stale chunk that the general refund query misses. [Retired-chunk drill](setrya/examples/support-search/retired.toml)
- **A re-upload hides a current passage.** An orphaned pgvector candidate takes a top-three slot. The search returns two current passages and drops the clause about the original payment method. [PostgreSQL/pgvector walkthrough](docs/pgvector.md)

If your suite already catches late writes, compare the plain Python test with Setrya’s saved evidence. The paired benchmark runs both approaches against the same pgvector database and worker hooks. [Plain test](setrya/examples/support_search/baseline.py) · [Paired benchmark](docs/benchmark.md)

To contribute a drill, include a broken case that exposes the regression and a control query that should keep working. [Contributing](CONTRIBUTING.md) · [Offline qualification](docs/qualification.md) · [Security policy](SECURITY.md) · [Apache 2.0 license](LICENSE)
