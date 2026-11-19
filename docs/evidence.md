# Verdict and evidence contract

Setrya uses a single runner-relative monotonic clock. Reports record the order and timing of lifecycle events, plus the start and end of each query. The runner issues serial probes so a query that started before the first verified new result cannot be mistaken for a subsequent read.

1. The adapter seeds the original revision and an unaffected control. The primary baseline proves that the original marker is present and the revision-specific replacement marker is absent.
2. The old attempt reads A and acknowledges that it is waiting at the checkpoint. An independent path returns a publication receipt for the exact run, document, and replacement revision. The runner timestamps the receipt and begins freshness sampling.
3. The designated freshness probe observes B on the declared route and caller scope before release and within the freshness window. A result containing B and a forbidden passage violates the assertion on that same probe. After this point, any verified forbidden passage in an applicable observation is a failure, including while the worker is still held.
4. The runner releases the old attempt. Under the default strict policy, for each final probe it needs either a matching terminal rejection observed before the query or a response from the declared route and caller scope with a watermark at or beyond the matching commit in the same index generation. Each final probe must find its required passages, be fully identifiable, and finish within the settle window.

A freshness deadline failure requires a completed, identified pre-deadline sample and an applicable boundary observation within `max_gap_ms`. The boundary observation must show absence in a query started at or after the cutoff, or first return the required revision after the cutoff. An unevaluable deadline yields NOT TESTED. The settle window bounds the wait for final evidence. Within that window, a fully identified final result that lacks a required passage after write resolution earns FAIL. Missing proof earns NOT TESTED. The checker keeps the first observed applicable violation. For each probe, the latest applicable final search inside the settle window must support its confirmation. If a later response contains an unidentified result or cannot show visibility of a committed write, an earlier good response is insufficient. A subsequent qualifying read can establish the missing proof.

The report attributes a stale passage to the old write only when its recorded origin matches the attempt and its response correlates to the relevant commit and search view. If the stale read preceded release, the report records that ordering. Other stale reads can establish FAIL with unknown write origin. If an adapter operation fails later, the checker keeps the verified FAIL and reports the failed operation and error type. An adapter error without a verified failure produces an execution error. The report carries the observations made before either error.

## Frozen search report v1

New reports identify themselves with `"format": "setrya.search"`, `"schema": 1` and `"checker": 1`. Untagged integer schemas 1 through 6 are historical formats; readers distinguish them before inspecting the events. Use `Report.load` for either format and inspect `report.format` and `report.version` for its wire identity. Version 1 stores the run ID, effective rules, ordered events and input fingerprints. The reader checks event order, clock order, field types and singleton cardinality before evaluating the trace. A second start, checkpoint, publication, release or receipt is invalid evidence. Repeated search samples are expected.

**Compatibility policy.** The v1 wire shape and verdict semantics are frozen. Future readers must keep replaying valid v1 traces under the v1 contract. New required evidence, changed verdict semantics or changed field meanings need a new version and a separate reader. The selected drill visibility policy travels with the report. Keep the artifact and the verifier release together for reproducible audits. A v1 report can be compared with a historical report when the effective rules and input fingerprints match. The legacy rules below describe how the historical reader works today.

Teams using a backend without per-query commit tokens can opt into a scoped refresh acknowledgment. PASS at this tier records a clean observation after the refresh with write visibility unverified. The strict policy still requires a rejected write or a matching watermark. [Store evidence and ingestion](store-signals.md)

## Replay and sharing

A JSON report saves schema and checker versions, an opaque run ID, text-free effective rules, ordered event times, actual query route and caller, passage markers, and publication, receipt, or read evidence. Replay uses those saved rules and events. It never invokes the adapter. Untagged legacy schema 1 reports still show their event history and replay as NOT TESTED because they lack the current publication and caller evidence. A corrupt or unsupported bundle exits with code 2.

Read portable reports with the installed `setrya replay` command or `python -I -m setrya replay`. Keep report artifacts with the exact commit and CI run. Share only synthetic or approved identifiers in public GitHub issues. Default output has no query or passage strings. The bundled fixtures use synthetic data and their independent search endpoint returns the full synthetic snippets to its local caller.

## Compare two saved runs

Run `python3 -m setrya compare before.json after.json --summary comparison.md` to check a patch offline. A verified fix starts with a recorded failure and ends with a passing run that proves the affected probe in the same phase. A missing prerequisite or final write visibility stays inconclusive. A candidate failure blocks review. The comparison calls it a regression when the base has confirming evidence for that probe in the affected phase. Otherwise it reports a failure observed on the candidate. The output names the probe, caller, route, depth, and decisive events. Exit codes are 0 for a verified fix, 1 for a failure in the after run, 2 for an input or execution error, and 3 for an inconclusive or incomparable pair.

Schema 3 reports carry the drill format version, effective rules, and SHA-256 fingerprints of the adapter factory and each exact question. Comparisons require these bindings to match. Adapter options and implementation code can change across patch runs. Keep the test data, hooks, and intended environment equivalent, and record the commits with the reports. Schema 2 verdicts still replay. Generate two current reports to compare them.

The question fingerprint is an unkeyed digest for equality checks. It can reveal the use of a guessable question to someone who hashes a candidate. Share synthetic or approved report data publicly. The command prints the fingerprint and probe ID, and raw question text stays out of the report.

## Recorded tests and release reviews

Schema 4 can carry a full application commit ID. It records a test failure by exception type and checks the recorded checkpoint deadline. Existing schema 2 and 3 reports remain replayable, and schema 3 reports retain the input fingerprints for offline comparison.

Schema 5 marks the stronger final-read rule. The current checker applies this rule when replaying readable schema 2–4 reports, so an older PASS can become NOT TESTED if a later recorded read lost the evidence needed to confirm it. Use Setrya 0.5 or later to replay a schema 5 report.

Schema 6 also requires the matching terminal receipt to arrive inside the settle window. A search watermark can confirm a commit retrospectively when both observations met that deadline. Replay checks the checkpoint and settle deadlines wherever older reports recorded the required times. Older results can become NOT TESTED under these rules. Use Setrya 0.5.1 or later to read schema 6.

Where legacy schemas differ from newer contracts, retain the original verifier to reproduce the original release decision.

A release review requires reports for the expected base and candidate commits and consistent native test exit codes. It can pass when both comparable tests pass or when the candidate verifies a fix. `compare` continues to require a failing baseline to claim a fix. Questions stay in the trusted drill. Reports name the probe and its fingerprint. [Recording and CI](recording.md)
