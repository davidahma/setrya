# Deletion Receipt v1 exchange draft

Use this exchange to send one deletion result to a reviewer who has no access to the application, database or journal. The format binds a small signed manifest to the original v3 receipt and evidence. Setrya replays the original evidence using the existing verdict rules. [JSON Schema](../setrya/casework/receipt-exchange-v1.schema.json) · [Try a completed PostgreSQL ticket](receipt-handoff.md)

The exchange format, `setrya.deletion-receipt-exchange.v1`, is a draft. Preserve the Setrya version used to issue it. Historical tenant v1 receipts and the underlying v3 receipt format retain their existing identifiers and verification rules. The draft will be frozen after an outside issuer and a separate reviewer complete the handoff independently. [Supported receipt formats](receipt-format.md)

## What the issuer sends

The exchange directory contains four regular files.

| File | Purpose |
| --- | --- |
| `exchange.json` | Versioned manifest. It records the system and version supplied by the issuer, declared stores and route, observation timing, the replayed verdict and open items, and SHA-256 digests of the two v3 files. Split HTTP drills also carry the deployment and the behavior and deployment file fingerprints from the receipt. |
| `exchange.sig` | Cosign detached signature or Sigstore bundle for the exact manifest bytes. The `signature_type` selects the representation. |
| `erasure-receipt.json` | The exact original v3 receipt. |
| `observation-evidence.json` or `erasure-evidence.json` | The exact original companion, chosen by the evidence kind. |

| Evidence kind | Required claims | Excluded claims |
| --- | --- | --- |
| `read-only-observation` | Completed ticket ID, exact request-export fingerprint, completion times of both recorded reads, declared stores, and `observation-evidence.json`. | Synthetic drill duration, endpoint route, live HTTP deployment, and drill evidence. |
| `synthetic-drill` | Recorded end time, duration, declared stores and route, and `erasure-evidence.json`. Split HTTP drills carry the observed deployment projection. | Completed ticket, first read-only observation time, and read-only observation evidence. |
| `vendor-attestation` | Vendor conformance target, recorded versions and per-surface results, recorded execution metadata when available, and `observation-evidence.json`. | Completed ticket, person scope, HTTP deployment, and drill evidence. |
| `provider-readback` | Declared API target and version, scope digest, baseline and follow-up time, and per-surface results with provider internals left open. | Credentials, raw record IDs, provider-internal deletion, and a bound completed ticket. |

For a read-only observation, the second journal snapshot must follow the first completed observation whenever that snapshot is available. A retained control shows that the observer could see the journalled store. A synthetic drill can also record an external assertion as an `attested` open item with an owner and follow-up date. That open item remains an obligation on the original drill.

The system label, version and declared surfaces describe the issuer's scope. The original evidence shows which reads occurred. The signature authenticates the exchange issuer against the verifier's own trust policy. The observer still determines what could be seen. Use the original bundle only with reviewers authorized to see potentially linkable subject, ticket and store identifiers. Synthetic evidence can also contain configured answer excerpts. Public examples use fictional data.

## Attach a vendor result to a governance ticket

Pack an existing conformance result. Setrya identifies it from the v3 receipt and replays its original observations before signing the exchange. The exchange keeps surface outcomes such as `FAIL · history` and coverage gaps such as `NOT TESTED · backups` visible.

```sh
setrya receipt pack ./vendor-result --output ./vendor-exchange \
  --system mem0 --system-version 2.2.1 --key issuer.key
setrya receipt verify-exchange ./vendor-exchange --key independently-trusted.pub
```

The exchange signer vouches for the exact attached receipt and evidence. The recorded operator is a claim from the fixture. To authenticate the original conformance signature and review a paired rerun, keep the original conformance bundle and use `setrya conformance review` with the appropriate issuer trust. A vendor exchange carries an engineering result for the tested version and surfaces. A governance ticket can reference its digest as supporting evidence. A completed-deletion ticket or app pin requires evidence from a case that actually binds it. [Vendor conformance](vendor-conformance.md) · [Governance handoff example](governance-handoff.md)

The same pack command classifies a provider API result as `provider-readback`. The CLI replays it without contacting the API. Earlier exchanges that carried an API readback under `vendor-attestation` remain verifiable under their signed bytes. [Provider readback](provider-readback.md)

## How an independent reviewer verifies it

Provision the expected issuer's public key separately. For keyless signing, use the exact issuer and certificate identity from your trust policy and a locally provisioned Sigstore root. The certificate identifies the keyless signer. For a detached signature, your trust record identifies the owner of the pinned public key. Trust learned from files inside the exchange cannot authenticate the issuer.

```sh
setrya receipt verify-exchange received/ --key independently-trusted.pub \
  --expect-ticket "$TICKET_ID" --expect-frame "$COMPLETED_FRAME_SHA256"
```

Get the expected ticket ID and frame digest from the originating ticket system. These checks bind the exchange to the case under review. No database, app fixture or network is needed after trust and tools have been provisioned. For keyless verification, substitute `--identity`, `--issuer` and `--trusted-root` for `--key`. For the GitHub release examples, the trusted identity is `https://github.com/davidahma/setrya/.github/workflows/erasure.yml@refs/heads/main` and the issuer is `https://token.actions.githubusercontent.com`. Provision the Sigstore root as described in the [receipt verification guide](northline.md).

For a split HTTP drill, `--expect-deployment record.json` pins the environment and build to the reviewer's independently sourced deployment record. `--expect-app-commit` pins a clean checkout and committed fixture files for a local starter. The two options check different observations. [HTTP case configuration](http-cases.md)

An independent implementation verifies the signature over the exact bytes of `exchange.json`, validates the schema, and checks the SHA-256 of the exact companion bytes. Python also replays the original v3 evidence and compares the resulting state, surfaces, timing, ticket binding, and open items to the manifest. The browser verifies the signature, digests and receipt claims. It displays the issuer's reported result and marks verdict replay as unchecked. JSON Pointer fragments in open items address fields in the original receipt.

The standalone browser verifier accepts detached and keyless signatures and supports the independent ticket, export, deployment and app pins. [Verify without installing tools](assessment.md#verify-without-installing-tools)

| Example directory | Replayed result | Exit |
| --- | --- | --- |
| `passed` | `PASS · READY_FOR_REVIEW` | 0 |
| `residual` | `FAIL · RESIDUAL_FOUND` | 1 |
| `unavailable` | `NOT TESTED · OPEN_ITEMS` | 3 |
| `open-items` | `PASS · OPEN_ITEMS` | 0, or 3 with `--case-review` |

[Download the four signed examples](https://github.com/davidahma/setrya/releases/download/v1.8.0/deletion-receipt-exchange.zip). The first three capture native PostgreSQL observations of synthetic completed tickets. The fourth opens the synthetic workspace issue with its provider follow-up unchanged. A malformed bundle, unverifiable signature or case pin mismatch exits 2. A valid signature can accompany any engineering verdict.

## Verification error codes

A rejected exchange reports one stable code. The CLI adds it in brackets and exits 2. A valid exchange still reports its own engineering exit code. For bundles with multiple faults, repair the reported fault and verify again.

| Code | Meaning |
| --- | --- |
| `UNEXPECTED_FILES` | Supply the four expected regular files with their original names. |
| `DUPLICATE_JSON_KEY` | A JSON file repeats a key within one object. |
| `INVALID_MANIFEST` | The exchange envelope or its required fields are invalid. |
| `WRONG_SIGNER` | The signature or its signer could not be authenticated with the supplied trust. |
| `EVIDENCE_KIND_MISMATCH` | The original receipt or evidence belongs to a different kind than the exchange declares. |
| `EVIDENCE_DIGEST_MISMATCH` | A companion differs from the signed manifest or the evidence differs from the original receipt's digest. |
| `SNAPSHOT_ORDER` | The second journal snapshot began before the first observation completed. |
| `VERDICT_MISMATCH` | The claimed verdict or review status differs from the original receipt or from replayed evidence. |
| `CLAIMS_MISMATCH` | The original evidence cannot be interpreted or the remaining claims differ. |
| `TICKET_ID_MISMATCH` | The completed ticket differs from the expected ticket. |
| `EXPORT_DIGEST_MISMATCH` | The completed request export differs from the expected export. |
| `DEPLOYMENT_MISMATCH` | The receipt does not verify the independently supplied deployment. |
| `APP_COMMIT_MISMATCH` | The receipt does not bind the expected clean application checkout. |
| `PROVENANCE_MISMATCH` | The receipt differs from the supplied full provenance record. |

Qualification exercises the same signed inputs against Python and JavaScript. In the case where the signed manifest and original receipt repeat the same incorrect verdict, Python detects the error by replaying the evidence. The browser identifies the verdict as issuer-reported and marks replay as unchecked.
