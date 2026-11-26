# Setrya examples and legacy workflows

## Get your first PR verdict

Start in the root of your GitHub repository with Python 3.11+ on macOS or Linux. These commands create a branch, a virtual environment and two files for your PR. Use an unused starter directory and workflow path. The sample uses synthetic data and needs no application credentials.

```sh
git switch -c try-setrya
python3 -m venv .venv-setrya
. .venv-setrya/bin/activate
python -m pip install setrya==1.15.0
setrya init --output setrya-case
setrya doctor --starter setrya-case/test_erasure.py --mode deletion
mkdir -p .github/workflows
curl -fsSL https://github.com/davidahma/setrya/releases/download/v1.15.0/setrya-quickstart.yml -o .github/workflows/setrya.yml
git add setrya-case/test_erasure.py .github/workflows/setrya.yml
git commit -m "Run the Setrya deletion check on pull requests"
git push -u origin HEAD
```

Open a pull request. In **Checks → Setrya erasure**, the sample should report **PASS · READY_FOR_REVIEW**. The workflow tests deletion, saves the receipt as `setrya-receipt` and comments on same-repository PRs. A green sample tells you the CI wiring works. [See a real PR comment](https://github.com/davidahma/setrya/pull/38#issuecomment-7065545463) · [First-PR workflow and offline replay](https://github.com/davidahma/setrya/blob/v1.8.0/docs/github-action.md)

## Test your application

In `setrya-case/test_erasure.py`, replace the sample imports with your own fixture.

```diff
-from setrya.examples.workspace import SIMPLE as CASE
-from setrya.examples.workspace import open_fixture
+from helpdesk_fixture import CASE, open_fixture
```

For example, seed a test customer’s depot code and a team dispatch day. Have `inspect` read the note from your store, `read` ask your authenticated answer route and `erase` call your deletion operation. Push the fixture and its test dependencies. The same PR now checks whether the depot code disappears and the dispatch day survives. Start with `deletion` and switch to `write-back` once you can pause and release the worker that captured the old note. [Runnable help-desk fixture and wiring instructions](https://github.com/davidahma/setrya/blob/v1.8.0/docs/github-action.md#switch-from-the-sample-to-your-app) · [Help-desk example PR](https://github.com/davidahma/setrya/pull/38)

## Try the broken and fixed versions

To see a stale write-back locally, run the bundled workspace demo. Two teammates reopen the same handoff after a customer asks for deletion.

```sh
python -m pip install setrya==1.15.0
setrya demo --output workspace
setrya replay workspace/guarded
```

| After the deferred writer finishes | Broken application | Guarded application |
| --- | --- | --- |
| Support lead asks for the deleted customer’s hostname | `cobalt-bridge.example` | No matching note |
| Account manager asks the same question in the same workspace | `cobalt-bridge.example` | No matching note |
| Both ask for the team’s next launch date | `October 18` | `October 18` |
| Engineering verdict | **FAIL** | **PASS** |

The broken worker restores a summary it captured before deletion. The fixed worker compares its captured source revisions with the surviving notes before it can publish. The launch date tests a second failure mode. Wiping the whole handoff would remove the hostname and also destroy information the team still needs.

The guarded receipt reports **PASS · OPEN_ITEMS**. The store and endpoint checks passed. A fictional provider still has a configuration-only record. The case owner can see who must follow up and when. Use `setrya replay workspace/guarded --case-review` to gate on that obligation too. [Download the signed workspace examples](https://github.com/davidahma/setrya/releases/download/v1.6.0/workspace-erasure.zip) · [Verify the signer](https://github.com/davidahma/setrya/blob/v1.8.0/docs/erasure-cli.md)

## What a receipt covers

| Result | What the reviewer can conclude |
| --- | --- |
| `PASS` · exit 0 | The declared store and serving checks passed for the tested callers and mode. |
| `FAIL` · exit 1 | A read found a residual or showed that required information was lost. |
| `ERROR` · exit 2 | The fixture or evidence could not be processed reliably. |
| `NOT TESTED` · exit 3 | A required observation was missing, such as a direct cache read or a confirmed worker checkpoint. |

Setrya can inspect declared keys, logged copies and synthetic markers within the scanners’ configured scope. A paraphrase can lose a marker, so ask about the fact itself. Provider configuration and retention dates become separate review items. The receipt names the evidence the application supplied and the scope the test exercised. [v3 schema and compatibility](https://github.com/davidahma/setrya/blob/v1.8.0/docs/api-stability.md) · [SDK integrations and coverage](https://github.com/davidahma/setrya/blob/v1.8.0/docs/harnesses.md)

## Hand off a completed deletion ticket

A support app closes a deletion ticket. Setrya checks the journalled PostgreSQL keys with a read-only login and signs a draft v1 exchange for the reviewer. The reviewer can check the issuer, match the original ticket and replay the verdict offline. Try the real PostgreSQL example or download the signed receipts for absence, a residual summary, a missing control and an open provider item. [PostgreSQL handoff](https://github.com/davidahma/setrya/blob/v1.8.0/docs/receipt-handoff.md) · [Draft receipt format](https://github.com/davidahma/setrya/blob/v1.8.0/docs/receipt-exchange.md) · [Download the examples](https://github.com/davidahma/setrya/releases/download/v1.8.0/deletion-receipt-exchange.zip)

## Follow a security finding into a saved PDF

Run `setrya demo northline` to start from a synthetic Macie finding. The application rewrites finding F-42 to remove the customer’s identifying detail while preserving the security finding. After a delayed refresh, Setrya reads the saved chart and PDF and asks the same investigation as two authorized analysts. Replay the broken result to see which copies came back. The guarded result passes the application checks and leaves a named provider follow-up open. [Walk through the case](https://github.com/davidahma/setrya/blob/v1.8.0/docs/northline.md) · [Download the signed receipts](https://github.com/davidahma/setrya/releases/download/v1.6.0/northline-case-review.zip)

### Catch a new export before the deletion test

Run `setrya demo northline-source` to see a code change add an archive PDF outside the approved case. Setrya flags the write site before the app runs. After the export is mapped, the broken worker restores the old PDF and the guarded worker leaves it redacted. [Source coverage and CI setup](https://github.com/davidahma/setrya/blob/v1.8.0/docs/source-coverage.md) · [Download the replayable results](https://github.com/davidahma/setrya/releases/download/v1.7.0/northline-source-review.zip)

## Check where the data can come back

Run `setrya demo branches` to see the source database pass a direct read while a pre-erasure SQLite fork and a restored snapshot still contain the record. The branch observer checks declared SQLite backups, Git clones and worktrees, and Neon branch metadata. A disclosed Postgres restore window stays open until its recorded expiry. [Branch lineage and replay](https://github.com/davidahma/setrya/blob/v1.8.0/docs/branch-lineage.md)

For encrypted copies, the read-only AWS KMS observer can verify a destroyed key against a captured list of ciphertext copies. The receipt distinguishes `crypto-erased` from physically absent. [Key-destruction evidence](https://github.com/davidahma/setrya/blob/v1.8.0/docs/crypto-erasure.md)

## More erasure drills

- **Check memory files and Git history.** A background worker has already read a deleted note, and Git still remembers the original. Run `setrya demo memory-files` to see both failures and replay the guarded result. [Memory-file drill and AgentFS scanner](https://github.com/davidahma/setrya/blob/v1.8.0/docs/memory-files.md)
- **Reopen a LangGraph investigation.** Two analysts ask about a quarantined laptop after a shared checkpoint is rewritten. A scoped canary also finds an export that lost its subject tag. Install `'setrya[erasure]'` and run `setrya demo analyst --output investigation`. [Investigation drill](https://github.com/davidahma/setrya/blob/v1.8.0/docs/shared-investigation.md) · [Independent Graphiti trial](https://github.com/davidahma/setrya/blob/v1.8.0/docs/graphiti-trial.md)
- **Start from an existing discovery finding.** The experimental Amazon Macie integration accepts a finding pointer and an application-owned mapping. It correlates the source revision with the stores, workers and routes in the test, then returns the evidence to the originating case. The bundled gap case names the unread cache and its owner. No AWS account is needed to run it. [Discovery trial](https://github.com/davidahma/setrya/blob/v1.8.0/docs/discovery-trial.md) · [Mapping and approval](https://github.com/davidahma/setrya/blob/v1.8.0/docs/discovery-mapping.md)

Setrya also tests access revocation, search freshness, late cache fills and tenant offboarding. [Other drills and Python SDK](https://github.com/davidahma/setrya/blob/v1.8.0/docs/other-drills.md) · [Release notes](https://github.com/davidahma/setrya/blob/v1.8.0/CHANGELOG.md) · [Apache 2.0 license](../LICENSE)

## Connect an app or review a completed request

A coding agent can replay receipts and explain the verdict through `setrya mcp`. Approve a disposable fixture to enable it to run a drill. Teams can bring copy observations from other instrumentation with `setrya journal import --otlp`. [MCP server and agent skill](https://github.com/davidahma/setrya/blob/v1.8.0/docs/mcp.md) · [Copy-event schema and attributes](https://github.com/davidahma/setrya/blob/v1.8.0/docs/copy-events.md)

For store authors, `setrya conformance run` signs observations from the pgvector, LangGraph Store or Mem0 fixture. The matrix shows which surfaces were exercised and which still require evidence for Setrya Verified. [Vendor fixtures and self-check Action](https://github.com/davidahma/setrya/blob/v1.8.0/docs/vendor-conformance.md)

Start with the stack your team already runs. The [LangGraph guide](https://github.com/davidahma/setrya/blob/v1.8.0/docs/langgraph-integration.md) tests saved memory. The [Langfuse guide](https://github.com/davidahma/setrya/blob/v1.8.0/docs/langfuse-integration.md) checks stored traces. For a TypeScript or other HTTP service, declare authenticated reads, deletion and direct store observations in YAML. [Stack starters and HTTP cases](https://github.com/davidahma/setrya/blob/v1.8.0/docs/http-cases.md)

For generated answers, Setrya defaults to three reads per caller, question and stage. A single observed disclosure fails the drill. A lexical miss alone leaves absence uncertain. The receipt records sample counts and judge coverage. [Verdict rules](https://github.com/davidahma/setrya/blob/v1.8.0/docs/erasure-v3.md)

For a completed deletion ticket, the read-only observer checks journalled keys and can issue an `observed-deletion` receipt. Use `setrya receipt summary` for an engineer, reviewer or customer. When a follow-up comes due, `setrya recheck` records new evidence and links it to the original receipt. [Casework and permissions](https://github.com/davidahma/setrya/blob/v1.8.0/docs/customer-casework.md)

When several agents handle the same record, a journal can record the turn that read it and the exact versions that fed a derived copy. Verify the saved hash chain with `setrya journal verify copies.sqlite`. [Turn provenance and journal verification](https://github.com/davidahma/setrya/blob/v1.8.0/docs/erasure-hooks.md)

To connect an existing app, start from a [working stack example](https://github.com/davidahma/setrya/blob/v1.8.0/docs/http-cases.md#runnable-application-repositories). Use `setrya coverage-drift` to find observed stores, trace sinks or memory namespaces that lack an adapter or scanner. [Contributor guide](../CONTRIBUTING.md)
