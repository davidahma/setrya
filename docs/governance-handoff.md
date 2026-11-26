# Attach deletion evidence to a governance ticket

A support team uses `ERASURE-07` to track its deletion check and `GOV-204` to track review of one release. The fictional workspace drill checks whether a background writer can restore a deleted note and whether both teammates can still read their launch date. The guarded run produces `PASS · OPEN_ITEMS`. The application checks passed and the platform team still owns a provider follow-up.

## Issue the evidence

Run these commands from the Setrya repository root with Bash, Python 3.11 or later and Cosign 3. The example uses the installed Setrya package, a disposable local application fixture and a temporary signing key.

```sh
example="$PWD/docs/examples/governance-handoff/handoff.py"
scratch=$(mktemp -d)
python3 -m venv "$scratch/env"
python="$scratch/env/bin/python"
setrya="$scratch/env/bin/setrya"
"$python" -m pip install setrya
mkdir "$scratch/issuer" "$scratch/reviewer"

(
  cd "$scratch/issuer" || exit 1
  COSIGN_PASSWORD="" cosign generate-key-pair --output-key-prefix issuer
  COSIGN_PASSWORD="" "$python" -I "$example" issue exchange --key issuer.key
)
```

The [example](examples/governance-handoff/handoff.py) supplies the existing `controls` field with `Support governance / ERASURE-07`. Replay checks the reference against the original evidence. The signature binds the exact receipt and evidence digests. Use a trusted signing key and share the exchange through your approved channel for a real review. [Control references](customer-casework.md#attach-a-contract-or-control-reference)

## Verify and attach to the ticket

Give the reviewer the four-file exchange and provision the trusted issuer public key separately. The copy commands simulate both deliveries for this local exercise. A reviewer can verify the saved exchange with the public key after the application stops.

```sh
cp -R "$scratch/issuer/exchange" "$scratch/reviewer/exchange"
cp "$scratch/issuer/issuer.pub" "$scratch/reviewer/trusted-issuer.pub"

(
  cd "$scratch/reviewer" || exit 1
  "$python" -I "$example" review exchange --key trusted-issuer.pub > ticket.json || exit 1
  "$setrya" receipt summary exchange --audience reviewer --format md > approval.md || exit 1
  cat ticket.json
)
```

Attach `exchange/`, `ticket.json`, and `approval.md` to the governance ticket. `ticket.json` names `GOV-204`, the control reference, the exchange attachment, and the SHA-256 of the exact `erasure-receipt.json` bytes. It records the verifying key's SHA-256 fingerprint, the reviewer's UTC verification time, the technical and review states, and the provider item with its owner, follow-up date, and pointer into the signed receipt. Use the signed exchange when reviewing a case. A later verification will have its own timestamp.

The reviewer authenticates the signer against the supplied key and replays the recorded evidence. The run remains the issuer's execution. `GOV-204` is the governance review location. For a completed deletion request, an originating ticket and request-export digest can also be checked using independent `--expect-ticket` and `--expect-frame` pins. [Completed-ticket handoff](receipt-handoff.md)

Treat a successfully generated attachment as evidence ready to review. Use the stricter case-review exit code when a workflow must wait for every item to close. Expect exit 3 for this example because the provider item remains open.

```sh
"$setrya" receipt verify-exchange "$scratch/reviewer/exchange" \
  --key "$scratch/reviewer/trusted-issuer.pub" --case-review
```

Use the observed stores, answers and writer as supporting evidence for your governance decision. Route the remaining item to its recorded owner. For a portable assessment artifact, export the same signed exchange with your Assessment Plan and control mapping. [Receipt exchange](receipt-exchange.md) · [OSCAL export](oscal.md)

## Attach a vendor result

Review the original vendor bundle under its own trusted signer policy first. An evidence issuer can then package the result for a ticket, and the ticket reviewer can open that exchange under the issuer's trusted key. Extract `vendor-conformance.zip` into `"$scratch/vendor"` for this example. The Mem0 history check reports `FAIL`. [Download the signed fixture results](https://github.com/davidahma/setrya/releases/download/v1.13.0/vendor-conformance.zip)

```sh
"$setrya" conformance review "$scratch/vendor/mem0" --key "$scratch/vendor/ci-fixture.pub" \
  --expect-operator setrya-ci
# Exit 1 is the observed residual. Confirm the report before packing it.
COSIGN_PASSWORD="" "$setrya" receipt pack "$scratch/vendor/mem0" \
  --output "$scratch/issuer/vendor-exchange" \
  --system mem0 --system-version 2.2.1 --key "$scratch/issuer/issuer.key"
cp -R "$scratch/issuer/vendor-exchange" "$scratch/reviewer/vendor-exchange"
"$python" -I "$example" review "$scratch/reviewer/vendor-exchange" \
  --key "$scratch/reviewer/trusted-issuer.pub" \
  --ticket GOV-205 --framework "Support governance" --control ERASURE-08 > "$scratch/reviewer/vendor-ticket.json"
```

The ticket labels the evidence as a vendor attestation and includes its target, versions, operator if recorded, and per-surface results. Its signer identifies the exchange issuer. The control mapping comes from the ticket reviewer. Preserve the original bundle and signer policy if the reviewer also needs to verify who signed the source result. [Vendor report scope](vendor-conformance.md) · [Receipt exchange](receipt-exchange.md)

## Attach a provider API result

After checking the original source signature, [pack the provider result](provider-readback.md#hand-off-the-api-observation). Verify the exchange in the reviewer directory and supply the receiving ticket and control reference.

```sh
"$python" -I "$example" review provider-exchange --key trusted-issuer.pub \
  --ticket GOV-206 --framework "Support governance" --control ERASURE-09 > provider-ticket.json
```

The ticket records the provider API outcome, observation window, scope digest and open items. The supplied ticket ID is the destination of the evidence. Use a completed-deletion receipt to verify the origin of a real deletion request. [Provider readback](provider-readback.md) · [Completed-ticket handoff](receipt-handoff.md)
