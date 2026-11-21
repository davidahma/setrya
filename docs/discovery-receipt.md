# Replay a discovery case

A Setrya discovery bundle ties one reviewed classification finding to one application environment, revision, and synthetic subject. Keep the mapping under the case owner's control. The application fixture supplies its environment and deployed Git revision independently and binds the subject through the application's normal test identity.

| File | What a reviewer can check |
| --- | --- |
| `case-input.json` | The approved scope, finding digest, opaque asset reference, case ID, mode, and a fresh random nonce. |
| `erasure/erasure-evidence.json` | Store reads for exact keys and revisions, the declared callers' endpoint observations, source relationships and the worker outcome. |
| `erasure/erasure-receipt.json` | The native v3 decision and SHA-256 of its evidence. The run ID binds the normalized case input, mode and nonce. |
| `case-result.json` | The discovery decision, per-service outcomes, owners, follow-up items and links to observations. This file includes the SHA-256 of the exact v3 receipt. |

Replay recomputes the v3 receipt and case result from the saved evidence. A passing v3 result can still produce a discovery `NOT TESTED` when a catalog-declared application service lacks a direct check. A witnessed failure takes precedence over a coverage gap. An external provider item remains open for its owner. Missing provider evidence alone leaves the engineering decision on the directly testable scope.

## Where the classification connects

Each direct service declaration names an approved store, key and handling rule. The fixture can only request actions from that list. Before calling the application's erase operation, Setrya requires a positive baseline, the mapped source revision, and a path from that subject's source revision to every affected version the fixture proposes to change. Another root belonging to the same person cannot establish that path. Preserved controls must lie outside it. A missing observation of the approved source or its dependencies leaves the deletion untested. The fixture must implement `erase_approved(subject, source, actions)`. In the application's transaction, verify the source key, subject, and expected revision, constrain writes to the approved actions, and preserve other people's content. Return `True` after committing or `False` after an atomic refusal with no writes. An exception reports an operation error and can follow a partial write. The runner checks the scope before dispatching a late writer and again at the held baseline. Supply `cancel_and_drain(subject, attempt)` to retire a held job without publishing its stale inputs if those checks change or the mutation cannot complete. Discovery v1 requires explicit keys for the approved operations. Standalone erasure drills also support marker searches.

When the same subject owns several independent source records, build the fixture’s `lineage(subject)` with `versions.affected(subject, source=VersionRef(store, key, revision))`. That query starts at the exact registered subject and source version within the journal’s namespace. It follows every downstream edge, including stores missing from the mapping, and keeps other inputs of shared results. A missing or unrelated root leaves the drill untested. Use a case-scoped journal for per-finding events when available. An unfiltered journal can report additional locations for investigation.

The mutation callback receives only fixture-declared actions that have passed their baseline checks. An unread cache remains in the coverage table. If rebuilding an application requires a cache outside the checked action list, the application can refuse the entire change. Before allowing a whole-key deletion, Setrya requires a direct read showing that the target fact is present and the retained fact is absent.

Each application service reports its baseline separately from its deletion status. `CONFIRMED` means the expected fact, control and source revision (when specified) were observed. `INCOMPLETE` identifies a missing or mismatched observation after at least one read. `UNREAD` means the fixture supplied no direct observation. If erasure was withheld, even a confirmed baseline has a deletion status of `NOT TESTED`. The service owner can locate the missing read before retrying the case.

The `services` array records every service declared in the mapping, including a cache with no read hook. For a checked store or endpoint, its evidence links identify within-bundle JSON pointers for the baseline, immediate, and final reads. The `lineage` field carries the source version roots and derivation edges supplied by the application. For a provider, the exhibit and follow-up date refer to the owner’s configuration evidence. The separate `invocation` record links a unique invocation key to a versioned application input and matching outbound journal events. To join these records, use a distinct key per SDK call and include the contributing input as the `source` key and a `key@revision` entry in `sources`. `SDK_RETURNED` describes an observed client return for that call. An attempted and returned SDK event must describe the same call. Reused request keys, a framework start callback, and ambiguous input versions leave the invocation unobserved. A configuration exhibit alone also leaves it unobserved. Provider-side deletion remains an open item. An unlisted system can remain invisible if the application records no lineage or journal event for it. Keep opaque subject, service, copy, and exhibit references suitable for your case audience.

For a source approved for retention, the CLI records the owner's exhibit and review date without opening a fixture. This bundle contains a v3 discovery receipt and no nested application drill. The technical result is `NOT TESTED`, and the case remains `OPEN_ITEMS`.

## Send the result back

```sh
setrya discovery replay run --finding finding.json --mapping mapping.json
setrya discovery export run --finding finding.json --mapping mapping.json --output case-update.json
```

New case results use the shared v3 receipt schema with person scope and the experimental discovery profile. An input from v0.18 replays to its original v1 discovery result.

Attach `case-update.json` to its originating case and retain the complete bundle for the reviewer. Export writes the validated case-result bytes. Reviewers can independently supply the original finding and mapping on replay. Both pin the expected case and scope. To authenticate the issuer, verify the detached Cosign signature on `case-result.json` with an independently trusted key or exact workflow identity. A signature on the case result also binds the nested erasure receipt and its evidence digest. A signature verifies who issued the recorded observations. Direct checks still depend on the trusted application fixture.

```sh
setrya verify --receipt run/case-result.json --bundle run/case.sig --key trusted-reviewer.pub
setrya discovery replay run --finding finding.json --mapping mapping.json --case-review
```

Engineering exits are 0 for PASS, 1 for FAIL, 2 for errors, and 3 for insufficient evidence. Case review also returns 3 when engineering passes and a case item remains open. The case ID, Macie finding ID, application revision and environment are included in both the JSON record and the review command's output.
