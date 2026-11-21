# Review erasure in a shared conversation

A v3 drill asks whether the same saved conversation still reveals a deleted fact and whether its other finding survives. The two questions must be independently answerable before erasure. The app identifies two authenticated callers and returns the conversation, route, and actual question for each response. Setrya records the fact checks and question digests. Keep caller aliases, conversation IDs, store keys, revisions and evidence references safe for the intended reviewer.

## Two decisions

| Observation | Technical verdict | Case review |
| --- | --- | --- |
| All declared application checks passed | PASS | READY_FOR_REVIEW |
| Application checks passed and a provider, backup or attestation needs follow-up | PASS | OPEN_ITEMS |
| A required reading, positive baseline or worker acknowledgment was missing | NOT TESTED | OPEN_ITEMS |
| An adapter raised an error | ERROR | OPEN_ITEMS |
| The erased fact returned, a removed record survived, or the retained finding was lost | FAIL | RESIDUAL_FOUND |

The engineering exit codes are 0 for PASS, 1 for FAIL, 2 for ERROR and 3 for NOT TESTED. Case review also exits 3 for an open external item on a technical PASS. A witnessed failure takes precedence over a later error. Each external item identifies its system, key, owner, disposition, evidence reference, the test subject and a follow-up date when available. Keep provider configuration and owner exhibits in your case system. Owners can use the evidence reference to retrieve them. Every external item stays open in this drill. Erasure from the application's required stores needs direct readings.

## Required evidence

Declare the shared conversation, route, callers, two questions, store keys, and handling rules before running the test. The app owns the semantic checks and the operation that rewrites or deletes its data. The `rewrite` rule requires a present artifact containing both facts beforehand, a distinct revision at the same key afterward, the retained fact still present, and the erased fact absent. The `preserve` rule checks the retained artifact and its original revision. A `delete` rule requires the exact key to disappear. A missing direct read prevents PASS even when the serving endpoint returns a clean answer.

A scanner searches a disposable scope for the exact synthetic marker and returns its keys. Each scanner needs a positive baseline. A match at an unlisted key adds that location to the required direct reads. Setrya calls the application's existing erase operation and then checks the exact key and repeats the scan. New matches anywhere in the scanned scope fail the drill. The scanner identifies what needs verification. Only the application chooses which records to mutate. Before calling erase, Setrya checks that keys scheduled for removal, including scanner discoveries, have an observed absence of the retained finding. If the control is present, declare an explicit rewrite and verify the preserved fact on the same key. An unknown control blocks the erase. A missing marker cannot establish that a paraphrase disappeared. The separately baselined fact question and direct inspection exercise that case.

With `require_lineage=True`, the app supplies the source revisions and reachable derivations. The checker binds every affected version to a baseline observation or a declared external item. An affected version outside the observed scope remains a gap. The evidence includes co-parent versions used to build a shared artifact. Coverage depends on the graph and source registrations the application actually supplies.

* `deletion` reads both questions as both callers, runs the app's erase operation, then repeats endpoint reads, direct inspections and scans twice. It reports **worker race not exercised**.
* `late-write` first holds a worker after it captured the original inputs. Setrya checks the attempt before erasure and after the immediate observations, releases and drains that same attempt, then repeats all reads. A missing checkpoint or mismatched drain prevents a technical PASS.

Both modes record drill duration in milliseconds. Installation, fixture wiring, signing and human review are separate measurements.

## Use the SDK

Import `Artifact`, `SharedCase`, `Stored`, `Answer`, `ExternalItem` and `run_shared` from `setrya.erasure.v3`. Derive a test subject with `setrya.erasure.Subject.derive`. Supply the following callbacks on your fixture:

| Callback | Return value |
| --- | --- |
| `inspect(subject, store, key)` | `Stored(key, exists, target_present, control_present, revision)` from a direct read. Confirmed absence has no revision. |
| `scan(subject, store)` | Set of opaque keys found in that scoped text search. |
| `read(subject, caller, conversation, question)` | `Answer(caller, conversation, route, question, target_present, control_present)` using the actual response. |
| `erase(subject)` | Complete the app's scoped deletion and rewrite. Wait for relevant refreshes. |
| `hold`, `checkpoint`, `release_and_drain` | Same attempt-bound `Hold` and `Drain` observations as the subject drill. Only the late-write mode dispatches a worker. |
| `lineage(subject)` | Optional mapping with `roots` and `derivations`. Every version is a `store`, `key`, `revision` triple. Each derivation identifies an `artifact` and its `inputs`. |
| `collect_journals(subject)` | Optional hook after final reads to seal and import durable worker packets before coverage is evaluated. |

For distributed coverage, supply a `CollectedJournal`, `run_id` and `expected_producers` to `run_shared`. The checker reads the producer roster and copy records from one SQLite snapshot after collection. Missing or unexpected producers prevent PASS, including producers observed only in traces. Recorded copy locations outside the declared observations and external review items are gaps.

## Offline replay

Keep `erasure-evidence.json`, `erasure-receipt.json` and the detached signature together. Each entry in `artifact_checks` links to the exact direct readings in the evidence. Replay recomputes both decisions from the evidence and checks its SHA-256 against the receipt. Verify the signature against an independently trusted key or workflow identity. Anyone able to fabricate the original observations controls what the receipt can attest.

V3 uses a distinct format name and a [published schema](../setrya/erasure/receipt-v3.schema.json). Historical v1 tenant and v2 subject receipts retain their original verdict and signature rules. [V2 evidence and signing](erasure.md)
