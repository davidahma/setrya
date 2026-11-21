# Follow one Macie finding through an AI investigation

Case `SEC-417` concerns a laptop identifier in a security analyst’s source. The source feeds a shared summary, a LangGraph checkpoint and two separate caches. Two authorized analysts can recover both the laptop identifier and a colleague’s credential reset by asking the same assistant. The case owner has approved deleting the laptop record and rebuilding the shared results. The colleague’s finding must survive.

Run the trial without AWS credentials or a model account. It reads a synthetic Macie EventBridge event from disk and operates on a fresh, local application for each run. Python 3.11 or later is required.

```sh
python -m pip install 'setrya[erasure]'
setrya discovery demo --output trial
```

| Run | What a reviewer can see | Engineering | Case review |
| --- | --- | --- | --- |
| `gap` | The catalog names an analyst handoff cache. Setup populated it, but the fixture never read it. The application declined the incomplete rewrite and the held writer was canceled. The result names `analyst-cache/handoff-41` and its owner, `case-platform`. | NOT TESTED | OPEN_ITEMS |
| `broken` | Both caches were checked. The old worker restored the laptop finding after deletion and both analysts recovered it through the real HTTP endpoint. | FAIL | RESIDUAL_FOUND |
| `guarded` | The worker checked its input revisions before saving. The laptop finding stayed absent from the checked stores and the answers. Both analysts kept the credential-reset finding in the same conversation. | PASS | OPEN_ITEMS |

All three runs use the same mapped source, environment, application revision, route, questions, callers, and list of required services. The lanes change the worker guard and which cache the fixture inspects. The second cache lives in a separate SQLite file. Reading the assistant’s cache cannot establish what the analyst’s handoff cache contains. During setup a synthetic classifier receives the seeded source through the same recording hook used with the Anthropic client. The journal records the call and source revision without the laptop text. A provider exhibit gives `provider-owner` a follow-up date. The classifier runs locally and the exhibit represents configuration in the fictional deployment.

## Give the result back to the case owner

Recompute the result against the finding and the mapping the case owner approved. Export writes the verified case result as JSON. Attach that file to the original case or have your case system ingest it. Keep the complete bundle for reviewers who need to replay the decision or follow its evidence links after the application shuts down.

```sh
setrya discovery replay trial/guarded \
  --finding trial/example/finding.json --mapping trial/example/mapping.json
setrya discovery export trial/guarded \
  --finding trial/example/finding.json --mapping trial/example/mapping.json \
  --output SEC-417.json
```

Look up `analyst-cache` in the result’s `services` array. Its evidence links identify the exact store readings before deletion, after the rewrite and after the worker finished. The result records both the Macie finding ID and the team’s own case ID, `SEC-417`. An export bound to another case fails. For a case gate use `setrya discovery replay trial/guarded --case-review`. The guarded fixture exits 3 because the provider item is still open. A successful file export exits 0 for any valid outcome.

To share an authenticated result, sign the case result and keep the detached signature beside the bundle. Provision the verification key separately.

```sh
setrya discovery sign --key cosign.key --receipt trial/guarded/case-result.json --bundle case.sig
setrya discovery verify --key cosign.pub --receipt trial/guarded/case-result.json --bundle case.sig
```

### Verify the published cases

The [release archive](https://github.com/davidahma/setrya/releases/download/v0.18.0/macie-case-review.zip) contains the three signed case bundles. A reviewer can recover the original synthetic finding and reviewed mapping from the matching PyPI package. For a customer's case, use the finding and mapping held in that customer's case system.

Start in a fresh directory with Cosign 3 installed. Set `TRUSTED_ROOT` to the path of a Sigstore root obtained independently of the case archive. [Trust-root setup](receipt-format.md)

```sh
python -m pip install 'setrya==0.18.0'
curl --fail --location --output macie-case-review.zip \
  https://github.com/davidahma/setrya/releases/download/v0.18.0/macie-case-review.zip
unzip -q macie-case-review.zip -d evidence
setrya discovery init --output approved
setrya discovery verify --receipt evidence/guarded/case-result.json \
  --bundle evidence/guarded/receipt.sigstore.json \
  --identity https://github.com/davidahma/setrya/.github/workflows/erasure.yml@refs/heads/main \
  --issuer https://token.actions.githubusercontent.com --trusted-root "$TRUSTED_ROOT"
setrya discovery replay evidence/guarded --finding approved/finding.json --mapping approved/mapping.json
setrya discovery export evidence/guarded --finding approved/finding.json --mapping approved/mapping.json \
  --output SEC-417.json
```

This review runs without LangGraph or a connection to the application. Replay the `broken` and `gap` directories to inspect the failure and the unread store. Their engineering exit codes are 1 and 3 respectively.

## Connect your own fixture

```sh
setrya discovery init --output connection
setrya discovery run --finding connection/finding.json --mapping connection/mapping.json \
  --fixture connection/fixture.py --mode late-write --output connected-run
```

The generated example is connected to the local synthetic application. To adapt it, start with one approved test case and a disposable tenant.

1. Replace `finding.json` with a single active Macie classification finding for a specific S3 object generation. The importer reads the finding ID, classification type, observation time and object reference. The normalized case uses digests for the object and finding. Keep the raw finding private. [AWS finding schema](https://docs.aws.amazon.com/macie/latest/user/findings-publish-event-schemas.html)
2. Have the case owner map the source to the test subject, approved handling, expected source revision and application commit. List both caches, the checkpoint, the answer route, and any external service that needs review. Give each entry an owner.
3. Replace `open_fixture(binding)` in `fixture.py` with a context manager that connects to your test deployment. Supply the runtime’s own subject, environment and application revision. Implement direct inspection and authenticated reads. In `erase_approved(subject, source, actions)`, compare the approved source revision at write time and change only authorized keys. The sample commits its source, checkpoint, summary and both cache changes in one SQLite transaction. In late-write mode, connect worker hold, release, and safe cancellation. [Shared erasure SDK](erasure-v3.md)
4. Rerun the fixture for the next change to that deployment and return its new bundle to the case. Each run needs a new output directory.

The bundled input follows the AWS event shape. The app models that object’s contents as a seeded SQLite record. The trial establishes how Setrya handles a pinned finding and a real application response across the declared services. AWS object deletion, platform-wide discovery, external ticket delivery, and provider-side erasure require their own integrations and evidence.
