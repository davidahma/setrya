# Review an erasure receipt

A v3 receipt identifies its scope (`person` or `tenant`), run, technical verdict, case review status, evidence and open items. The [published JSON Schema](../setrya/erasure/receipt-v3.schema.json) specifies the fields for each scope. Experimental discovery adds `profile: "discovery"` and records which approved services were inspected. A discovery gap can hold a case at `NOT TESTED` even when the nested application drill passed.

Keep the whole directory. Run `setrya replay DIRECTORY` to recompute the decision offline or add `--case-review` to require closure of open items. The public Python reviewer accepts the same evidence.

```python
from setrya.receipts import exit_code, replay, summary

receipt = replay("tenant-run/receipt", expected_request=ticket_digest)
print(summary(receipt))
code = exit_code(receipt, case_review=True)
```

Replay establishes what the captured reads and exhibits support. Authenticate those bytes with `setrya verify --receipt FILE --bundle SIGNATURE --key independently-trusted.pub` for a local Cosign signature, or supply a trusted Sigstore root, exact identity and issuer for a keyless bundle. A signed receipt still depends on the app's observer and the scope it tested.

## Historical v1 tenant receipts

A receipt records the evidence for one previously issued deletion request. The [JSON Schema](../setrya/offboarding/receipt-v1.schema.json) defines the wire format. Setrya and Axiom v0.3.2 use the same version. The field names, types, status values and meanings are frozen. A future incompatible format will have a different version and its own schema. Setrya will continue to accept v1 in `verify-receipt`.

The policy specifies the read scopes. The request, policy, evidence and exhibits are bound by SHA-256 over their exact bytes. A schema check validates the shape. Run `verify-receipt` with the request digest from the ticket to recompute the decision, validate the times and verify the exhibit hashes. Keep `OPEN_ITEMS` visible until the case owner resolves them. [Evidence and review commands](offboarding.md)

## Sign the receipt

Install Cosign 3 on the signer's machine. This command authenticates via OIDC and uses the public Sigstore infrastructure. The signer's identity and artifact digest enter the public transparency log. Keep customer contents and exhibits in the case system. Run this after issuing and checking the receipt.

```sh
setrya offboarding sign-receipt --receipt receipt.json --bundle receipt.sigstore.json
```

## Verify offline

Provision the trusted root on a connected, trusted machine using Cosign's TUF client. Deliver the root to the reviewer through your own trust channel. Keep the expected certificate identity and issuer in your review policy.

```sh
TUF_ROOT="$PWD/sigstore-cache" cosign initialize
cp sigstore-cache/tuf-repo-cdn.sigstore.dev/targets/trusted_root.json trusted_root.json
```

On the reviewing machine, use the exact receipt bytes, signature bundle and independently provisioned root. For the release sample, the identity is the Setrya workflow on its main branch. The sample signature file covers `receipt/receipt.json` in `offboarding-example.zip` from the same run.

```sh
setrya offboarding verify-signature --receipt receipt.json --bundle receipt.sigstore.json \
  --trusted-root trusted_root.json \
  --identity 'https://github.com/davidahma/setrya/.github/workflows/offboarding-portable.yml@refs/heads/main' \
  --issuer 'https://token.actions.githubusercontent.com'
```

The receipt can be up to 1 MiB. The signature bundle and trusted root must be local regular files of up to 32 MiB each. Verification reads a private snapshot of each file. Before invoking Cosign, both signature commands validate the frozen v1 schema, check timestamp order and unique identifiers, and verify that each probe’s reasons support its verdict. They recompute the technical verdict and review status from the probe and exhibit rows. For example, a missing read can leave a probe inconclusive. A confirmed read of the departed tenant’s data after offboarding records a failure. Exit 0 from verification authenticates the signer and bytes. Verify the underlying deletion evidence separately. Changing the receipt, including whitespace, invalidates the signature. Verification requires Cosign 3, Python 3.11+ and the local files. [Sigstore blob signing](https://docs.sigstore.dev/cosign/signing/signing_with_blobs/) · [Sigstore verification](https://docs.sigstore.dev/cosign/verifying/verify/)
