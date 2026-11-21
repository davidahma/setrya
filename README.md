# Setrya

**Test whether your AI assistant can still reveal a customer's deleted information.**

A customer asks your team to delete their onboarding notes. The source row disappears. A support lead opens yesterday’s workspace handoff and asks which hostname the customer used. The assistant still knows it.

Setrya turns that incident into a regression test. It holds the job that wrote the handoff, runs your application’s deletion operation and asks the real answer endpoint what two teammates can still see. It saves the store reads, answers and worker observations in a receipt that a release reviewer can replay offline.

## Try the broken and fixed versions

The included B2B SaaS workspace runs locally with Python 3.11 or later. It uses SQLite, an HTTP endpoint, separate user sessions and fictional customer data.

```sh
python -m pip install setrya
setrya demo --output workspace
setrya replay workspace/guarded
```

| After the delayed writer finishes | Broken application | Guarded application |
| --- | --- | --- |
| Support lead asks for the deleted customer’s hostname | `cobalt-bridge.example` | No matching note |
| Account manager asks the same question in the same workspace | `cobalt-bridge.example` | No matching note |
| Both ask for the team’s next launch date | `October 18` | `October 18` |
| Engineering verdict | **FAIL** | **PASS** |

The broken worker restores a summary it captured before deletion. The fixed worker compares its captured source revisions with the surviving notes before it can publish. The launch date tests a second failure mode. Wiping the whole handoff would remove the hostname and also destroy information the team still needs.

The guarded receipt reports **PASS · OPEN_ITEMS**. The store and endpoint checks passed. A fictional provider still has a configuration-only record. The case owner can see who must follow up and when. Use `setrya replay workspace/guarded --case-review` to gate on that obligation too. [Download the signed workspace examples](https://github.com/davidahma/setrya/releases/download/v1.0.0/workspace-erasure.zip) · [Verify the signer](https://github.com/davidahma/setrya/blob/v1.0.0/docs/erasure-cli.md)

## Test your application

Start with a private note and one authorized reader. The starter runs as generated so you can see the full handoff before connecting your own app.

```sh
setrya init --output customer-test
setrya doctor --starter customer-test/test_erasure.py
setrya run --starter customer-test/test_erasure.py --mode deletion --output receipt
setrya replay receipt
```

Replace `CASE` and `open_fixture` in the starter. Seed a disposable workspace with one fact to erase and one to keep. Wire `inspect` to read each declared key, `read` to ask the authenticated application and `erase` to call its deletion operation. Doctor reads the baseline. The drill performs the deletion. You can also run the starter with pytest.

For a shared answer, declare which objects the application must **delete**, **rewrite at the same key**, or **preserve**. Choose a literal marker, regex, entity or application-supplied model judge for answers. Connect the worker checkpoint to test `--mode late-write`. In CI the GitHub Action runs the fixture, keeps the exit code and posts the receipt summary to the PR. [Fixture and verdict rules](https://github.com/davidahma/setrya/blob/v1.0.0/docs/erasure-v3.md) · [GitHub Actions workflow](https://github.com/davidahma/setrya/blob/v1.0.0/docs/github-action.md)

## What a receipt covers

| Result | What the reviewer can conclude |
| --- | --- |
| `PASS` · exit 0 | The declared store and serving checks passed for the tested callers and mode. |
| `FAIL` · exit 1 | A read found a residual or showed that required information was lost. |
| `ERROR` · exit 2 | The fixture or evidence could not be processed reliably. |
| `NOT TESTED` · exit 3 | A required observation was missing, such as a direct cache read or a confirmed worker checkpoint. |

Setrya can inspect declared keys, logged copies and synthetic markers within the scanners’ configured scope. A paraphrase can lose a marker, so ask about the fact itself. Provider configuration and retention dates become separate review items. The receipt names the evidence the application supplied and the scope the test exercised. [v3 schema and compatibility](https://github.com/davidahma/setrya/blob/v1.0.0/docs/api-stability.md) · [SDK integrations and coverage](https://github.com/davidahma/setrya/blob/v1.0.0/docs/harnesses.md)

## Two more ways to use it

- **Reopen a LangGraph investigation.** Two analysts ask about a quarantined laptop after a shared checkpoint is rewritten. A scoped canary also finds an export that lost its subject tag. Install `'setrya[erasure]'` and run `setrya demo analyst --output investigation`. [Investigation drill](https://github.com/davidahma/setrya/blob/v1.0.0/docs/shared-investigation.md) · [Independent Graphiti trial](https://github.com/davidahma/setrya/blob/v1.0.0/docs/graphiti-trial.md)
- **Start from an existing discovery finding.** The experimental Amazon Macie integration accepts a finding pointer and an application-owned mapping. It correlates the source revision with the stores, workers and routes in the test, then returns the evidence to the originating case. The bundled gap case names the unread cache and its owner. No AWS account is needed to run it. [Discovery trial](https://github.com/davidahma/setrya/blob/v1.0.0/docs/discovery-trial.md) · [Mapping and approval](https://github.com/davidahma/setrya/blob/v1.0.0/docs/discovery-mapping.md)

Setrya also tests access revocation, search freshness, late cache fills and tenant offboarding. [Other drills and Python SDK](https://github.com/davidahma/setrya/blob/v1.0.0/docs/other-drills.md) · [Release notes](https://github.com/davidahma/setrya/blob/v1.0.0/CHANGELOG.md) · [Apache 2.0 license](https://github.com/davidahma/setrya/blob/v1.0.0/LICENSE)
