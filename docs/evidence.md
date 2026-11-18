# Verdict and evidence contract

Setrya uses a single runner-relative monotonic clock. Reports record the order and timing of lifecycle events, plus the start and end of each query. The runner issues serial probes so a query that started before the first verified new result cannot be mistaken for a subsequent read.

1. The adapter seeds the original revision and an unaffected control. The primary baseline proves that the original marker is present and the revision-specific replacement marker is absent.
2. The old attempt reads A and acknowledges that it is waiting at the checkpoint. An independent path returns a publication receipt for the exact run, document, and replacement revision. The runner timestamps the receipt and begins freshness sampling.
3. The designated freshness probe observes B on the declared route and caller scope before release and within the freshness window. A result containing B and a forbidden passage violates the assertion on that same probe. After this point, any verified forbidden passage in an applicable observation is a failure, including while the worker is still held.
4. The runner releases the old attempt. For each final probe it needs either a matching terminal rejection observed before the query or a response from the declared route and caller scope with a watermark at or beyond the matching commit in the same index generation. Each final probe must find its required passages, be fully identifiable, and finish within the settle window.

A freshness deadline failure requires a completed, identified pre-deadline sample and an applicable boundary observation within `max_gap_ms`. The boundary observation must show absence in a query started at or after the cutoff, or first return the required revision after the cutoff. An unevaluable deadline yields NOT TESTED. The settle window bounds the wait for final evidence. Within that window, a fully identified final result that lacks a required passage after write resolution earns FAIL. Missing proof earns NOT TESTED. The checker keeps the first observed applicable violation.

The report attributes a stale passage to the old write only when its recorded origin matches the attempt and its response correlates to the relevant commit and search view. If the stale read preceded release, the report records that ordering. Other stale reads can establish FAIL with unknown write origin. If an adapter operation fails later, the checker keeps the verified FAIL and reports the failed operation and error type. An adapter error without a verified failure produces an execution error. The report carries the observations made before either error.

## Replay and sharing

A JSON report saves schema and checker versions, an opaque run ID, text-free effective rules, ordered event times, actual query route and caller, passage markers, and publication, receipt, or read evidence. Replay uses those saved rules and events. It never invokes the adapter. Schema 1 reports still show their event history and replay as NOT TESTED because they lack the current publication and caller evidence. A corrupt or unsupported bundle exits with code 2.

Keep report artifacts with the exact commit and CI run. Share only synthetic or approved identifiers in public GitHub issues. Default output has no query or passage strings. The bundled fixtures use synthetic data and their independent search endpoint returns the full synthetic snippets to its local caller.

## Compare two saved runs

Run `python3 -m setrya compare before.json after.json --summary comparison.md` to check a patch offline. A verified fix starts with a recorded failure and ends with a passing run that proves the affected probe in the same phase. A missing prerequisite or final write visibility stays inconclusive. The output names the probe, caller, route, depth, and decisive events. Exit codes are 0 for a verified fix, 1 for a failure in the after run, 2 for an input or execution error, and 3 for an inconclusive or incomparable pair.

Schema 3 reports carry the drill format version, effective rules, and SHA-256 fingerprints of the adapter factory and each exact question. Comparisons require these bindings to match. Adapter options and implementation code can change across patch runs. Keep the test data, hooks, and intended environment equivalent, and record the commits with the reports. Schema 2 verdicts still replay. Generate two current reports to compare them.

The question fingerprint is an unkeyed digest for equality checks. It can reveal the use of a guessable question to someone who hashes a candidate. Share synthetic or approved report data publicly. The command prints the fingerprint and probe ID, and raw question text stays out of the report.
