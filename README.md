# Setrya

**Test whether your AI assistant can still reveal a customer's deleted information.**

A customer deletes their onboarding notes. Yesterday’s background job finishes and puts their hostname back into the support handoff. Setrya catches this by asking the assistant what it can still tell two teammates. On a pull request, the GitHub Action posts the verdict and links to the store reads and answers behind it. [Setrya erasure drill on GitHub Marketplace](https://github.com/marketplace/actions/setrya-erasure-drill)

## Get your first PR verdict

Start in the root of your GitHub repository with Python 3.11+ on macOS or Linux. These commands create a branch, a virtual environment and two files for your PR. Use an unused starter directory and workflow path. The sample uses synthetic data and needs no application credentials.

```sh
git switch -c try-setrya
python3 -m venv .venv-setrya
. .venv-setrya/bin/activate
python -m pip install setrya==1.0.2
setrya init --output setrya-case
setrya doctor --starter setrya-case/test_erasure.py --mode deletion
mkdir -p .github/workflows
curl -fsSL https://raw.githubusercontent.com/davidahma/setrya/v1.0.2/docs/examples/first-pr/setrya.yml -o .github/workflows/setrya.yml
git add setrya-case/test_erasure.py .github/workflows/setrya.yml
git commit -m "Run the Setrya deletion check on pull requests"
git push -u origin HEAD
```

Open a pull request. In **Checks → Setrya erasure**, the sample should report **PASS · READY_FOR_REVIEW**. The workflow tests deletion, saves the receipt as `setrya-receipt` and comments on same-repository PRs. A green sample tells you the CI wiring works. [See a real PR comment](https://github.com/davidahma/setrya/pull/35#issuecomment-7064370996) · [First-PR workflow and offline replay](https://github.com/davidahma/setrya/blob/v1.0.2/docs/github-action.md)

## Test your application

In `setrya-case/test_erasure.py`, replace the sample imports with your own fixture.

```diff
-from setrya.examples.workspace import SIMPLE as CASE
-from setrya.examples.workspace import open_fixture
+from helpdesk_fixture import CASE, open_fixture
```

For example, seed a test customer’s depot code and a team dispatch day. Have `inspect` read the note from your store, `read` ask your authenticated answer route and `erase` call your deletion operation. Push the fixture and its test dependencies. The same PR now checks whether the depot code disappears and the dispatch day survives. Start with `deletion` and switch to `late-write` once you can pause and release the worker that captured the old note. [Runnable help-desk fixture and wiring instructions](https://github.com/davidahma/setrya/blob/v1.0.2/docs/github-action.md#switch-from-the-sample-to-your-app)

## Try the broken and fixed versions

To see the worker race locally, run the bundled workspace demo. Two teammates reopen the same handoff after a customer asks for deletion.

```sh
python -m pip install setrya==1.0.2
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

The guarded receipt reports **PASS · OPEN_ITEMS**. The store and endpoint checks passed. A fictional provider still has a configuration-only record. The case owner can see who must follow up and when. Use `setrya replay workspace/guarded --case-review` to gate on that obligation too. [Download the signed workspace examples](https://github.com/davidahma/setrya/releases/download/v1.0.2/workspace-erasure.zip) · [Verify the signer](https://github.com/davidahma/setrya/blob/v1.0.2/docs/erasure-cli.md)

## What a receipt covers

| Result | What the reviewer can conclude |
| --- | --- |
| `PASS` · exit 0 | The declared store and serving checks passed for the tested callers and mode. |
| `FAIL` · exit 1 | A read found a residual or showed that required information was lost. |
| `ERROR` · exit 2 | The fixture or evidence could not be processed reliably. |
| `NOT TESTED` · exit 3 | A required observation was missing, such as a direct cache read or a confirmed worker checkpoint. |

Setrya can inspect declared keys, logged copies and synthetic markers within the scanners’ configured scope. A paraphrase can lose a marker, so ask about the fact itself. Provider configuration and retention dates become separate review items. The receipt names the evidence the application supplied and the scope the test exercised. [v3 schema and compatibility](https://github.com/davidahma/setrya/blob/v1.0.2/docs/api-stability.md) · [SDK integrations and coverage](https://github.com/davidahma/setrya/blob/v1.0.2/docs/harnesses.md)

## Two more ways to use it

- **Reopen a LangGraph investigation.** Two analysts ask about a quarantined laptop after a shared checkpoint is rewritten. A scoped canary also finds an export that lost its subject tag. Install `'setrya[erasure]'` and run `setrya demo analyst --output investigation`. [Investigation drill](https://github.com/davidahma/setrya/blob/v1.0.2/docs/shared-investigation.md) · [Independent Graphiti trial](https://github.com/davidahma/setrya/blob/v1.0.2/docs/graphiti-trial.md)
- **Start from an existing discovery finding.** The experimental Amazon Macie integration accepts a finding pointer and an application-owned mapping. It correlates the source revision with the stores, workers and routes in the test, then returns the evidence to the originating case. The bundled gap case names the unread cache and its owner. No AWS account is needed to run it. [Discovery trial](https://github.com/davidahma/setrya/blob/v1.0.2/docs/discovery-trial.md) · [Mapping and approval](https://github.com/davidahma/setrya/blob/v1.0.2/docs/discovery-mapping.md)

Setrya also tests access revocation, search freshness, late cache fills and tenant offboarding. [Other drills and Python SDK](https://github.com/davidahma/setrya/blob/v1.0.2/docs/other-drills.md) · [Release notes](https://github.com/davidahma/setrya/blob/v1.0.2/CHANGELOG.md) · [Apache 2.0 license](https://github.com/davidahma/setrya/blob/v1.0.2/LICENSE)
