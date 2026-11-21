# Setrya

**Test whether your AI assistant can still reveal deleted information.** Setrya drives a deletion through a disposable application, checks its stores and serving endpoint, and records what a reviewer can replay after the app shuts down.

## Reopen a shared investigation

Two security analysts use the same saved investigation. Alice’s source identifies a quarantined laptop. Bob’s source records a credential reset. Delete Alice’s source while a background job still holds the old summary. When either analyst asks “Which laptop was quarantined?”, what does the assistant reveal?

```sh
python -m pip install 'setrya[erasure]'
setrya erasure demo --output investigation
setrya erasure replay investigation/guarded
```

| Run | What the analysts and stores show | Engineering | Case review |
| --- | --- | --- | --- |
| Broken worker | Alice’s finding returns in the original investigation after the worker resumes. | FAIL | RESIDUAL_FOUND |
| Guarded worker | Both analysts keep Bob’s finding. Alice’s finding stays gone from the checked stores and answers. | PASS | OPEN_ITEMS |

The demo also finds a synthetic snapshot that lost its subject tag. A fictional model provider appears as a follow-up. The case owner can see who owns it, the evidence available, and when to check again. A Cosign signature authenticates the receipt, which binds the observations used for replay. [Download both signed runs](https://github.com/davidahma/setrya/releases/download/v0.17.0/shared-investigation.zip) · [Verify a receipt](https://github.com/davidahma/setrya/blob/v0.17.0/docs/erasure-cli.md)

## Connect your app

Run `setrya erasure init` in a test checkout. Connect direct store reads, your app’s selective rewrite, and two authenticated sessions. Start with deletion only. Add the pre-write checkpoint when you want to test a delayed worker. Setrya checks declared stores, observed or linked copies, and searchable synthetic markers in the test fixture. Providers supply configuration evidence. [Starter and command guide](https://github.com/davidahma/setrya/blob/v0.17.0/docs/erasure-cli.md) · [Shared investigation](https://github.com/davidahma/setrya/blob/v0.17.0/docs/shared-investigation.md) · [Store integrations](https://github.com/davidahma/setrya/blob/v0.17.0/docs/erasure-adapters.md)

Setrya also tests tenant offboarding, access revocation, search freshness, and late cache fills in pull requests. [Examples and Python SDK](https://github.com/davidahma/setrya/blob/v0.17.0/docs/other-drills.md) · [Independent Graphiti trial](https://github.com/davidahma/setrya/blob/v0.17.0/docs/graphiti-trial.md)
