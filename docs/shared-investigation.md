# Erase one finding from a shared investigation

Two security analysts work in the same saved investigation. One source identifies a quarantined laptop. Another records a colleague’s credential reset. A background worker has already read both findings and is preparing a handoff.

```sh
python -m pip install 'setrya[erasure]'
setrya erasure demo --output investigation
setrya replay investigation/guarded
```

Both callers first recover both fictional facts through the authenticated HTTP endpoint. After deletion, the application rebuilds the shared summary, answer cache and LangGraph checkpoint under the original conversation ID. The endpoint opens the SQLite-backed saver again for each request. The test asks “Which laptop was quarantined?” and separately asks about the credential reset. Neither question identifies a person or includes the synthetic canary.

* In `broken`, the delayed worker saves its stale inputs. Both users can recover the laptop identifier again. The receipt identifies the endpoint reads and the stored derivatives containing the residual.
* In `guarded`, the writer compares the input revisions it captured with the current source revisions before saving. Both users can recover the credential reset after the worker has drained. The laptop finding is absent from the answers, saved summary, cached answer, and surviving checkpoint history.
* The guarded receipt gives engineering a technical `PASS`. A fictional provider observation stays with its case owner under `OPEN_ITEMS`. A reviewer can inspect the exhibit reference and follow up with that owner.

The laptop and reset probes are deterministic checks for two seeded facts. There is no model call in this fixture. The signed test result covers the declared conversation, stores, two callers, questions and schedule. A production app provides its own authorized rewrite and observed route.

## Identify affected revisions

`VersionLineage` stores immutable input relationships next to a `CopyJournal`. Pick an opaque namespace for the tenant or disposable workspace. Register each source revision and record the exact revisions consumed at publication. An unchanged version can be registered again with the same inputs.

```python
from setrya.erasure.lineage import VersionLineage, VersionRef

lineage = VersionLineage(journal, "test-workspace")
finding_a = VersionRef("source", "finding-17", "1")
finding_b = VersionRef("source", "finding-28", "1")
summary = VersionRef("summaries", "incident-41", "2")
answer = VersionRef("answers", "incident-41", "3")
lineage.record(finding_a, subjects=[alice])
lineage.record(finding_b, subjects=[bob])
lineage.record(summary, inputs=[finding_a, finding_b])
lineage.record(answer, inputs=[summary])
affected = lineage.affected(alice)
```

The affected graph contains Alice’s original revision, the mixed summary, and the answer derived from that summary. The summary’s input list also names Bob’s retained revision. Relationships record observed publication and can describe old versions. Inspect each affected live or historical version in its store. A deployment must arrange durable lineage recording alongside its write or report any gaps.

## Discover a snapshot whose tag is missing

The example creates a scoped, disposable export whose key is generated at runtime. It has no subject tag or journal entry. The export copies the original finding verbatim, including its unique synthetic marker. A direct scan reports its store and key before deletion. The application’s erasure policy removes a disposable export only when it matches the scoped subject’s original finding byte for byte. A mixed export remains available for review. A row in a different investigation containing the same marker survives.

```python
from setrya.erasure.canary import SQLiteCanaryScanner

scanner = SQLiteCanaryScanner(connection, table="snapshots", key="id",
                              content="content", scope_column="conversation")
matched_keys = scanner.find(scope="incident-41", marker=synthetic_marker)
```

A marker match identifies a location to inspect. A paraphrased summary can omit the marker entirely. That is why the drill also checks the fictional fact in the stored content and in answers. The scanner only reads the declared column and scope. The adapter decides which copies the application is authorized to remove or rebuild.

The demo’s deletion-only mode produces a guarded receipt with the race marked “not exercised.” Use a fresh output directory for each run.

```sh
setrya erasure demo --mode deletion --output deletion-only
setrya replay deletion-only/guarded
```
