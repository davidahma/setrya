# Check deletion through a provider API

Use a disposable project containing a synthetic target and a separate record that should survive. Setrya reads both before the deletion. Apply the deletion through your normal test workflow, then have Setrya read the same IDs under the same credentials and sign the result. No application fixture is required.

A PASS covers the API view and records the boundaries left open. A missing record in an exhausted listing can establish absence from that listing. A failed direct read, including a 404, can reflect permissions or availability and is reported as NOT TESTED. Provider-internal storage, traces, backups and re-keyed copies require separate observations.

## Run the two reads

Install Setrya and Cosign 3. Use an independently provisioned test-project key and avoid placing credentials in command-line arguments. For this example, load an OpenAI test-project key into `SETRYA_OPENAI_API_KEY` and set `VECTOR_STORE_ID`, `TARGET_FILE_ID`, and `RETAINED_FILE_ID` to test resources.

```sh
setrya conformance baseline --target openai-vector-files --synthetic \
  --scope "$VECTOR_STORE_ID" --record "$TARGET_FILE_ID" --retain "$RETAINED_FILE_ID" \
  --output baseline.json
```

Once your workflow has removed the target attachment, leave the control attached and take a new read.

```sh
setrya conformance readback baseline.json --after-delete \
  --scope "$VECTOR_STORE_ID" --record "$TARGET_FILE_ID" --retain "$RETAINED_FILE_ID" \
  --key fixture.key --output provider-result
setrya verify --receipt provider-result/erasure-receipt.json \
  --bundle provider-result/conformance.sig --key fixture.pub
setrya replay provider-result
```

The baseline is an issuer-local checkpoint. Keep it separate from what you publish. Setrya enforces continuity of credentials, surface, scope, target and control. The signed output includes the scope fingerprint, API label and both observation times. It excludes credentials, provider record IDs and response contents. `--after-delete` records that an operator reported deletion. Verify the application operation or case ticket separately if the review requires it. Obtain the signing public key or expected keyless signer from an independent trust source.

## Hand off the API observation

```sh
setrya receipt pack provider-result --output provider-exchange \
  --system synthetic-provider --system-version v1 --key issuer.key
setrya receipt verify-exchange provider-exchange --key independently-trusted.pub
```

The new exchange records `provider-readback`, the exact API scope digest, both times, and the provider follow-ups. A reviewer can attach the exchange and its verified digest to a governance ticket or export the observed API surface to OSCAL. The provider exchange carries API observations. A ticket binding requires independent completed-deletion evidence. [Governance handoff](governance-handoff.md#attach-a-provider-api-result) · [OSCAL export](oscal.md)

## Available reads

| Target | Visible surface | `--scope` | Credential |
| --- | --- | --- | --- |
| `openai-conversations` | Exact conversation metadata | Omit | `SETRYA_OPENAI_API_KEY` |
| `openai-conversation-items` | Exact ID in the conversation's item listing | Conversation ID | Same |
| `openai-responses` | Exact stored response | Omit | Same |
| `openai-vector-files` | Exact file ID attached to one vector store | Vector store ID | Same |
| `openai-files` | Exact uploaded file ID listed in the project | Omit | Same |
| `anthropic-files` | Exact Files API metadata ID listed in the workspace | Omit | `SETRYA_ANTHROPIC_API_KEY` |
| `anthropic-sessions` | Exact Managed Agents session ID listed in the workspace | Omit | Same |
| `anthropic-session-events` | Exact event ID listed in one Managed Agents session | Session ID | Same |
| `bedrock-sessions` | Exact Bedrock session ID listed under the authorized principal | AWS Region | AWS SDK credentials |
| `bedrock-invocation-steps` | Exact invocation step ID listed in one Bedrock session | `REGION:SESSION_ID` | Same |
| `letta-archival` | Exact passage ID listed in an agent's archival memory | Agent ID | `SETRYA_LETTA_API_KEY` |
| `mem0-hosted` | Exact memory record | Omit | `SETRYA_MEM0_API_KEY` |
| `mem0-hosted-history` | Text or input fields returned by an exact memory history read | Omit | Same |

For Bedrock install `setrya[crypto-aws]` and select an authorized test principal with STS identity and the applicable Bedrock list operation. Setrya calls AWS SDK list methods, which AWS transports as POST requests. The HTTP adapters use GET, a fixed vendor origin, and no redirects. Listings stop after 10 pages of 50 results. A malformed page, repeated cursor or exceeded budget leaves an absence undetermined.

The OpenAI conversation, item, vector attachment and uploaded file reads cover separate API resources. Deleting a conversation leaves deletion of its items to the caller. Removing a vector attachment leaves deletion of the uploaded file to the caller. A detached file can also remain searchable briefly while the index catches up. [Conversation API](https://developers.openai.com/api/reference/typescript/resources/conversations) · [Responses API](https://developers.openai.com/api/reference/typescript/resources/responses) · [Vector stores](https://developers.openai.com/api/docs/guides/retrieval) · [Files API](https://developers.openai.com/api/reference/resources/files/methods/list/)

Claude file metadata and Managed Agents events are separate reads. An expired file can retain metadata after content becomes unavailable. The Bedrock checks apply to Session Management and the selected scope. [Claude Files API](https://platform.claude.com/docs/en/build-with-claude/files) · [Managed Agents events](https://platform.claude.com/docs/en/api/beta/sessions/events/list) · [Bedrock Session Management](https://docs.aws.amazon.com/bedrock/latest/userguide/sessions.html)

Letta's archival read checks passage IDs and leaves agent messages and other stores open. Mem0 OSS and hosted Mem0 use distinct trials. [Letta passages](https://docs.letta.com/api/python/resources/agents/subresources/passages/methods/list) · [Mem0 Platform API](https://github.com/mem0ai/mem0/blob/main/skills/mem0/references/api-reference.md)

## Release evidence

This release tests the hosted adapters against local, simulated API responses, including successful reads, partial listings, retained targets and ambiguous errors. The hosted services themselves are unmeasured. The separate [vendor results](vendor-conformance.md) and [Graphiti trial](graphiti-trial.md) record native local observations.
