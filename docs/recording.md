# Add replay evidence to an existing pytest test

Use `Recorder` when a test already starts your app, controls an indexing worker, and exercises search. It measures the real calls your test makes and saves the same replayable observations as `setrya check`. The test still owns the worker, fixtures, and assertions. The [example test](../examples/recorded_search/existing_test.py) uses ordinary pytest with a loopback search app and an independent indexing worker.

## Record the observations as they happen

Use a [test-owned drill](../examples/recorded-search/drill.toml) containing the rules and probes. The `[adapter]` table is optional for recording. `setrya check` still requires an adapter.

```python
from setrya.recorder import Recorder

recorder = Recorder(
    "tests/refund-search.toml",
    test_id="refund-worker",
    report="refund-report.json",
    revision=application_commit,
)
with recorder:
    app.seed()
    recorder.seeded()
    recorder.search("refund-window", search)
    recorder.search("delivery", search)

    attempt = app.start_old()
    recorder.started(attempt)
    recorder.held(wait_for_checkpoint(attempt))
    recorder.published(app.publish_revision())
    recorder.search("refund-window", search)

    app.release(attempt)
    recorder.released(attempt)
    recorder.receipt(wait_for_terminal_receipt(attempt))
    recorder.search("refund-window", search)
    recorder.search("delivery", search)

assert recorder.result.verdict.exit_code == 0, recorder.result.verdict.detail
```

In this example, your helpers return Setrya's typed `Checkpoint`, `Publication`, `Receipt`, and `SearchResult`. The search callback receives a `Probe` with the exact question, caller, route, and result depth. Map the actual response into a `SearchResult` so the original test can also assert on the results. Record the observations from the same run and use `recorder.run_id` in the app when it generates receipts. You can supply an existing run ID if your test already has one. Use a stable test ID for the same test and a fresh report path for each CI execution. [Evidence contracts](adapters.md#adapter-methods)

Each recorder captures one serial sequence. The checker requires the original revision at baseline, the held attempt, publication, the intended caller seeing the replacement before release, and search evidence after the old write resolves. It measures the hold deadline against the recorded start and checkpoint. An assertion inside the recording preserves an established FAIL. A test exception before a conclusive failure produces an execution error and saves any observations already made. The report stores the exception type while leaving the original exception visible to pytest.

## Run the two-revision example

```sh
python3 -m pip install '.[pytest]'
python3 -m examples.recorded_search.verify --output /tmp/setrya-recorded-review
```

The example creates a disposable Git repository with a real change to the worker's revision guard. It checks out both commits independently, runs the same pytest file and drill against each, then closes both apps before comparing. The output contains `base.json`, `candidate.json`, and `review.md`. GitHub Actions runs this example and attaches the reports even when its verification fails. [CI job](../.github/workflows/ci.yml)

## Use it in a pull request

Check out the base and candidate into separate directories using their full commit IDs. Run the same test files and configuration from a shared harness, pointing the app loader at each checkout. Derive `application_commit` inside the test from `git rev-parse HEAD` in the checkout the app actually loads. Record the native test command's exit code even when the base fails. Run the comparison after both app processes close.

```sh
setrya review base.json candidate.json \
  --base-commit "$BASE_SHA" --candidate-commit "$CANDIDATE_SHA" \
  --base-exit "$BASE_TEST_EXIT" --candidate-exit "$CANDIDATE_TEST_EXIT" \
  --summary review.md
```

Use exit **0** for a verified fix or a passing comparable pair, **1** when the candidate records a failure, **2** for input or test execution errors, and **3** for inconclusive or incompatible evidence. A recorded PASS requires a successful native test. A recorded FAIL requires the test command to report failure. A missing observation or commit mismatch stays unresolved. Upload both JSON reports and the Markdown summary after success or failure. Each report can be replayed independently.

Setrya checks that reported commit IDs match the workflow's expected IDs, and that the reports use the same test identity and inputs. The workflow supplies checkout provenance. Run the harness in a disposable environment with a lightweight, read-only pull-request job on GitHub Actions. Keep the repositories, fixture inputs, corpus, and test command equivalent across the pair.

The [Dify case study](dify-regression.md) shows how the handoff differs for a native regression that observes the index directly.
