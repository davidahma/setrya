# Setrya

**Regression tests for what search returns after an indexing change.**

Your team shortens the refund window from 30 days to 14. The support assistant’s search returns the new policy. An hour later, an indexing job resumes with the copy it read yesterday. The same search now returns the 30-day policy.

Setrya lets engineers who maintain search for AI agents, support assistants, and internal tools reproduce that sequence on demand. Pause the old worker, publish the replacement, and check what the same caller retrieves before and after the worker resumes. Save the run so the next engineer can see which revision came back, at what rank, and in what order.

[![Tests](https://github.com/davidahma/setrya/actions/workflows/ci.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/ci.yml)
[![Offline qualification](https://github.com/davidahma/setrya/actions/workflows/qualification.yml/badge.svg)](https://github.com/davidahma/setrya/actions/workflows/qualification.yml)

## Run the demo

You need Git and Python 3.11 or later with SQLite FTS5. Once cloned, the demo runs offline with synthetic documents and the Python standard library.

```sh
git clone https://github.com/davidahma/setrya.git
cd setrya
python3 -m setrya demo
```

The demo runs the same refund-policy change three ways. The last two look identical to someone spot-checking search.

| Verdict | What the support agent finds | What happened to the old write |
| --- | --- | --- |
| **FAIL** | 30 days → 14 days → 30 days | The worker wrote the old revision back. The old policy was the first search result. |
| **PASS** | 30 days → 14 days | The worker checked the source revision and rejected its stale write. Final searches found the new refund policy and the unaffected delivery policy. |
| **NOT TESTED** | 30 days → 14 days | The old write committed. The search response supplied no evidence that it had seen that commit. |

## Save a run and replay it in code review

The bundled HTTP example starts a disposable search app with its own worker and SQLite index. It runs the fixed case through the app’s regular search endpoint and writes two files.

```sh
python3 -m setrya check examples/support-search/drill.toml --report run.json --summary summary.md
python3 -m setrya replay run.json
```

Share `summary.md` in the pull request and attach `run.json` to the CI run. After the app shuts down, a reviewer can replay the JSON to recompute the verdict from the recorded rules and observations. Reports include passage IDs, caller, route, rank order, timing, and write evidence. Question text and passage text stay out of the report. Use identifiers you can share.

For CI, `check` and `replay` exit **0** for PASS, **1** for FAIL, **2** for an input or execution error, and **3** for NOT TESTED. The demo exits successfully when all three examples produce their expected outcomes. The [CI workflow](.github/workflows/ci.yml) shows how to retain and replay a failing report.

## Use Setrya with AI agents

If an agent searches company policy before suggesting a refund, run the drill against its search tool using the agent’s caller identity. The report shows which policy the tool returned. Testing the refund the agent recommends or issues takes a separate check. The [Python example](examples/support-search/python.toml) shows how to exercise a retrieval function directly.

## Decide whether an indexing patch fixed the result

Save reports for the broken version and the patch using the same question, caller, and result depth. Compare them after both apps shut down.

```sh
python3 -m setrya compare before.json after.json --summary comparison.md
python3 -m setrya diagnose after.json
```

The comparison shows the affected probe and decisive observations. Changed questions or depths make the runs incomparable. Missing write evidence leaves the result inconclusive. Attach the report pair and Markdown summary to the pull request. [Evidence and comparison contract](docs/evidence.md#compare-two-saved-runs)

## Keep your existing pytest test

If your test already controls the indexing worker, add a recorder at the checkpoint, publication, and search calls. Run the same test on the base and candidate commits, then use `setrya review` to publish one CI decision with the two reports. [Recording and CI example](docs/recording.md)

## Connect your own search

Start with one document your users rely on, one question that retrieves it, and a second question about an unrelated document. Run the drill against a disposable instance of your application.

1. **Define what must stay findable.** Copy the [HTTP drill](examples/support-search/drill.toml). It asks the support agent’s refund question at a depth of three, requires `refund@B`, and forbids `refund@A`. A separate delivery query checks that `delivery@1` survives the update. If your results lack revision IDs, put distinct markers in the synthetic passages and map them to IDs in your adapter.
2. **Control the timing.** Connect a test hook after the indexing worker reads the old revision and before it writes. While that worker waits, publish the new revision independently and return a receipt identifying what was published.
3. **Observe the user’s search path.** Have the adapter call the endpoint or retrieval function the app uses and report the route and caller that actually served the response. After release, supply either a rejection receipt for the old attempt or evidence that each final search saw an index snapshot at or beyond its commit. If evidence is missing, the report explains which observation the drill needs.

The [adapter guide](docs/adapters.md) covers the interface, a Python retrieval function, pytest integration, and troubleshooting. To see how a conventional integration test handles the same worker and search endpoint, read the [standalone comparison](examples/support_search/baseline.py).

## What a pass covers

A PASS applies to the configured caller, route, queries, result depths, and time windows. The intended caller saw the replacement while the old worker was paused. After release, each query found its required passages in a search that accounted for the old write. If a search returns both refund policies, it fails even when the new one ranks first. A forbidden passage in a later result on the tested path also fails the drill. Conversation history, other caches, and physical deletion need separate tests. [Verdict and evidence contract](docs/evidence.md)

A document can also shrink from five chunks to three while a removed clause still turns up in search. The [retired-chunk drill](examples/support-search/retired.toml) asks specifically for that clause, which a general refund query can miss.

The optional [PostgreSQL/pgvector example](docs/pgvector.md) catches a second failure. During delete-and-reupload, an orphaned candidate can take a top-three slot, leaving two current passages while a required clause drops out. The [paired pytest benchmark](docs/benchmark.md) runs the late overwrite and re-upload against the same app and hooks.

## Develop and verify

```sh
python3 -m unittest discover -s tests -v
python3 -m setrya qualify
```

The [qualification guide](docs/qualification.md) covers offline runs, replay, and 90 first-attempt repeatability trials. See [Contributing](CONTRIBUTING.md) to submit a drill or change. Setrya is licensed under [Apache 2.0](LICENSE).
