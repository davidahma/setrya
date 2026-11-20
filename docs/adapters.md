# Connect a disposable search application

Start with the [HTTP configuration](../setrya/examples/support-search/drill.toml) or run the same drill through the application's [Python retrieval function](../setrya/examples/support-search/python.toml).

```sh
python3 -m setrya check setrya/examples/support-search/python.toml --report run.json
```

A drill names a Python `module:factory` inside the trusted checkout or Python path. Both the installed `setrya` command and `python3 -m setrya` can import adapters from the working directory. Treat a drill and its adapter as executable test code. Setrya imports the adapter module and calls its factory with a `RunContext` containing a run ID, an isolated temporary directory, and the parsed drill. Use the run ID to namespace external state. Use only disposable data and make each operation bounded. Setrya manages the polling schedule, and the adapter manages timeouts for its own calls.

## Adapter methods

The [public types](../setrya/adapter.py) and the [reference adapter](../setrya/examples/support_search/adapter.py) show the complete interface.

| Method | What the app supplies |
| --- | --- |
| `seed()` | A disposable source revision A and a searchable unrelated control. |
| `start_old()` | A job that reads A, plus the ID of that exact attempt. |
| `checkpoint()` | Acknowledgment that the attempt holds the revision it actually read and is waiting at the named checkpoint. Return `None` while the acknowledgment is unavailable. |
| `publish()` | A `Publication(run_id, document, revision)` receipt for what was actually published. Return `None` if acknowledgment is unavailable. Keep this path independent of the held attempt. |
| `search(probe)` | A `SearchResult` with ordered passage IDs at the requested depth and the route and scope that actually served the response. Use `None` for an unidentified passage. Include a read snapshot when the path exposes one. |
| `release()` | Release of the specific held attempt. |
| `receipt()` | A terminal rejection or commit for the specific old attempt. Return `None` while terminal evidence is unavailable. |
| `close()` | Bounded cleanup of resources owned by the current run. The factory cleans up any resources it started if initialization fails. |

Use the IDs of returned revisions or map distinct markers in synthetic passages to stable, opaque IDs. A document ID shared across revisions can accompany a revision-specific marker. The primary probe needs a required marker unique to the replacement, and a forbidden marker observed at baseline. Keep those markers out of questions. Only count text from the search response that the named caller actually receives. For write attribution, a result can also carry the attempt that produced each passage. A rejection receipt binds to the run, attempt, document, and old revision. A commit receipt also carries an index generation and commit number. A read watermark describes the snapshot that produced the response, including its route and caller scope. A result without a watermark can still identify the actual route and caller using `SearchResult.route` and `SearchResult.scope`. Use the identity of the retrieval path the adapter exercised. Copying the expected identity from the drill can conceal a routing mistake.

The runner polls the selected attempt’s checkpoint and receipt again before publication and release. Return the same checkpoint while the attempt is held. Return `None` from `receipt()` while it is pending. The release hook should atomically require the attempt to still be held. A recorded release belongs only to a successful hook call.

## Drill fields

The file uses `version = 1`, an `[adapter]` table, a `[rules]` table, and two or more `[[probe]]` tables.

- `adapter.factory` selects the Python factory. `adapter.options` supplies app-specific settings.
- `rules.id`, `document`, `old_revision`, `new_revision`, and `checkpoint` identify the change. Use short, non-sensitive IDs.
- `freshness_probe` names the probe that first establishes B. `freshness_ms` measures from the publication acknowledgment. `checkpoint_ms` limits the wait for the old attempt and `settle_ms` limits the search and write evidence window after release. `sample_ms` schedules probes and `max_gap_ms` bounds the gap around the freshness deadline.
- Each probe has `id`, `route`, `scope`, `question`, `top_k`, `require`, and `forbid`. Optional `before` markers establish the baseline. Give the freshness probe at least one baseline marker and one forbidden marker.

## When a drill reports NOT TESTED

| Report says | What to check |
| --- | --- |
| Missing checkpoint acknowledgment | Verify the worker received the job and reports the run, attempt, document, revision it read, and checkpoint name after it pauses. |
| Baseline search did not distinguish the original revision | Reproduce the query in the same route, caller scope, and depth. Choose a query that retrieves an old marker and verify that the replacement marker is absent before publication. |
| Missing matching publication | Inspect the update acknowledgment and return the actual run, document, and replacement revision. |
| Missing freshness observation on the declared route and caller | Check which endpoint and caller served the probe while the worker was held. A later response from the intended caller cannot establish that event order. |
| Revision or passage identity missing | Return revision metadata or map a distinct synthetic passage marker to an opaque ID. |
| Write rejection or visibility evidence missing | Bind the rejection to the held attempt or return the watermark for the snapshot that served each probe. A worker log or index database inspection establishes a different observation. |
| Freshness deadline could not be evaluated | Inspect the saved event times. Make sure the sample schedule covers the deadline, the app responds within the sampling window, and results carry passage IDs. |

## Import from Python or pytest

Call `setrya.runner.run` with a parsed `Drill` or a path. If you construct a `Drill` directly, use the same `ProbeRules` in `drill.rules.probes` and in each `Probe`. A mismatch is rejected before the adapter loads. It returns a `RunResult` with `verdict` and `report`. Save the latter using `result.report.save("run.json")`.

For pytest, install the optional dependency using `python3 -m pip install '.[pytest]'` on a development machine, then enable the plugin with `python3 -m pytest -p setrya.pytest_plugin`.

```python
def test_refund_index(setrya_check):
    setrya_check("setrya/examples/support-search/drill.toml")
```

If your existing pytest test owns worker timing and calls the search endpoint, use the `setrya_record` fixture to capture those observations in the test. [Record an existing test](recording.md)

## PostgreSQL reference integration

The [pgvector walkthrough](pgvector.md) uses an independent queued worker, current-upload filtering, and the normal HTTP search response. Run `setrya diagnose REPORT` for the first missing evidence when a check is inconclusive. For example, a terminal write receipt and a search snapshot watermark are separate observations. Use [offline comparison](evidence.md#compare-two-saved-runs) to hand off results from a broken run and its patch.

The SQLite demo and its bundled adapters need FTS5. Other adapters use their own backend prerequisites. If an adapter changes the working directory during a check, relative report and summary destinations still refer to the directory where the check began. Use separate paths for the drill and each output.

For chunk metadata, native PostgreSQL positions, Elasticsearch sequence numbers and refresh-only observations, see [Store evidence and ingestion](store-signals.md).

Run the three store controls or verify a queue task's capture, hold, release and terminal receipt before relying on an adapter in CI. [Adapter conformance suite](adapter-conformance.md)

## Schedule final reads after a receipt

By default, Setrya samples the read path as soon as it releases the worker. To compare two revisions specifically after the worker finishes, an adapter can set `final_reads_after_receipt = True`. The runner keeps polling on its original settle deadline and starts final queries when it receives the terminal receipt. A missing receipt leaves the post-write phase untested. This schedule omits the interval between release and acknowledgment. Use the default when that interval is part of the test.
