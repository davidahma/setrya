# Setrya

**Regression tests for stale search results after document updates.**

[![Tests](https://github.com/davidahma/setrya/actions/workflows/ci.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/ci.yml)
[![Offline qualification](https://github.com/davidahma/setrya/actions/workflows/qualification.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/qualification.yml)
[![Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

A support team changes its refund window from 30 days to 14. Search returns 14 days, so the change looks ready. Then an indexing worker finishes with the copy it read before the update. Search returns 30 days again.

Setrya reproduces that event order against the search path your application uses. It records the observations behind the verdict so a teammate can review and replay the result.

## Run the demo

Use Python 3.11 or later with SQLite FTS5. Run these commands from a clone or the unpacked [release archive](https://github.com/davidahma/setrya/releases).

```sh
git clone https://github.com/davidahma/setrya.git
cd setrya
python3 -m setrya demo
```

The synthetic demo shows three outcomes. It runs locally with the Python standard library and SQLite.

| Verdict | What the drill saw |
| --- | --- |
| **FAIL** | Search returned 14 days. After the old worker resumed, search returned 30 days. |
| **PASS** | The index rejected the old write. Search returned 14 days and the unrelated delivery policy was still findable. |
| **NOT TESTED** | Search returned 14 days. The write committed, and the tested search view supplied no proof it had processed that write. |

Each case prints a short history and a rerun command. Try `python3 -m setrya demo --case opaque` to see the third outcome on its own.

## Check an application and replay the report

The bundled app has its own SQLite source, persistent queue, worker process, index, and loopback HTTP search endpoint.

```sh
python3 -m setrya check examples/support-search/drill.toml --report run.json
python3 -m setrya replay run.json
```

`check` starts the app and exercises its regular search route. `replay` recomputes the verdict from `run.json` after the app exits, even if the drill has changed. The report carries the actual route and caller, the publication receipt, result order, timing, and write evidence. Use opaque IDs in your adapter and drill. Setrya omits question and passage text from its standard reports, terminal output, and Markdown summaries.

`check` and `replay` use exit code **0** for PASS, **1** for FAIL, **3** for NOT TESTED, and **2** for an execution or input error. `demo` and `qualify` succeed when their expected outcomes match, including intentional failures. If an adapter errors after a verified regression, the check keeps the FAIL and records the error alongside it. Reports from 0.1 are readable in 0.2 and replay as NOT TESTED. Rerun those drills to collect current evidence.

## Write a drill for your search path

1. Copy the [HTTP drill](examples/support-search/drill.toml). Define the old and new passage markers, caller scope, result depth, and a separate query for an unaffected control.
2. Map your app's regular retrieval response in a small [Python adapter](examples/support_search/adapter.py). Use a disposable target. Connect a checkpoint after the worker reads the old revision and before it writes. An independent path must be able to publish the new revision while that worker waits.
3. Return a publication receipt for the new revision and identify the actual route and caller on each search. Return a rejection receipt for the held attempt or expose the relevant search snapshot. Check the broken behavior once, then keep the drill alongside the fix.
4. In a pull request, link the commit, CI run, and approved report artifact. Generate the Markdown summary with `check --report run.json --summary summary.md`. It lists the primary probe, the published revision, and the replay command when those observations are available. Give reviewers the drill path so they can rerun it.

The [adapter guide](docs/adapters.md) documents the public methods, the Python-function example, and fixes for each missing-evidence outcome. The [plain integration test](examples/support_search/baseline.py) uses the same HTTP fixture and worker hooks if your team wants a direct comparison.

## What the result covers

The old worker stays held until the declared caller sees the replacement on the declared route. Once that freshness probe sees the new revision, Setrya treats a forbidden passage in an applicable result as a failure at any returned rank. A passing run observes the old worker resuming, resolves what happened to its write on the tested search path, and checks the updated passage and the control. The recorded route, caller scope, questions, result depth, and observation window define the claim. Generated answers, other caches, and physical deletion each require their own checks. [Verdict and evidence contract](docs/evidence.md)

The demo is synthetic. For a related incident in a real search pipeline, see the stale index entries reported in [Dify](https://github.com/langgenius/dify/issues/44363) and its [merged regression test](https://github.com/langgenius/dify/pull/44364).

## Develop and verify

```sh
python3 -m unittest discover -s tests -v
python3 -m setrya qualify
```

Use `python3 -m setrya qualify --case retired-chunks` to run one acceptance case. The full suite covers late writes, retired chunks, early stale reads, missing evidence, adapter handoff, replay, concurrent runs, and 90 first-attempt trials. See the [release qualification guide](docs/qualification.md) and [contributing guide](CONTRIBUTING.md).

Setrya is licensed under [Apache 2.0](LICENSE).
