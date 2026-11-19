# Check runtime updates in an existing pytest test

Use the opt-in `setrya_record` fixture when your test can pause a worker, publish an update, and query the application as its intended caller. Your test owns setup and its native assertions. The fixture records each observation, saves the report, and makes the verdict part of the pytest result. The [complete working example](../examples/recorded_search/existing_test.py) uses a loopback search app and an independent indexing worker.

## Enable the fixture

From your application’s test environment, install the release and enable the pytest plugin for your test.

```sh
python -m pip install 'setrya[pytest] @ https://github.com/davidahma/setrya/releases/download/v0.6.0/setrya-0.6.0.tar.gz'
python -m pytest -p setrya.pytest_plugin tests/test_refund.py
```

To enable the fixture for the whole suite, add `pytest_plugins = ["setrya.pytest_plugin"]` to the root `conftest.py`.

## Record the observations as they happen

Start with a [test-owned drill](../examples/recorded-search/drill.toml) that declares the expected passages, caller, route, search depth, and time limits. The fixture takes that file and yields a recorder. This example assumes your existing `app` fixture exposes worker controls and returns typed observations.

```python
def test_refund_update(app, setrya_record):
    with setrya_record("tests/refund-search.toml") as recorder:
        app.seed(recorder.run_id)
        recorder.seeded()
        recorder.search("refund-window", app.search)
        recorder.search("delivery", app.search)

        attempt = app.start_old()
        recorder.started(attempt)
        recorder.held(app.wait_for_checkpoint(attempt))
        recorder.published(app.publish_revision())
        recorder.search("refund-window", app.search)

        app.release(attempt)
        recorder.released(attempt)
        recorder.receipt(app.wait_for_terminal_receipt(attempt))
        recorder.search("refund-window", app.search)
        recorder.search("delivery", app.search)
```

On normal exit, a PASS lets the test continue. FAIL and NOT TESTED fail pytest and identify the report to replay. If a native assertion raises inside the context, pytest keeps the original failure and the recorder saves the observations collected so far.

Each recording gets its own default file under pytest’s temporary directory. The default test identity derives from a hash of the pytest node ID, including its parameter ID. For release reviews, use a stable identity, a fresh output path per run, and the full Git commit ID of the checkout supplying the app. For example, pass `test_id="refund-worker", report="candidate.json", revision=application_commit` to the fixture. The [working test](../examples/recorded_search/existing_test.py) reads the revision from its application checkout.

For Python harnesses that decide how to enforce the result themselves, `from setrya.recorder import Recorder` provides the same recording calls. Construct it with `test_id` and `report`, use it as a context manager, then inspect `recorder.result.verdict`. The adapter-driven pytest fixture `setrya_check` runs a complete drill when Setrya owns the worker controls. [Adapter guide](adapters.md)

The helpers return Setrya’s typed `Checkpoint`, `Publication`, `Receipt`, and `SearchResult`. The search callback receives a `Probe` with the configured question, route, caller, and depth. Map the actual response into a `SearchResult` and return receipts for the same `recorder.run_id`. [Evidence contracts](adapters.md#adapter-methods)

## Run the two-revision example

```sh
python3 -m pip install '.[pytest]'
python3 -m examples.recorded_search.verify --output /tmp/setrya-recorded-review
```

The example creates a disposable Git repository with a real change to the worker's revision guard. It checks out both commits independently, runs the same pytest file and drill against each, then closes both apps before comparing. The output contains `base.json`, `candidate.json`, and `review.md`. A third run on the candidate, saved as `opaque.json`, serves the expected refund policy from a cache while an old write commits. The native search assertions pass, the fixture fails pytest, and the saved report replays as NOT TESTED. GitHub Actions runs this example and attaches the reports even when its verification fails. [CI job](../.github/workflows/ci.yml)

## Use it in a pull request

Check out the base and candidate into separate directories using their full commit IDs. Run the same test files and configuration from a shared harness, pointing the app loader at each checkout. Derive `application_commit` inside the test from `git rev-parse HEAD` in the checkout the app actually loads. Run the selected test once per checkout in a fresh artifact directory so a skipped test cannot inherit a report from an earlier run. Capture the native test command’s exit code even when the base fails. Run the comparison after both app processes close.

```sh
setrya review base.json candidate.json \
  --base-commit "$BASE_SHA" --candidate-commit "$CANDIDATE_SHA" \
  --base-exit "$BASE_TEST_EXIT" --candidate-exit "$CANDIDATE_TEST_EXIT" \
  --summary review.md
```

Use exit **0** for a verified fix or a passing comparable pair, **1** when the candidate records a failure, **2** for input or test execution errors, and **3** for inconclusive or incompatible evidence. A recorded PASS requires a successful native test. A recorded FAIL requires the test command to report failure. A missing observation or commit mismatch stays unresolved. If a report is missing or unreadable, the command writes an ERROR summary identifying the affected side and the evidence available from the other side. Upload the available JSON reports and the Markdown summary after success or failure. Each report can be replayed independently.

Setrya checks that reported commit IDs match the workflow's expected IDs, and that the reports use the same test identity and inputs. The workflow supplies checkout provenance. Run the harness in a disposable environment with a lightweight, read-only pull-request job on GitHub Actions. Keep the repositories, fixture inputs, corpus, and test command equivalent across the pair.

The [Dify case study](dify-regression.md) shows how the handoff differs for a native regression that observes the index directly.
