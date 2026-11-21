# Setrya

**Test whether your AI assistant can still reveal deleted information.** A source record can disappear while its contents survive in a conversation, a cached answer, or a background job about to write yesterday’s summary. Setrya gives a platform engineer a regression test for that last mile. It runs against a disposable application and saves the evidence for whoever reviews the release.

## Reopen the investigation

Two security analysts share a saved investigation. Alice’s finding identifies a quarantined laptop. Bob’s records a credential reset. A worker has already read both when the application deletes Alice’s source and rebuilds the investigation. After the worker resumes, both analysts reopen the **same conversation** and ask “Which laptop was quarantined?” They also ask about Bob’s reset. Deleting the whole conversation would fail the second check.

Run both versions locally with Python 3.11 or later. The demo uses LangGraph, SQLite, a local HTTP endpoint, and fictional findings. It needs no model credentials.

```sh
python -m pip install 'setrya[erasure]'
setrya erasure demo --output investigation
setrya erasure replay investigation/guarded
```

| What happened after the worker finished | Engineering verdict | Case review |
| --- | --- | --- |
| The old worker wrote its stale summary. Both analysts could name Alice’s laptop again. | `FAIL` | `RESIDUAL_FOUND` |
| The guarded worker checked its input revisions before writing. Alice’s finding stayed gone from the checked stores and answers. Both analysts could still explain Bob’s reset. | `PASS` | `OPEN_ITEMS` |

The open item belongs to a fictional model provider. The receipt names an owner, an evidence reference, and a follow-up date. A release reviewer can see that the application passed and that provider-side deletion remains unverified. To review the published example, download the two signed runs, verify the signer, and replay either directory with the core package. [Signed runs](https://github.com/davidahma/setrya/releases/download/v0.17.0/shared-investigation.zip) · [Signature and replay commands](https://github.com/davidahma/setrya/blob/v0.18.0/docs/erasure-cli.md)

## What the drill checks

Setrya first establishes that both callers can recover both facts. It then calls the application’s own erasure operation and inspects the source, shared summary, LangGraph checkpoint, and cached answer directly. A rewritten record must have a new revision at the same key, lose Alice’s fact, and keep Bob’s. A clean answer with a missing required store read earns `NOT TESTED`.

The demo also seeds an export that lost its subject tag. A scoped search for a unique synthetic marker finds its store and key. A paraphrase can lose that marker, so the endpoint test asks about the laptop without mentioning Alice or the marker. In `late-write` mode, Setrya repeats the checks after the held worker finishes. In `deletion` mode, it reports the store and endpoint results and marks the worker race as unexercised. [Evidence and verdict rules](https://github.com/davidahma/setrya/blob/v0.18.0/docs/erasure-v3.md)

In a trial against **Graphiti on Neo4j**, removing the source episode succeeded on both tested versions. On 0.31.1 the shared node still named the laptop. On 0.31.2 the laptop disappeared and the credential reset survived. The trial used Graphiti’s own deletion method, a local summarizer, and a two-user test endpoint. Both results replay offline. [Reproduce the trial](https://github.com/davidahma/setrya/blob/v0.18.0/docs/graphiti-trial.md) · [Download the comparison](https://github.com/davidahma/setrya/releases/download/v0.17.0/graphiti-comparison.zip)

## Start with a finding from your catalog

Amazon Macie found sensitive data in an S3 object. Your security team has a case number and an object reference. Your application team needs to know whether the same fact survives in a summary, a conversation checkpoint, or a second cache that the assistant can read. Setrya accepts a local Macie finding export. It needs no AWS credentials.

```sh
setrya discovery demo --output catalog-review
setrya discovery replay catalog-review/gap
```

The second command exits 3 and identifies the unread analyst cache and its owner. In `catalog-review/broken`, the delayed writer restores the erased fact. In `catalog-review/guarded`, both analysts keep the credential-reset finding and the model-provider follow-up stays open. All three runs start from the same source and declared service list. [Download the signed cases](https://github.com/davidahma/setrya/releases/download/v0.18.0/macie-case-review.zip) · [Run the trial](https://github.com/davidahma/setrya/blob/v0.18.0/docs/discovery-trial.md)

| Who supplies the evidence | What it establishes |
| --- | --- |
| Discovery tool | The finding ID, classification, observation time, and a fingerprint of the S3 object version or ETag. Raw object names and matched content stay out of the imported finding. |
| Application owner | A reviewed mapping identifies the test subject, approved action, source revision, application commit, and environment. Each service has an owner. A retained source produces a dated follow-up. |
| Setrya drill | Direct reads show what happened to each checked key. Source-version relationships connect the original finding to summaries and caches. Two authenticated sessions show what the assistant actually returned. |
| Case reviewer | The exported JSON carries the original case and finding IDs, the technical verdict, per-service evidence and gaps, and provider follow-ups. It can be attached to the existing ticket and verified against the signed result. |

To try the handoff, run `setrya discovery init --output catalog-sample`. It writes a synthetic finding, a reviewed mapping, and a working Python fixture. Replace the sample with a disposable deployment of your app. A classification label needs an application-owned mapping before Setrya can identify which record to delete, rewrite, or retain. The report covers the services declared or observed in that drill. [Mapping format](https://github.com/davidahma/setrya/blob/v0.18.0/docs/discovery-mapping.md) · [Evidence and ticket handoff](https://github.com/davidahma/setrya/blob/v0.18.0/docs/discovery-receipt.md)

## Connect your application

Generate a starter in a test checkout.

```sh
setrya erasure init --output subject-test
```

In `subject-test/test_erasure.py`, connect `open_fixture()` to a disposable tenant. Seed one fact to erase and one to retain. Declare the store keys, the two authorized callers, and their questions. Wire `inspect` to the stores, `read` to the real answer endpoint, and `erase` to the operation the application already uses. Then run the connected starter.

```sh
setrya erasure run --starter subject-test/test_erasure.py --mode deletion --output first-run
setrya erasure replay first-run
```

For a queued writer, connect its hold and release hooks and repeat with `--mode late-write` and a fresh output directory. In CI, exit codes are `0` for PASS, `1` for FAIL, `2` for ERROR, and `3` for NOT TESTED. Reviewers who also gate on outstanding case items can use `setrya erasure replay first-run --case-review`. [Starter and CLI](https://github.com/davidahma/setrya/blob/v0.18.0/docs/erasure-cli.md) · [PostgreSQL, Elasticsearch, Redis, and async LangGraph adapters](https://github.com/davidahma/setrya/blob/v0.18.0/docs/erasure-adapters.md)

Setrya also tests access revocation, tenant offboarding, search freshness, and late cache fills. [Examples and Python SDK](https://github.com/davidahma/setrya/blob/v0.18.0/docs/other-drills.md)
