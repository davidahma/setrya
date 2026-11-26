# Setrya

**Prove deleted customer data stays out of your AI, even when a background job tries to write it back.**

Find where your app and agents copy customer data, and prove deletion reaches each copy you test.

Your app and its agents copy customer data into caches, vector indexes, memory and traces. Deleting the source can leave those copies behind, and a job that read the data before deletion can write it back afterward. Setrya finds copy sites in your code, runs your app's real deletion, and checks the stores, answers and background writers you declare. Each completed drill produces a replayable receipt you can sign for offline review. [Why deleted data comes back](https://github.com/davidahma/setrya/blob/v1.17.0/docs/write-back.md)

## 1. Find the copies

```sh
python -m pip install setrya
setrya discovery copy-scan .
```

The default scan gives you the file and line of supported Python writes. For TypeScript, install `setrya[typescript]` and run `setrya discovery copy-scan . --language typescript`. The GitHub Action can report new copy sites and recognized background writers on pull requests. [Scan and PR setup](https://github.com/davidahma/setrya/blob/v1.17.0/docs/copy-coverage.md) · [GitHub Marketplace](https://github.com/marketplace/actions/setrya-erasure-drill)

## 2. Catch a write-back

```sh
setrya demo --output workspace
setrya replay workspace/broken    # FAIL: a job restored the deleted note
setrya replay workspace/guarded   # PASS: the job checked its sources first
```

A customer's support note is deleted while a handoff job is still running. In the broken app, the job writes the note back and two teammates can read it again. The guarded job checks its source revisions before writing. In both, the teammates can still read the team's launch date. [Connect your app](https://github.com/davidahma/setrya/blob/v1.17.0/docs/github-action.md#switch-from-the-sample-to-your-app) · [Test an HTTP app on any runtime](https://github.com/davidahma/setrya/blob/v1.17.0/docs/http-cases.md) · [Fix a write-back](https://github.com/davidahma/setrya/blob/v1.17.0/docs/fix-write-back.md)

## 3. Hand over the receipt

| Verdict | Exit | What a reviewer can conclude |
| --- | --- | --- |
| `PASS` | 0 | The tested stores and answers stayed clear of the erased data through the post-deletion checks, and the retained data survived. |
| `FAIL` | 1 | A copy survived or came back, or data that should have stayed was lost. |
| `ERROR` | 2 | The test or evidence could not be processed reliably. |
| `NOT TESTED` | 3 | Required evidence was missing or inconclusive. |

Drill receipts can record the Setrya version, checkout and test-file fingerprints, and deployment identity when captured. Provider follow-ups stay visible alongside the technical verdict. Reviewers can verify signed receipt exchanges and assessments in a browser with nothing installed. The CLI replays the full evidence and can compare a release with the last one they approved. [Receipt guide](https://github.com/davidahma/setrya/blob/v1.17.0/docs/receipt-format.md) · [Verify offline](https://github.com/davidahma/setrya/blob/v1.17.0/docs/assessment.md#verify-without-installing-tools) · [Compare releases](https://github.com/davidahma/setrya/blob/v1.17.0/docs/assessment.md) · [Attach evidence to a governance ticket](https://github.com/davidahma/setrya/blob/v1.17.0/docs/governance-handoff.md)

Export verified receipt observations as OSCAL Assessment Results for a governance review. A control-mapping draft is open for auditor review. [Public draft](https://github.com/davidahma/setrya/blob/v1.17.0/docs/control-mappings/README.md) · [OSCAL export](https://github.com/davidahma/setrya/blob/v1.17.0/docs/oscal.md)

Check that a distinct record under hold survives deletion, consolidation, and summary regeneration. [Legal-hold case](https://github.com/davidahma/setrya/blob/v1.17.0/docs/legal-hold.md)

## What it checks today

- **Code scanning** covers Python and TypeScript. Install `setrya[typescript]` and pass `--language typescript` to scan TypeScript, or `--language all` for both. [TypeScript starter](https://github.com/davidahma/setrya/blob/v1.17.0/docs/typescript-starter.md) · [HTTP cases](https://github.com/davidahma/setrya/blob/v1.17.0/docs/http-cases.md)
- **Stores and memory** include PostgreSQL and pgvector, SQLite, Elasticsearch, Redis, LangGraph, Mem0, Graphiti, local files and Git history. [Agent and trace integrations](https://github.com/davidahma/setrya/blob/v1.17.0/docs/harnesses.md)
- **Hosted AI platforms** include readback adapters for OpenAI, Anthropic, Bedrock, Letta and Mem0. These checks cover what each API shows. This release validates the adapters against simulated responses. [Provider checks](https://github.com/davidahma/setrya/blob/v1.17.0/docs/provider-readback.md)
- **Memory vendors** have signed results with the recorded operator and tested surfaces. Review a linked rerun for changes or attach a vendor attestation to a governance ticket. [Vendor results](https://github.com/davidahma/setrya/blob/v1.17.0/docs/vendor-conformance.md)

Setrya reports only what it observed. Use the receipt's declared scope to see which copies and read paths were checked.

[Glossary](https://github.com/davidahma/setrya/blob/v1.17.0/docs/glossary.md) · [Examples and legacy workflows](https://github.com/davidahma/setrya/blob/v1.17.0/docs/examples-archive.md) · [Release schedule](https://github.com/davidahma/setrya/blob/v1.17.0/docs/releases.md) · [Contributing](https://github.com/davidahma/setrya/blob/v1.17.0/CONTRIBUTING.md) · [Apache 2.0 license](https://github.com/davidahma/setrya/blob/v1.17.0/LICENSE)
