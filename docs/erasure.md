# Verify one test subject’s deletion

Setrya follows one tenant-scoped subject through a disposable application. The journal records where your hooks saw a copy. Direct adapters also search every system in the inventory for subject-tagged records, including records the hooks missed. Untagged data outside those boundaries requires a separate discovery process.

## Run and review

```sh
python -m pip install 'setrya[erasure]'
setrya demo erasure --output alice
setrya replay alice/guarded
setrya init erasure --output subject-test
```

The demo writes three independent evidence directories. Open a receipt’s `copies` array to see its location, handling labels, owner, and a pointer to the corresponding observations. Follow those pointers in the sibling evidence file. The `reads` section records both authenticated callers at each stage. The signed GitHub release includes all three runs. [Starter guide](erasure-starter.md) · [Adapters and isolation](erasure-adapters.md) · [Instrumenting an application](erasure-hooks.md)

The worker captures the old input and stops at its pre-write checkpoint. The app has to serve the subject and a retained control to both test callers before deletion. Setrya discovers copies, confirms the same attempt is held, calls the declared deletion adapters, and observes the stores and both callers. It releases the worker, waits for completion and relevant refreshes, reopens the serving path, and checks every direct store again. The application supplies authenticated reads and owns the worker and refresh barriers.

| Per-copy status | Evidence |
| --- | --- |
| `verified gone` | That key was present in direct discovery, and checks found it absent immediately after deletion and after the held worker drained. |
| `still present` | A direct read found the key after deletion or after the worker resumed. The receipt also identifies either caller who saw the subject return. |
| `attested` | The inventory owner supplied an exhibit ID for the copy. This is the owner’s assertion. |
| `retained until` | The owner supplied an exhibit and a retention date still in force at observation time. A later run can test expiry. |
| `unknown` | Setrya lacks the observations needed to establish this copy’s disposition. |

An observed residual, lost control, or handling violation fails the drill (exit 1). Adapter errors exit 2 unless a failure is already established. Missing observations or remaining external claims return `NOT TESTED` (3). A completed drill with direct evidence for all observed copies and systems returns `PASS` (0).

## Inventory and external evidence

`inventory.toml` maps a physical deletion boundary to a named adapter. One system can contain many subject keys. Supply the same name to your hooks and use trusted synthetic caller aliases. Every approved classification is an exact customer-defined string.

```toml
version = 1
[[systems]]
name = "chat-memory"
owner = "assistant-team"
role = "memory"
approved_labels = ["restricted"]
retention_days = 7
adapter = "memory"
```

Setrya flags journal entries from undeclared systems and entries whose classification the owner did not approve. A retention period in the inventory describes policy. To report an actual retained copy, pass `Disposition(store, key, owner, evidence, subject_ref=subject.ref, until="2027-01-31")` in `run_erasure(..., dispositions=[...])`. Keep the source exhibit in your case system. A disposition without an expiry records an attestation. The retained date and issuer remain visible on replay.

For Bedrock or the Anthropic API, record the model, AWS region where applicable, source references and configured invocation logging and prompt caching settings. The provider hook records a keyed fingerprint when configured. The hook does not export raw prompts. Use the inventory role `provider` and adapter `none`. Configuration evidence and a successful inference cannot establish what remains in a provider’s storage.

## Sign and verify

A drill writes an unsigned receipt and the observations it covers. Cosign 3 can sign the receipt on a laptop using a local key pair. Keep the private key out of the evidence directory. Distribute the trusted public key independently.

```sh
cosign generate-key-pair
setrya erasure sign-receipt --key cosign.key \
  --receipt alice/guarded/erasure-receipt.json --bundle alice/guarded/receipt.sig
setrya erasure verify-signature --key cosign.pub \
  --receipt alice/guarded/erasure-receipt.json --bundle alice/guarded/receipt.sig
setrya replay alice/guarded
```

The local key flow creates a detached signature. CI uses a GitHub Actions identity and a Sigstore transparency-log bundle. Download `subject-erasure.zip` from the release, extract it, and use a Sigstore trusted root obtained independently. From the extracted directory run:

```sh
setrya erasure verify-signature --receipt guarded/erasure-receipt.json \
  --bundle guarded/receipt.sigstore.json --trusted-root "$TRUSTED_ROOT" \
  --issuer https://token.actions.githubusercontent.com \
  --identity https://github.com/davidahma/setrya/.github/workflows/erasure.yml@refs/heads/main
setrya replay guarded
```

Both signature commands first recompute the receipt from the supplied observations. Verification authenticates the exact receipt bytes and the SHA-256 of its evidence. Replay works offline with the core package. Trust in the observations also depends on who ran the fixture and which data their credentials could inspect.

## Receipt v2 and the onboarding target

The v2 format is specific to per-subject erasure. The existing tenant offboarding receipt uses its own v1 schema and replay command. The v2 receipt and evidence travel together. The format name, required fields, status meanings, and evidence links are fixed for v2. A change that would reinterpret existing evidence requires a new format version. [Receipt schema](../setrya/erasure/receipt-v2.schema.json)

`time_to_first_verdict_ms` measures the live drill from startup through the first computed decision. `unknown_copies` counts distinct observed store/key pairs with unknown disposition. Unobserved systems and missing reads are listed under `gaps`. Installation, hook wiring, and signing take additional time. The one-hour onboarding target needs a timed session with an engineer new to Setrya. The test suite separately connects the generated starter to an independently written application and checks both metrics.
