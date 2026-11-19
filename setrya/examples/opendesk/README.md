# Two workspaces on opendesk

This trial exercises the normal workspace offboarding path of [opendesk v0.4.14](https://github.com/useopendesk/opendesk/releases/tag/v0.4.14). It compares database observations with the view available to one support agent who belongs to both synthetic workspaces.

## Reproduce

Install this checkout with `python -m pip install .` in a Python 3.11+ environment and start Docker Compose v2. Run this from any directory with a new output path. From a source checkout, the command is also available as `python3 -m setrya offboarding opendesk`.

```sh
setrya offboarding opendesk --output opendesk-run
```

OpenDesk writes the source message and AI summary for both fictional workspaces. The runner uses a real support-agent account to read both summaries. A separate database role can read the message ID and summary field. The owner offboards Acme and the observer repeats the reads.

On the pinned build the CLI exits **3**. The two storage probes pass. The support agent still reads Birch, but OpenDesk returns HTTP 400 with `workspace_required` for Acme. The resulting agent-read check is **INCONCLUSIVE**. The summary jobs finished before deletion, so worker overlap is **NOT TESTED** and the corresponding gate check is **INCONCLUSIVE**. The raw trial outcome `COMPLETED` means that the trial collected the expected observations and cleaned up its environment.

The first run pulls three images pinned by digest. A randomly named, isolated Compose project runs on an internal network without published ports, using ephemeral credentials and a local model stub. The runner allows 15 minutes for startup and downloads, 60 seconds for each Compose execution, and 90 seconds for cleanup. A cleanup failure identifies the project on stderr. All four evidence files are kept together for completed or interrupted observations. New runs need fresh output directories.

## Inspect or replay the saved evidence

```sh
setrya offboarding opendesk --verify-report opendesk-run/opendesk.json
setrya offboarding opendesk --from-report opendesk-run/opendesk.json --output opendesk-replay
```

`--verify-report` checks the raw report against the pinned metadata, observation schema, and recorded outcome, returning 0 for a valid report. `--from-report` uses that report's matching v1 or v2 policy and runs the gate, returning its decision code. Both operations work offline. Historical v0.4.13 reports from Axiom and Setrya v0.7.0 retain their original commit and digest during replay. The command exits 2 for invalid input or output, and also identifies startup or cleanup failures with exit 2 unless a residual establishes FAIL.

The output contains the raw `opendesk.json`, the scope policy, the observations bound to the upstream commit and report digest, and the decision. You can re-run only the gate:

```sh
setrya offboarding gate --policy opendesk-run/opendesk-policy.json \
  --observations opendesk-run/opendesk-observations.json --output opendesk-replay-decision.json
```

For a historical example, [observed.json](observed.json) is a recorded v1 run. The v1 storage probe checked for a summary key and its agent probe compared the complete answer. The v2 policy requires new marker-based measurements. Replay preserves the original scope identifiers. Compare the [v1 policy](policy.json) and [v2 policy](policy-v2.json).

## Observations

| Step | What actually executes |
| --- | --- |
| Create | The trial registers a workspace owner and a separate support agent. The owner enables MFA and authenticates with a one-time recovery code. The owner creates Acme and Birch through opendesk, invites the same support account to both as an agent, and the agent accepts each invitation. |
| Generate | In each workspace the owner enters a synthetic inbound ticket. The agent invokes the ordinary ticket-summary endpoint. The local model stub recognises the fixture phrase in the model request and returns deterministic structured content. opendesk processes and persists it. |
| Establish the baseline | Using the real agent account, the trial reads both ticket summaries. A separate SQL role checks the source message by primary key and reads the summary field on the named ticket. The storage and agent reads look for each tenant's synthetic marker in the summary text and key points. The SQL role has column-level SELECT grants. |
| Offboard | Only after the four positive storage observations and both agent reads succeed, the MFA-authenticated owner invokes opendesk's workspace delete endpoint for Acme. The database observer performs no deletion. |
| Read again | Repeat both SQL lookups and both agent calls. Detection of a surviving Acme row, stored summary, or served synthetic answer produces **FAIL**. Inconclusive setup, a lost Birch control, or a cleanup failure makes the command exit unsuccessfully. The report uses fixed field names, roles and enumerated outcomes in place of customer content. |

Version 2 records each read separately. A read error marks that read inconclusive, and an unattempted read stays untested. A later timeout cannot erase an earlier observed leftover. On a successful HTTP response the observer checks that the ticket ID matches and `custom` is an object. A missing, null or empty summary can establish absence on that ticket. Unexpected response shapes and unrelated nonempty summary text remain inconclusive. On either surface, the fixture marker in a longer summary still counts as present. These checks follow opendesk's [ticket response type](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/internal/domain/types.go) and [summary writer](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/internal/ai/features.go).

The native summary is stored on the ticket. Its source in this trial is a separate inbound message. This measures logical presence at those recorded identifiers. Copies in other tables, search indexes, backups, physical storage, and other application paths are outside its coverage. Both agent-initiated summary requests finish before offboarding. **Upstream worker overlap is NOT TESTED.** Setrya’s [delayed-worker demo](../../../docs/offboarding.md) exercises that case separately.

## Upstream boundary

The trial runs the published multi-architecture image from [the pinned release](https://github.com/useopendesk/opendesk/releases/tag/v0.4.14). The release records the image digest in `images.json`. The application source, database migrations, ticket save, membership checks, and deletion implementation come from upstream. Fixture data, the model substitute, the read-only observer, and the report are supplied by Setrya. For the exact behavior at this revision, see the [summary writer](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/internal/ai/features.go), [workspace API](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/internal/core/workspaces.go), and [offboarding operation](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/internal/core/ops.go).

## Trial image review

The Compose project accepts synthetic inputs on its own internal network and publishes no host ports. Each service runs as a non-root user with a read-only root filesystem, dropped Linux capabilities, and process and memory limits. PostgreSQL starts directly as its database OS user. The runner removes the containers and their data volumes after the trial.

The trial uses digest-pinned Chainguard PostgreSQL 18.7 and Python 3.15.2 images. OpenDesk's migration supports a database without the vector extension. The storage checks read the source message and the ticket summary by ID. Vector search is outside this trial's coverage. [Upstream migration](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/migrations/000501_ai_embeddings.up.sql) · [PostgreSQL image](https://images.chainguard.dev/directory/image/postgres/overview) · [Python image](https://images.chainguard.dev/directory/image/python/overview)

The November 19, 2026 Grype inventory for these image digests returned the following package-level matches. The same counts were recorded on both Linux amd64 and arm64 for the pinned images. The earlier inventory covered amd64.

| Trial service | Previous images, Critical / High | Pinned digests, Critical / High | Other matches per architecture on that date |
| --- | --- | --- | --- |
| OpenDesk v0.4.14 | 1 / 0 | 1 / 0 | 1 Medium |
| PostgreSQL | 12 / 206 | 0 / 0 | 1 Medium, 1 Low |
| Python model stub | 0 / 78 | 0 / 0 | 4 Medium, 3 Low, 1 Negligible |

**OpenDesk and Brotli.** The released application includes Brotli v1.2.2. The advisory affects the decoder's `Reader.Read` method and lists v1.2.7 as the first fixed release. PostHog uses the Brotli writer to compress outbound telemetry. For linux/amd64 and linux/arm64, `go list -deps` identified PostHog as the only direct importer in the OpenDesk command. The Go function tables in both published binaries contained the encoder and had no entry for `(*Reader).Read`. Keep the package finding visible and reassess it whenever the OpenDesk image changes. [Go advisory](https://pkg.go.dev/vuln/GO-2026-6888) · [OpenDesk dependency pin](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/go.mod#L58) · [PostHog encoder](https://github.com/PostHog/posthog-go/blob/v1.42.4/request_compression.go#L42-L52)

**Image updates.** Dependabot checks the Compose images weekly. For an image update, verify the publisher and multi-architecture digest, review the inventory for both platforms, and run the full offboarding trial. The runner checks the OpenDesk image against the target declared in its report before starting the services. When updating that target, preserve replay of evidence recorded against earlier releases. [Compose image pins](compose.yaml) · [Chainguard signature verification](https://www.chainguard.dev/docs/chainguard/containers/security-and-compliance/verifying-chainguard-images-and-metadata-signatures-with-cosign)

The [trial image audit](https://github.com/davidahma/setrya/actions/workflows/offboarding-trial-images.yml) runs weekly, on demand, and on pull requests that change the image pins or the audit. It saves the Syft package inventory, full Grype report and High/Critical findings for each image and architecture. A new High or Critical match fails the audit. The reviewed Brotli match is displayed as `KNOWN` only for the recorded image digest, module version and binary path. A scanner or database failure also fails the job.
