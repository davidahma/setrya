# Tenant offboarding

A customer leaves. Your deletion job removes its support ticket and the summary your assistant uses. Both reads come back empty. Then a worker finishes summarizing the ticket it read before deletion. Can a support agent retrieve the customer’s data again?

Setrya answers two questions. An engineer can run a **CI gate** against a build and see which reads still find tenant data. A case owner can issue a **deletion request** before dispatch and produce a **receipt** afterward. The receipt evaluates the reads for that request and records outstanding provider or backup items.

## Try it in a disposable app

Install the core SDK in a Python 3.11+ virtual environment. The example uses SQLite, a local HTTP endpoint, and fictional records.

```sh
python -m pip install 'setrya @ https://github.com/davidahma/setrya/releases/download/v0.10.0/setrya-0.10.0.tar.gz'
setrya offboarding demo --output offboarding-run
```

Choose a new directory. The demo checks the departing tenant and a retained tenant before deletion, after deletion, and after the held summary job finishes. A successful demo exits 0 when it reproduces all the expected cases.

| Evidence in the output directory | Expected result | What it shows |
| --- | --- | --- |
| `broken.json` | Gate FAIL | The delayed write restores a summary the support agent can read. |
| `fixed.json` | Gate PASS | The guarded worker discards its stale write. The declared reads find the retained tenant and no record for the departing tenant. |
| `inconclusive.json` | Gate NOT TESTED | A missing baseline prevents the observer from establishing what the agent could see. |
| `receipt/receipt.json` | Technical PASS, review OPEN_ITEMS | In a separately issued staging case, the reads passed, a provider reported deletion, and the backup schedule still needs review. |

Each CI case has its own `*-decision.json`. To replay the regression after the app exits, supply the original policy and observations. This example exits **1** and identifies the failed reads.

```sh
setrya offboarding gate --policy offboarding-run/policy.json \
  --observations offboarding-run/broken.json --expect-run broken
```

## Drive the deletion race

For a new integration, have Setrya capture the pending worker, delete the tenant, read as the declared caller, and release the delayed work. The retained tenant provides a control. The saved schedule can be replayed on a machine without the application.

```sh
setrya offboarding drill --schedule all --output deletion-run
setrya replay deletion-run
```

Use `setrya scenario revoke` to exercise a change to one caller’s access. Supply your adapter to run either case against your own application. [Adapter and schedule examples](scenarios.md)

## Add the CI gate to your own test

1. **Declare the reads.** Commit a version 1 JSON policy with a target and a list of probes. Each probe has an `id`, a `scope` identifying the lookup and caller, and a `checkpoint` of `after_offboarding` or `after_worker`. Declare separate probes when you need both checkpoints.
2. **Collect observations in the application.** Seed markers for the departing and retained tenants. Confirm both are readable. Run the app’s offboarding job, wait for completion, and repeat the same reads. Record each result as `present`, `absent`, `unavailable`, or `not_tested`. An HTTP error or a missing tenant context makes the read unavailable. For worker checks, observe the same job capturing its input before deletion and finishing afterward.
3. **Bind the run to the build.** Supply the CI run identifier and the revision of the app you tested. Keep the policy and verifier under review. The gate evaluates evidence from your observer, which owns the credentials, job hooks, and live reads. Use the demo’s `policy.json` and `fixed.json` as complete examples of the input format. [Reference observer](../setrya/examples/offboarding/)

```sh
setrya offboarding gate --policy ci/offboarding-policy.json \
  --observations "$RUN_DIR/observations.json" --output "$RUN_DIR/decision.json" \
  --expect-run "$TEST_RUN_ID" --expect-revision "$TESTED_REVISION"
```

Use a fresh output path for each CI attempt. Save the policy, observations, and decision together on exits 0, 1, and 3. A confirmed residual takes precedence over a gap elsewhere. Exit 2 indicates invalid arguments, input, or output. An existing decision file at that path can belong to an earlier attempt.

From Python, `setrya.offboarding.gate.evaluate(policy, observations, expect_run=run_id, expect_revision=revision)` accepts decoded JSON objects and returns a decision dictionary. Read `decision["verdict"]` and `decision["probes"]` in your test. The file-based CLI also records SHA-256 digests of the exact policy and observation bytes.

## Review one deletion request

**Before dispatch**, issue a fresh request. The manifest binds the target and policy to a run, revision, context, and required exhibits. Keep the printed request digest in the original case ticket before the deletion job starts.

```sh
setrya offboarding request --context customer --policy policy.json \
  --run-id "$RUN_ID" --revision "$APP_REVISION" \
  --statement provider --schedule backups --output request.json
```

Associate the request ID with the tenant in your case system. Have the deletion job check that association and persist the request digest as it commits the change. The demo does this in the same database transaction as the deletion. Its observer supplies a version 1 envelope containing `job` and `observations`. The job carries `id`, `request_sha256`, `started_at`, and `observed_at` in UTC at second precision. The nested observations use the CI gate format. An ordinary CI decision lacks this request and job binding.

**After the job**, collect the files for this request. For each external item, supply a descriptor and the original document. The descriptor binds the request and document digests to an issuer, item ID, and kind. A `provider_statement` records what its issuer reported. A `retention_schedule` records an expiry date. A scheduled item stays open when the date passes. The demo’s `receipt/` directory contains examples of both.

```sh
setrya offboarding receipt --request request.json --expect-request "$TICKET_DIGEST" \
  --expect-job "$DISPATCHED_JOB_ID" --policy policy.json --evidence evidence.json \
  --record provider.json provider.txt --record backups.json backups.txt --output receipt.json
```

**Replay the receipt** with the original files and the digest from the case ticket. For the local demonstration, `request.sha256` stands in for that independently kept digest. Run this from the directory where you started the demo.

```sh
cd offboarding-run/receipt
setrya offboarding verify-receipt --request request.json --expect-request "$(cat request.sha256)" \
  --policy policy.json --evidence evidence.json --receipt receipt.json \
  --record provider.json provider.txt --record backups.json backups.txt
```

The example prints `Receipt verified.`, `Technical verdict PASS`, and `Review status OPEN_ITEMS`. Exit **3** means the reviewer reproduced the outstanding backup item. For a real case, pin the expected job ID from the dispatch log when available. Replay verifies the evidence and attachments supplied for the request. The reviewer judges the observer’s authority, the scope of each read, and the provider’s statement. [Handling private evidence](../SECURITY.md)

| Exit | Offboarding gate | Receipt creation or replay |
| --- | --- | --- |
| 0 | PASS | READY_FOR_REVIEW |
| 1 | FAIL | RESIDUAL_FOUND |
| 2 | ERROR | Invalid input, output, or receipt mismatch |
| 3 | NOT TESTED | OPEN_ITEMS |

## Review evidence without installing a package

Download `setrya-offboarding.pyz` from the Setrya release. Verify its build provenance, then run it with Python 3.11 or newer. The arguments after the filename are the same ones you would pass after `setrya offboarding`. For example, validate an archived reference report offline with `python3 -I setrya-offboarding.pyz validate demo-report.json`. [Release files](https://github.com/davidahma/setrya/releases/tag/v0.10.0) · [Verify the build](../SECURITY.md#verify-a-downloaded-offboarding-tool)

A reviewer can also sign the exact bytes of a deletion receipt with an authorized OpenSSH key. The signature namespace stays `axiom-receipt-v1` so existing signatures and allowed-signers policies continue to work. Obtain the allowed-signers file independently. The signature identifies the signing key. The receipt and original evidence supply the findings.

```sh
ssh-keygen -Y sign -f "$SIGNING_KEY" -n axiom-receipt-v1 receipt.json
ssh-keygen -Y verify -f "$TRUSTED_ALLOWED_SIGNERS" -I "$AUTHORIZED_SIGNER" \
  -n axiom-receipt-v1 -s receipt.json.sig < receipt.json
```

## Migrate from Axiom

Use the Setrya repository, SDK, CLI, releases and issue tracker for tenant offboarding. Axiom v0.3.2’s files retain their original formats and digest bindings. In Setrya 0.8 the process exit codes changed. Update any shell script that treats 2 as an inconclusive gate or open receipt. Those outcomes now exit 3. Historical gate JSON continues to spell the incomplete verdict `INCONCLUSIVE`. Keep the original policy, request digest, job evidence and exhibits when replaying a case.

| Axiom workflow | Setrya command |
| --- | --- |
| Staging CI gate | `setrya offboarding gate` with the original arguments |
| Request issuance and deletion receipts | `setrya offboarding request`, `receipt`, and `verify-receipt` with the original arguments |
| Original three-case worker demo | `setrya offboarding reference-demo --output demo-report.json` |
| Validate a saved worker demo | `setrya offboarding validate demo-report.json` |
| SQLite source and answer-cache observer | `setrya offboarding sqlite-policy` and `setrya offboarding observe-sqlite` |
| Run the pinned OpenDesk application | `setrya offboarding opendesk --output trial` with Docker Compose |
| Validate or replay raw OpenDesk evidence | `setrya offboarding opendesk --verify-report opendesk.json` or `--from-report opendesk.json --output replay` |
| Single-file CLI, verified builds, and downloadable reports | [Setrya release assets](https://github.com/davidahma/setrya/releases/tag/v0.10.0) |

The reference demo records the worker and 18 observations per case across two tenants, three checkpoints and three read paths. Validation checks that recorded times follow the three checkpoints and recomputes the verdicts. A valid report can describe a failing application. Use the SQLite observer to exercise source and cache checks or produce observations bound to an issued staging request. [SQLite observer](../setrya/examples/offboarding/sqlite/README.md)

In the pinned OpenDesk trial, the source and summary storage reads passed after deletion. The support agent’s route could no longer answer for the departing workspace, and both summary jobs had finished before deletion. The CLI reports NOT TESTED and the saved v1 gate decision records INCONCLUSIVE. You can replay that evidence without Docker. [OpenDesk setup and evidence](../setrya/examples/opendesk/README.md)

## Read-only observation and receipt signatures

Give the observer a separate identity with read access only to the columns named by the policy. In the OpenDesk trial, `axiom_observer` can select message IDs and ticket IDs and summaries. The workspace owner performs the deletion. For SQLite integrations, open the application database with a read-only OS identity and use this SDK helper before and after the application runs the job. Use the two resulting pairs for the probe's `before` and `after` fields.

```python
from setrya.offboarding.observer import open_reader, read_pair

with open_reader(database_path) as reader:
    observed = read_pair(reader, "SELECT 1 FROM tickets WHERE tenant = ?", (departing, retained))
```

Each subject and control pair uses one SQLite snapshot. A writer committing between the two reads becomes visible on the next pair. The connection uses SQLite's read-only mode and rejects writes and attached databases. A failed read produces `unavailable`. The observer can run in a process with no application writer credentials. [Frozen receipt schema, Sigstore signing and offline verification](receipt-format.md)
