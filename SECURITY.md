# Security policy

Report suspected vulnerabilities privately using [GitHub's vulnerability reporting form](https://github.com/davidahma/setrya/security/advisories/new). Include the affected Setrya version, what you observed, and a minimal synthetic reproduction. Keep credentials, customer queries, passages, and private system details out of public issues.

The current 0.9 release series receives fixes. Setrya loads adapter code from the local Python environment, so review the drill and its dependencies before executing it. Use the installed `setrya replay` command or `python -I -m setrya replay` to inspect downloaded evidence. The installed console adds the working checkout to Python’s import path only for live adapter checks.

## Tenant offboarding evidence

Use the installed `setrya offboarding gate` or `setrya offboarding verify-receipt` command to review portable offboarding evidence. Treat request digests and job IDs in the original case ticket as the reference. Keep customer receipts, provider documents and backup schedules in the authorized case system. Only the bundled synthetic example is intended for public CI artifacts.

Setrya checks the files, request binding, and internal consistency. Someone who controls the observer or job log could fabricate the recorded events. Review the identity and permissions of each evidence producer before relying on its readings. A passing technical result covers the declared reads and checkpoints. Provider statements identify their issuer, and scheduled retention stays an open item. [Receipt guide](docs/offboarding.md#review-one-deletion-request)

## Verify a downloaded offboarding tool

The portable CLI runs with Python 3.11 or newer. Before executing it, check that GitHub attested the exact file on Setrya’s protected main branch. Obtain the expected full commit ID from your approved release record. The same command accepts the downloadable synthetic offboarding ZIP and reference report. The workflow runs the packaged code and scans the generated evidence before uploading the files. [Portable build workflow](.github/workflows/offboarding-portable.yml)

```sh
gh attestation verify setrya-offboarding.pyz --repo davidahma/setrya \
  --signer-workflow davidahma/setrya/.github/workflows/offboarding-portable.yml \
  --source-ref refs/heads/main --source-digest "$EXPECTED_COMMIT" --deny-self-hosted-runners
python3 -I setrya-offboarding.pyz --help
```

The OpenDesk trial uses synthetic data and digest-pinned images. Its guide documents the isolation, the scope of each read and the image audit. [OpenDesk trial](setrya/examples/opendesk/README.md)
