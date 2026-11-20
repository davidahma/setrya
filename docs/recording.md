# Check runtime updates in an existing pytest test

Use the opt-in `setrya_record` fixture when your test can pause a worker, publish an update, and query the application as its intended caller. Your test owns setup and its native assertions. The fixture records each observation, saves the report, and makes the verdict part of the pytest result. The [complete working example](../setrya/examples/recorded_search/existing_test.py) uses a loopback search app and an independent indexing worker.

## Enable the fixture

From your application’s test environment, install the release and enable the pytest plugin for your test.

```sh
python -m pip install 'setrya[pytest]'
python -m pytest -p setrya.pytest_plugin tests/test_refund.py
```

To enable the fixture for the whole suite, add `pytest_plugins = ["setrya.pytest_plugin"]` to the root `conftest.py`.

## Record the observations as they happen

Start with a [test-owned drill](../setrya/examples/recorded-search/drill.toml) that declares the expected passages, caller, route, search depth, and time limits. The fixture takes that file and yields a recorder. This example assumes your existing `app` fixture exposes worker controls and returns typed observations.

```python
def test_refund_update(app, setrya_record):
    with setrya_record("tests/refund-search.toml") as recorder:
        app.seed(recorder.run_id)
        recorder.seeded()
        recorder.search("refund-window", app.search)
        recorder.search("delivery", app.search)

        attempt = app.start_old()
        recorder.started(attempt)
        held = app.wait_for_checkpoint(attempt)
        recorder.held(held)
        if app.receipt(attempt) is not None:
            return
        recorder.published(app.publish_revision())
        recorder.search("refund-window", app.search)

        if app.checkpoint(attempt) != held or app.receipt(attempt) is not None:
            return
        app.release(attempt)
        recorder.released(attempt)
        recorder.receipt(app.wait_for_terminal_receipt(attempt))
        recorder.search("refund-window", app.search)
        recorder.search("delivery", app.search)
```

On normal exit, a PASS lets the test continue. FAIL and NOT TESTED fail pytest and identify the report to replay. If a native assertion raises inside the context, pytest keeps the original failure and the recorder saves the observations collected so far.

Each recording gets its own default file under pytest’s temporary directory. The default test identity derives from a hash of the pytest node ID, including its parameter ID. For release reviews, use a stable identity, a fresh output path per run, and the full Git commit ID of the checkout supplying the app. For example, pass `test_id="refund-worker", report="candidate.json", revision=application_commit` to the fixture. The [working test](../setrya/examples/recorded_search/existing_test.py) reads the revision from its application checkout.

For Python harnesses that decide how to enforce the result themselves, `from setrya.recorder import Recorder` provides the same recording calls. Construct it with `test_id` and `report`, use it as a context manager, then inspect `recorder.result.verdict`. The adapter-driven pytest fixture `setrya_check` runs a complete drill when Setrya owns the worker controls. [Adapter guide](adapters.md)

The helpers return Setrya’s typed `Checkpoint`, `Publication`, `Receipt`, and `SearchResult`. The search callback receives a `Probe` with the configured question, route, caller, and depth. Map the actual response into a `SearchResult` and return receipts for the same `recorder.run_id`. [Evidence contracts](adapters.md#adapter-methods)

## Run the two-revision example

```sh
python3 -m pip install '.[pytest]'
python3 -m setrya.examples.recorded_search.verify --output /tmp/setrya-recorded-review
```

The example creates a disposable Git repository with a real change to the worker's revision guard. It checks out both commits independently, runs the same pytest file and drill against each, then closes both apps before comparing. The output contains `base.json`, `candidate.json`, and `review.md`. A third run on the candidate, saved as `opaque.json`, serves the expected refund policy from a cache while an old write commits. The native search assertions pass, the fixture fails pytest, and the saved report replays as NOT TESTED. GitHub Actions runs this example and attaches the reports even when its verification fails. [CI job](../.github/workflows/ci.yml)

## Use it in a pull request

From an application checkout, install its test dependencies and Setrya into a test environment outside the checkout. Keep both Git refs available locally. Select one existing test that uses `setrya_record` or `setrya_check`, then pass its native command after `--`.

```sh
setrya run-pr --repo . --base origin/main --candidate HEAD \
  --drill tests/refund-search.toml --output refund-review -- \
  python -m pytest -p setrya.pytest_plugin -q tests/test_refund.py::test_refund_update
```

Setrya resolves each ref to a full commit and runs the same command once in each of two disposable local clones. The caller's uncommitted files stay in the original checkout. Write the selected test so it imports the application from its current clone, including any worker subprocess. Use equivalent dependencies and disposable data in both lanes. If the integration test is new on the PR, a trusted test file at an absolute path can drive both checkouts. The bundled example does this. A passing verdict refers to the app exercised by that harness.

Inside the selected pytest test, `with setrya_record(test_id="refund-worker") as recorder:` uses the pinned drill, report destination and commit. A test that already passes the same drill can keep that argument. The `setrya_check()` fixture also accepts the lane defaults. Keep a stable test or pytest node ID between revisions. Multiple fixture recordings in one command make the handoff ambiguous and produce an error. For a non-pytest harness, read `SETRYA_REVIEW_DRILL`, `SETRYA_REVIEW_REPORT`, and `SETRYA_REVIEW_REVISION` from the environment and pass them to the recorder; produce one report at the supplied path. The live SDK binds the run to the complete selected drill, including adapter options. For a harness that exits with `result.verdict.status.exit_code`, pass `--exit-contract setrya` before `--`. This interprets exit 3 with matching evidence as NOT TESTED. The default `pytest` contract treats exit 3 as an internal pytest error. The saved review command preserves the selected contract.

`refund-review/review.md` names the decisive probe, phase and observation. From the directory containing that file, use the commands in the summary to replay either lane or recompute the decision without the app. The directory includes `base.json` and `candidate.json` for the lanes that produced valid reports. `run-pr.json` records the commit and native exit for each lane, the digest of each published report, and the SHA-256 of the selected drill. A command that failed to start or hit its time limit has a null exit code. Test output goes to the job console; the handoff omits command arguments, raw logs and the drill text. Treat your CI log as application data.

The command returns **0** for a verified fix or a passing comparable pair, **1** for an observed candidate failure, **2** for input or execution errors, and **3** for inconclusive or incompatible evidence. A failing base still runs the candidate. On a timeout, missing report, or mismatched test input, the summary identifies the affected lane and keeps usable evidence from the other. Upload the output directory even when the command fails. Start each invocation with a new output path. Use `--timeout SECONDS` to change the per-test limit from 900 seconds.

Run reviewed test commands in a disposable CI job with read-only repository permissions and the credentials needed for its test fixtures. Both checkouts execute application code with the job's privileges. If your CI already produces the two reports and captures the native exits, keep that setup and run the reviewer directly.

```sh
setrya review base.json candidate.json \
  --base-commit "$BASE_SHA" --candidate-commit "$CANDIDATE_SHA" \
  --base-exit "$BASE_TEST_EXIT" --candidate-exit "$CANDIDATE_TEST_EXIT" \
  --summary review.md
```

The [Dify case study](dify-regression.md) shows how the handoff differs for a native regression that observes the index directly.

The same review command accepts two scenario directories for runs with retries, duplicate messages or multiple workers. [Compare scheduled runs in CI](scenarios.md#compare-the-base-and-candidate-in-ci)
