# Run, replay, and review erasure

Install the core package with Python 3.11 or later. The primary starter runs against a disposable workspace. For `init` and `demo`, person scope is the default. Use `--scope tenant` for tenant offboarding. Choose a fresh output directory for each run.

```sh
python -m pip install setrya
setrya init --output subject-test
setrya doctor --starter subject-test/test_erasure.py
setrya run --starter subject-test/test_erasure.py --mode deletion --output before-queue
setrya run --starter subject-test/test_erasure.py --mode late-write --output after-queue
setrya replay after-queue
```

The generated starter also runs under `python -m pytest -q subject-test/test_erasure.py`. Replace its sample `CASE` and `open_fixture` imports with your own test fixture. Doctor opens the fixture, checks its declared hooks, reads its store keys and asks both questions as each caller. Your fixture setup may seed test data. Doctor does not invoke the application's deletion or worker hooks. For `late-write`, wire a checkpoint that holds the same job before its write and drains its serving effects on release. The older `setrya init erasure` template starts with an unconnected shared-case fixture and includes a cache factory. [Observation types and evidence contract](erasure-v3.md) · [Migrating cache revisions](local-cache.md)

For PostgreSQL LangGraph deployments install `'setrya[erasure,erasure-postgres]'`. For Elasticsearch add `erasure-elasticsearch`. For the shared answer cache add `redis`. Replay of downloaded evidence uses the core package alone.

If the fixture exits before writing a receipt, `setrya run` reports the fixture exit code. An unconnected starter exits 3. Other exits without a receipt return ERROR (2). For application logs and tracebacks, run the trusted starter directly in your private development environment with its usual `--mode` and a fresh `--output`.

## Two decisions

`setrya replay DIRECTORY` returns the engineering result. Exit 0 means required application evidence passed, 1 means an established failure, 2 means an error, and 3 means required evidence is insufficient. A deletion-only result identifies the worker race as unexercised. `setrya replay DIRECTORY --case-review` also exits 3 when a technically passing case has outstanding review items. Open the owner, evidence reference and follow-up date in the case system. A missing required application read prevents technical PASS.

## Verify the signed release

Extract `workspace-erasure.zip` or `shared-investigation.zip` from the release. Provision a trusted Sigstore root independently, then verify the signed receipt and its sibling evidence.

```sh
setrya verify --receipt guarded/erasure-receipt.json --bundle guarded/receipt.sigstore.json \
  --trusted-root "$TRUSTED_ROOT" --issuer https://token.actions.githubusercontent.com \
  --identity https://github.com/davidahma/setrya/.github/workflows/erasure.yml@refs/heads/main
setrya replay guarded
```

Use `erasure sign --key cosign.key --receipt … --bundle …` and `erasure verify --key cosign.pub --receipt … --bundle …` for local detached Cosign signatures. Distribute the public key independently. Add `--scope person` or `--scope tenant` to `setrya verify` or `setrya replay` when the reviewer requires that scope.

## Tenant scope

`setrya demo --scope tenant --output tenant-run` exercises the departing and retained tenants and writes a request-bound v3 receipt. Replay the receipt with `setrya replay tenant-run/receipt`. In a real case, add `--expect-request` with the digest from the ticket issued before the deletion, plus `--expect-job` with the dispatch ID. Use `setrya run --scope tenant --help` for the original evidence and attachment arguments. The offboarding CI gate remains available separately. [Tenant recipe](offboarding.md)

Person, tenant and experimental discovery receipts use the same top-level replay and verify commands. Historical v2 person receipts retain their issued verdict. An archived v1 tenant receipt requires the original request digest, policy, observations and exhibits. [Receipt compatibility](receipt-format.md)
