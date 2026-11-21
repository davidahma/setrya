# Run, replay, and review erasure

Start from a disposable test subject in a LangGraph app. Use Python 3.11+ and choose a new output directory for each run.

```sh
python -m pip install 'setrya[erasure]'
setrya erasure init --output subject-test
# Connect open_fixture() and declare the artifact keys, questions and callers in test_erasure.py.
setrya erasure run --starter subject-test/test_erasure.py --mode deletion --output before-queue
setrya erasure run --starter subject-test/test_erasure.py --mode late-write --output after-queue
setrya erasure replay after-queue
```

The generated starter also runs under `python -m pytest -q -s subject-test/test_erasure.py`. It fails visibly until connected. Wire the fixture to your own test application and use the contract for inspecting store keys, reopening the real route, and running the application’s delete or rebuild operation. In late-write mode the application must hold the same worker attempt before its write and wait for its writes and refreshes to complete on release. The generated `answer_cache.py` factory enables source-revision checks if your fixture uses Setrya’s local or Redis cache. [Observation types and evidence contract](erasure-v3.md) · [Migrating cache revisions](local-cache.md)

For PostgreSQL LangGraph deployments install `'setrya[erasure,erasure-postgres]'`. For Elasticsearch add `erasure-elasticsearch`. For the shared answer cache add `redis`. Replay of downloaded evidence uses the core package alone.

## Two decisions

`setrya erasure replay DIRECTORY` returns the engineering result. Exit 0 means required application evidence passed, 1 means an established failure, 2 means an error, and 3 means required evidence is insufficient. A deletion-only result identifies the worker race as unexercised. `setrya erasure replay DIRECTORY --case-review` also exits 3 when a technically passing case has outstanding review items. Open the owner, evidence reference and follow-up date in the case system. A missing required application read prevents technical PASS.

## Verify the signed release

Extract `shared-investigation.zip` from the release. Provision a trusted Sigstore root independently, then verify the signed receipt and its sibling evidence.

```sh
setrya erasure verify --receipt guarded/erasure-receipt.json --bundle guarded/receipt.sigstore.json \
  --trusted-root "$TRUSTED_ROOT" --issuer https://token.actions.githubusercontent.com \
  --identity https://github.com/davidahma/setrya/.github/workflows/erasure.yml@refs/heads/main
setrya erasure replay guarded
```

Use `erasure sign --key cosign.key --receipt … --bundle …` and `erasure verify --key cosign.pub --receipt … --bundle …` for local detached Cosign signatures. Distribute the public key independently.

## Tenant scope and existing commands

`erasure demo --scope tenant` runs the tenant fixture. `erasure init --scope tenant` writes its starter policy. `erasure run --scope tenant --policy POLICY --observations OBSERVATIONS` evaluates the tenant gate. `erasure replay --scope tenant` accepts the original tenant verification arguments, including request digest, policy, evidence, and any external exhibits. `erasure sign` and `erasure verify` accept `--scope tenant` for a tenant receipt. The tenant receipt and the person receipt keep their original rules. [Tenant verification recipe](offboarding.md)

`setrya demo erasure` and `setrya init erasure` are aliases for the corresponding person commands. Specify `--example legacy` for the original two-thread demo or its starter. `setrya replay`, `setrya offboarding`, `erasure sign-receipt`, and `erasure verify-signature` remain available. [Onboarding measurement](erasure-handoff.md)
