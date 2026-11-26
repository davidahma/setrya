# Sign a deletion assessment

Use `setrya assess` to combine results from several repositories. The report records the supplied commit for each repository, the digest and gap count of each coverage check, and the verdict and evidence digest of each replayed receipt. It creates a signed JSON report and a short Markdown summary.

## Build the report

Generate scan and coverage reports with the [copy-site check](copy-coverage.md), `route-coverage --json` or `coverage-drift --json`. Use an empty list where an input is unavailable. List paths relative to the plan or use absolute paths. Use full Git commit IDs.

```json
{
  "format": "setrya.assessment-input.v1",
  "repositories": [
    {
      "name": "support",
      "commit": "2222222222222222222222222222222222222222",
      "checks": ["support-copy-review.json", "support-journal-coverage.json"],
      "receipts": ["support-receipt"]
    },
    {
      "name": "billing",
      "commit": "3333333333333333333333333333333333333333",
      "checks": ["billing-routes.json"],
      "receipts": []
    }
  ]
}
```

```sh
setrya assess build --plan assessment-plan.json --output assessment --key issuer.key
setrya assess verify assessment --key trusted-issuer.pub
```

Cosign 3 signs the exact `assessment.json` bytes. The report binds `summary.md` to those bytes by digest. For keyless signing, omit `--key` on build. The CLI verifier takes the already trusted `--identity`, `--issuer`, and `--trusted-root`. Keep the inputs separately if a reviewer needs to reproduce the assessment. Share them only with people authorized to see the underlying evidence.

Copy scans and coverage exports describe what their input files report. A map entry identifies work to test. The builder replays receipt contents without contacting the application. A clean recorded checkout or commit-bearing coverage export that differs from the plan blocks the build. Older receipts, dirty checkouts, and unpinned checks carry no checkout binding. The signature authenticates the report's issuer. The issuer's controls establish which inputs and runner it trusted.

## Compare a client release with its approved baseline

Add a `client` label to both assessment plans. This keeps the scope with the signed report and includes replayed person drills and read-only deletion observations. Supply the approved assessment digest from your review system. If the candidate should match a particular deployed build, supply a deployment record obtained independently of the candidate issuer.

```sh
setrya assess compare approved-assessment candidate-assessment \
  --client acme --approved-sha256 "$APPROVED_ASSESSMENT_SHA256" \
  --key trusted-assessor.pub --sign-key comparison.key \
  --expect-deployment approved-deployment.json --output approval
setrya assess verify-comparison approval --key trusted-comparison.pub
```

Attach `approval.md` and `customer.md` to the existing approval ticket. Keep `comparison.json` and `comparison.sig` with the attachments so reviewers can authenticate both files. The comparison records gains and losses among observed copies, queries, scans, and write-back checks. Query coverage includes the caller, route, question digest, role, and sample count. Reassigning a question from retained control to erased target loses coverage of the original control. The comparison also lists residual findings and each candidate follow-up with its owner and due date.

The comparison sends a candidate back for review when it loses coverage, reports a coverage gap or an open item, finds a residual, or changes the observed environment, origin, or HTTP behavior digest. It uses stable scope identifiers to detect individual route, copy-review and journal entries that disappear from retained reports. A changed Git release needs a clean checkout binding or the independently supplied deployment pin. A changed deployed build or certificate needs the deployment pin. The pin confirms the deployment identity. If the HTTP behavior SHA-256 changed, review the test file that produced the new digest before approving the release.

The command accepts the existing Cosign public-key or keyless trust options. The two input assessments must use the same independently supplied verification policy. Older assessments and comparison attachments still verify against their signed contents. If an assessment lacks query roles, rebuild it from the recorded receipts with the current Setrya release before using the comparison for approval.

## Verify without installing tools

Download the [offline verifier](https://github.com/davidahma/setrya/releases/download/v1.15.0/setrya-verify-offline.html) independently of the sender and open it in a browser. Select all files from one assessment or draft receipt exchange.

- For detached signatures, select the trusted Cosign P-256 PEM public key from your organization's approved channel. Confirm the displayed key fingerprint against your trust record.
- For keyless signatures, select an independently provisioned Sigstore trust root and enter the exact expected certificate identity and OIDC issuer. The page checks the signing certificate and transparency-log proof.
- For receipt exchanges, enter any independently supplied ticket, export digest, deployment record, or expected app commit you need to check.

The page runs locally and shows the outcome of each verification step. The issuer's technical verdict appears with its replay status. Use the CLI for full evidence replay and to verify signed comparison attachments. [Receipt exchange](receipt-exchange.md)
