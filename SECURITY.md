# Security policy

Report suspected vulnerabilities privately using [GitHub's vulnerability reporting form](https://github.com/davidahma/setrya/security/advisories/new). Include the affected Setrya version, what you observed, and a minimal synthetic reproduction. Keep credentials, customer queries, passages, and private system details out of public issues.

The current release series receives fixes. Setrya loads adapter code from the local Python environment, so review the drill and its dependencies before executing it. Use the installed `setrya replay` command or `python -I -m setrya replay` to inspect downloaded evidence. The installed console adds the working checkout to Python’s import path only for live adapter checks.

## Tenant offboarding evidence

Use the installed `setrya offboarding gate` or `setrya offboarding verify-receipt` command to review portable offboarding evidence. Treat request digests and job IDs in the original case ticket as the reference. Keep customer receipts, provider documents and backup schedules in the authorized case system. Only the bundled synthetic example is intended for public CI artifacts.

Setrya checks the files, request binding, and internal consistency. Someone who controls the observer or job log could fabricate the recorded events. Review the identity and permissions of each evidence producer before relying on its readings. A passing technical result covers the declared reads and checkpoints. Provider statements identify their issuer, and scheduled retention stays an open item. [Receipt guide](docs/offboarding.md#review-one-deletion-request)

## Subject erasure evidence

Run destructive adapters against disposable test subjects. Only propagate subject headers, trace baggage and LangGraph configuration across trusted application boundaries. A tenant-keyed subject reference is linkable pseudonymous data. Store keys, labels, caller aliases, model names and source IDs must be safe to share before publishing a receipt. The observer, its inventory, and its credentials define the scope of a verified result. [Evidence and signature verification](docs/erasure.md)

## Python dependency checks

Core Setrya has no runtime dependencies. CI audits the resolved packages used by the PostgreSQL, Redis, LangChain, Celery and pytest integration job, including its Python packaging tools. The OpenDesk image audit separately checks the native executable and service images on amd64 and arm64. [Integration workflow](.github/workflows/ci.yml) · [Image audit](.github/workflows/offboarding-trial-images.yml)

## Verify the Python release

Starting with 0.9.1, the offline qualification workflow attests the source archive after testing the checkout and its unpacked contents. Download the archive, set `EXPECTED_COMMIT` to the full Git commit from your approved release record, and verify the artifact before installing it.

```sh
gh attestation verify setrya-0.17.0.tar.gz --repo davidahma/setrya \
  --signer-workflow davidahma/setrya/.github/workflows/qualification.yml \
  --source-ref refs/heads/main --source-digest "$EXPECTED_COMMIT" --deny-self-hosted-runners
python -m pip install ./setrya-0.17.0.tar.gz
```

## Verify a downloaded offboarding tool

The portable CLI runs with Python 3.11 or newer. Before executing it, check that GitHub attested the exact file on Setrya’s protected main branch. Obtain the expected full commit ID from your approved release record. The same command accepts the downloadable synthetic offboarding ZIP and reference report. The workflow runs the packaged code and scans the generated evidence before uploading the files. [Portable build workflow](.github/workflows/offboarding-portable.yml)

```sh
gh attestation verify setrya-offboarding.pyz --repo davidahma/setrya \
  --signer-workflow davidahma/setrya/.github/workflows/offboarding-portable.yml \
  --source-ref refs/heads/main --source-digest "$EXPECTED_COMMIT" --deny-self-hosted-runners
python3 -I setrya-offboarding.pyz --help
```

The OpenDesk trial runs synthetic data against a checksum-verified application build and two digest-pinned service images. It checks the running application’s executable digest before collecting observations. The trial guide covers isolation, read permissions and the vulnerability inventory for each supported architecture. [OpenDesk trial](setrya/examples/opendesk/README.md)
