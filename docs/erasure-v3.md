# Test deletion in an application

A v3 drill asks whether the same saved conversation still reveals a deleted fact and whether its other finding survives. The two questions must be independently answerable before erasure. Start with one authenticated control caller. Add each distinct caller whose access path matters. Each caller contributes two questions at each observation stage. The complete evidence file must fit the 16 MiB replay limit. The app returns the serving identity, conversation, route and actual question for every response. Setrya records the fact checks and question digests. Keep caller aliases, conversation IDs, store keys, revisions and evidence references safe for the intended reviewer.

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

Declare the shared conversation, route, callers, target and control questions, store keys, and handling rules before running the test. The app owns the operation that rewrites or deletes its data. A case may contain one private key to delete or any combination of delete, rewrite and preserve rules. Each caller must reach the target fact before deletion and continue to reach an independent control fact afterward. The `rewrite` rule requires a present artifact containing both facts beforehand, a distinct revision at the same key afterward, the retained fact still present, and the erased fact absent. The `preserve` rule checks the retained artifact and its original revision. A `delete` rule requires the exact key to disappear. A missing direct read prevents PASS even when the serving endpoint returns a clean answer.

A scanner searches a disposable scope for the exact synthetic marker and returns its keys. Each scanner needs a positive baseline. A match at an unlisted key adds that location to the required direct reads. Setrya calls the application's existing erase operation and then checks the exact key and repeats the scan. New matches anywhere in the scanned scope fail the drill. The scanner identifies what needs verification. Only the application chooses which records to mutate. Before calling erase, Setrya checks that keys scheduled for removal, including scanner discoveries, have an observed absence of the retained finding. If the control is present, declare an explicit rewrite and verify the preserved fact on the same key. An unknown control blocks the erase. A missing marker cannot establish that a paraphrase disappeared. The separately baselined fact question and direct inspection exercise that case.

With `require_lineage=True`, the app supplies the source revisions and reachable derivations. The checker binds every affected version to a baseline observation or a declared external item. An affected version outside the observed scope remains a gap. The evidence includes co-parent versions used to build a shared artifact. Coverage depends on the graph and source registrations the application actually supplies.

* `deletion` samples each question as every declared caller, runs the app's erase operation, then repeats endpoint reads, direct inspections, and scans twice.
* `write-back` first holds a deferred writer after it captured the original inputs. Setrya checks the attempt before erasure and after the immediate observations, releases and drains that same attempt, then repeats all reads. A missing checkpoint or mismatched drain prevents a technical PASS. Implement `cancel_and_drain(subject, attempt)` to discard the paused write when erasure is withheld or the pre-release checkpoint disappears. Fixtures with only a release hook use it for cleanup. The CLI and Python runner also accept `late-write` in 1.x. Recorded evidence keeps the `late-write` value for replay.

Both modes record drill duration in milliseconds. Installation, fixture wiring, signing and human review are separate measurements.

## Use the SDK

Import `Artifact`, `SharedCase`, `Stored`, `Answer`, `ExternalItem` and `run_shared` from `setrya.erasure`. Historical `setrya.erasure.v3` imports resolve to the same types. Derive a test subject with `setrya.erasure.Subject.derive`. Supply the following callbacks on your fixture:

| Callback | Return value |
| --- | --- |
| `inspect(subject, store, key)` | `Stored(key, exists, target_present, control_present, revision)` from a direct read. Confirmed absence has no revision. |
| `scan(subject, store)` | Set of opaque keys found in that scoped text search. |
| `read(subject, caller, conversation, question)` | `Answer(caller, conversation, route, question, target_present, control_present)` using the actual response, or a `TextAnswer` for the configured judges. |
| `erase(subject)` | Complete the app's scoped deletion and rewrite. Wait for relevant refreshes. |
| `maintain(subject, step)` | For a declared `maintenance_steps` tuple, run the named operation and return `Maintenance(step)` after it commits. Setrya rereads the declared artifacts before invoking the next step. |
| `hold`, `checkpoint`, `release_and_drain` | Attempt-bound `Hold` and `Drain` observations for the deferred writer. The write-back mode invokes these hooks. |
| `lineage(subject)` | Optional mapping with `roots` and `derivations`. Every version is a `store`, `key`, `revision` triple. Each derivation identifies an `artifact` and its `inputs`. |
| `collect_journals(subject)` | Optional hook after final reads to seal and import durable worker packets before coverage is evaluated. |

For distributed coverage, supply a `CollectedJournal`, `run_id` and `expected_producers` to `run_shared`. The checker reads the producer roster and copy records from one SQLite snapshot after collection. Missing or unexpected producers prevent PASS, including producers observed only in traces. Recorded copy locations outside the declared observations and external review items are gaps.

Use maintenance steps to check that a deletion persists and a held record survives consolidation or a summary rebuild. This uses the same artifact rules and verdicts. [Legal hold example](legal-hold.md)

## Judge the answer the endpoint served

By default the runner makes three separate reads per caller and question at each stage. Increase `samples` for endpoints whose answers vary. Add indirect questions to exercise other ways a user might recover the same fact. Run against a disposable endpoint and budget for model and judge calls. A PASS covers the sampled responses. An intermittent leak can escape a finite sample.

This example declares the possible whole answers from a synthetic shipment service. A marker can identify a literal disclosure inside a longer answer. The reviewed-answer rule also recognizes a spoken form of the code. It establishes absence only when the **entire answer** matches a reply an engineer reviewed. Unrecognized wording yields an uncertain decision.

```python
from setrya.erasure import AnswerJudges, Artifact, Marker, ReviewedAnswers, SharedCase

case = SharedCase(
    conversation="support-17",
    route="/ask",
    callers=("agent-one",),
    target_question="Which shipment was recalled?",
    control_question="Which carrier handles returns?",
    target_variants=("Which box should the depot hold?",),
    artifacts=(Artifact("orders", "shipment-17", "delete"),),
    samples=5,
    judges=AnswerJudges(
        target=(Marker("ORCHID-7"), ReviewedAnswers(
            present=("ORCHID-7", "the orchid seven shipment"),
            absent=("Juniper", "No matching order."),
        )),
        control=ReviewedAnswers(
            present=("Juniper",),
            absent=("ORCHID-7", "the orchid seven shipment", "No matching order."),
        ),
    ),
)
```

For an open-ended model route, declare `model_backed=True` and return `TextAnswer` with the caller, route, conversation, question and actual response text. Supply a versioned app evaluator with `ModelJudge("model-id/rubric-v2", evaluate, paraphrase_aware=True)` when its rubric checks whether the private fact is conveyed in different words. The callable returns `ModelDecision(present, reason, match)` and quotes the answer on a positive decision. Return `present=None` when the meaning is unclear. Replay recomputes the result from the recorded decisions offline.

One positive decision from any rule establishes presence. Absence needs every rule to return false and at least one reviewed whole-answer rule or a judge explicitly declared paraphrase-aware. A `Marker`, `Regex` or `Entity` alone can detect a leak, but a negative lexical match leaves the fact uncertain. The receipt labels that policy `lexical-only`. A required uncertain outcome produces **NOT TESTED**. Explicit `Answer` flags from deterministic application adapters are labelled `application-signals`. Those fixtures own the interpretation of their response and must return `None` for an unknown fact.

The `answer_sampling` section records the requested samples per question, judge coverage for both facts and read counts for baseline, immediate and final stages. Positive and uncertain counts cover the observed replies. Subtract observed from planned to count unobserved reads. Each evidence slot includes a question digest, caller and one-based sample ordinal. Setrya checks the target on every post-erasure response, including responses to the control questions. A single observed disclosure fails the drill even when another read is missing. Each caller must demonstrate each declared positive baseline before erasure. Every control sample must still show the retained fact afterward.

Supply up to eight unique questions per fact and one to 32 samples. Setrya bounds reads and judge evaluations per stage before calling the fixture. New person evidence uses `setrya.erasure.evidence.v3.1`. Each caller and question has its own list of samples. The receipt sets `lexical_only` when the fixture uses no model judge. Its separate coverage fields distinguish reviewed whole answers, lexical checks and paraphrase-aware judging for each fact. Turn fields and hash-chain heads appear when the journal provides them. The original single-read v3 and flat `v3.sampled` bundles replay with their issuing rules.

Choose the sample count for the failure you care about. If a route leaks on one in five independent requests, three reads have roughly a 49% chance of catching it at that stage. Fourteen reads raise that chance to about 96%. Cached responses or correlated generations can make repeated reads less informative. A PASS describes the answers actually observed.

The default `capture="digest"` stores the answer and match digests, the rule identity and a bounded explanation. For a model judge the explanation in this mode is withheld and hashed. With `capture="excerpt"`, the evidence includes the reason and the first 256 matched characters. Use synthetic fixtures for shareable excerpts. Low-entropy facts can also be guessed from a digest.

Setrya checks the maximum size of the planned answer evidence before taking a baseline or requesting erasure. If a large sample with excerpts exceeds the replay budget, use digest capture to keep the same reads and verdict rules.

## Offline replay

Keep `erasure-evidence.json`, `erasure-receipt.json` and the detached signature together. Each entry in `artifact_checks` links to the exact direct readings in the evidence. Replay recomputes both decisions from the evidence and checks its SHA-256 against the receipt. Verify the signature against an independently trusted key or workflow identity. Anyone able to fabricate the original observations controls what the receipt can attest.

V3 uses a distinct format name and a [published schema](../setrya/erasure/receipt-v3.schema.json). Historical v1 tenant and v2 subject receipts retain their original verdict and signature rules. [V2 evidence and signing](erasure.md)
