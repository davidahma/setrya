# Export Setrya evidence as OSCAL Assessment Results

Use `setrya receipt export-oscal` to attach authenticated deletion observations to an assessment. The export uses the NIST OSCAL 1.2.0 Assessment Results format. The assessor makes the control determination. [NIST Assessment Results model](https://pages.nist.gov/OSCAL/learn/concepts/layer/assessment/assessment-results/) · [OSCAL 1.2.0 schemas](https://github.com/usnistgov/OSCAL/releases/tag/v1.2.0)

## Try the fictional governance ticket

Follow the [governance handoff](governance-handoff.md) to create the fictional exchange and provision the fixture's Cosign public key. Then create an Assessment Results file beside the verified exchange.

```sh
sample="$PWD/docs/examples/governance-handoff"
cp "$sample/oscal-assessment-plan.json" "$sample/oscal-control-mapping.json" "$scratch/reviewer/"
(
  cd "$scratch/reviewer" || exit 1
  "$setrya" receipt export-oscal exchange --key trusted-issuer.pub \
    --assessment-plan oscal-assessment-plan.json --mapping oscal-control-mapping.json \
    --ticket GOV-204 --output oscal-assessment-results.json
)
```

The sample result shows `PASS · OPEN_ITEMS`. It references the direct readings of the retained calendar and the overall synthetic erasure result. The provider follow-up remains open. The example controls are fictional, and the sample Assessment Plan contains a placeholder SSP link. Use the assessor's actual plan and SSP when exchanging evidence for a real assessment.

## Use your own Assessment Plan and mapping

1. Place the OSCAL 1.2.0 Assessment Plan JSON in the handoff directory. Use explicit whole-control IDs in its reviewed-controls selection. Setrya checks these IDs locally and respects the plan's exclusions. The assessor's OSCAL tooling resolves the plan's SSP and catalog.
2. Provide a [versioned control mapping](examples/governance-handoff/oscal-control-mapping.json). Each `supports` row binds a check to an exact framework, edition, reference and local OSCAL `control_id`. Include the applicability boundary and an HTTPS source citation. Have the appropriate auditor or CPA review real mappings before publication.
3. Use the independent issuer key or exact Sigstore identity, OIDC issuer and trusted root from your existing receipt review policy. For completed requests, preserve the reviewer's `--expect-ticket` and `--expect-frame` pins. A separate `--ticket` labels the governance destination. Use `--expect-plan-sha256` and `--expect-mapping-sha256` when the assessor has independently approved those exact files.

The exporter selects only mapped checks present in the verified result and included in the plan. A failed or incomplete run can still yield a valid export. A successful exit means the evidence file was written. Read the recorded verdict and status for the engineering outcome.

| Mapping check | Evidence referenced |
| --- | --- |
| `synthetic-erasure` | Overall replayed technical verdict from a person drill. |
| `artifact-delete`, `artifact-preserve`, `artifact-rewrite` | Recorded post-erasure reads for at least one artifact under the named rule. The export gives recorded and declared counts. |
| `endpoint-read`, `store-scan` | Post-erasure phases with recorded endpoint or scanner observations. |
| `write-back` | The recorded release and drain of the same deferred writer across erasure. |
| `read-only-observation` | Recorded direct-read outcomes for a completed deletion request. |
| `vendor-conformance` | Scoped backend or API surface results labelled as a vendor attestation. |

## Attach and verify

Attach the Assessment Results JSON with the original four-file exchange and the intended Assessment Plan. Keep their relative paths intact, and use your governing SSP and catalog where applicable. Keep the detached or keyless trust policy with the reviewer. The export references the signed evidence and carries receipt, evidence, plan and map digests, the exchange signer, verification time, recorded verdict and open items. Unsigned map entries and ticket labels come from the reviewer. A control reference already bound to the receipt is identified separately.

Verify the underlying evidence with `setrya receipt verify-exchange`. The OSCAL document is a local export. Signature verification and technical replay apply to the original exchange. Setrya makes no network requests for the Assessment Plan or mapping citations. Review the export and original files for sensitive system names, ticket references and observations before sending them through an approved channel. [Offline receipt verification](receipt-exchange.md)
