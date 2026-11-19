# Two workspaces on opendesk

This trial exercises the normal workspace offboarding path of [opendesk v0.4.14](https://github.com/useopendesk/opendesk/releases/tag/v0.4.14). It compares database observations with the view available to one support agent who belongs to both synthetic workspaces.

## Reproduce

Install this checkout with `python -m pip install .` in a Python 3.11+ environment and start Docker Compose v2. Run this from any directory with a new output path. From a source checkout, the command is also available as `python3 -m setrya offboarding opendesk`.

```sh
setrya offboarding opendesk --output opendesk-run
```

OpenDesk writes the source message and AI summary for both fictional workspaces. The runner uses a real support-agent account to read both summaries. A separate database role can read the message ID and summary field. The owner offboards Acme and the observer repeats the reads.

On the pinned build the CLI exits **3**. The two storage probes pass. The support agent still reads Birch, but OpenDesk returns HTTP 400 with `workspace_required` for Acme. The resulting agent-read check is **INCONCLUSIVE**. The summary jobs finished before deletion, so worker overlap is **NOT TESTED** and the corresponding gate check is **INCONCLUSIVE**. The raw trial outcome `COMPLETED` means that the trial collected the expected observations and cleaned up its environment.

The first run needs network access for two digest-pinned service images, a pinned Go toolchain, the upstream source archive and Go modules. Docker BuildKit builds the trial binary locally. Subsequent runs can reuse its build cache. A randomly named, isolated Compose project runs on an internal network without published ports, using ephemeral credentials and a local model stub. The runner allows 15 minutes for startup and downloads, 60 seconds for each Compose execution, and 90 seconds for cleanup. A cleanup failure identifies the project on stderr. All four evidence files are kept together for completed or interrupted observations. New runs need fresh output directories.

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

The trial builds the API and workers from the OpenDesk v0.4.14 source archive at commit `779e28e`. This Setrya build uses Brotli v1.2.7, the first release that fixes GO-2026-6888. The upstream release pinned v1.2.2. The web and admin frontends are outside this API trial and their browser bundles are omitted. The migrations, ticket storage, membership checks and deletion code come from the pinned source. [Build recipe](Dockerfile.trial) · [Upstream source](https://github.com/useopendesk/opendesk/tree/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5) · [Go advisory](https://pkg.go.dev/vuln/GO-2026-6888)

Setrya checks the build recipe, then hashes the executable in the running container before seeding either workspace. The report records the source commit, recipe hash, Brotli version, platform and verified binary hash. A startup failure can record the intended recipe without a verified binary. Archived observations from the publisher’s v0.4.13 and v0.4.14 images still identify their original digests.

## Trial image review

The Compose project accepts synthetic inputs on its own internal network and publishes no host ports. Each service runs as a non-root user with a read-only root filesystem, dropped Linux capabilities, and process and memory limits. PostgreSQL starts directly as its database OS user. The runner removes the containers and their data volumes after the trial.

The trial uses digest-pinned Chainguard PostgreSQL 18.7 and Python 3.15.2 images. OpenDesk's migration supports a database without the vector extension. The storage checks read the source message and the ticket summary by ID. Vector search is outside this trial's coverage. [Upstream migration](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/migrations/000501_ai_embeddings.up.sql) · [PostgreSQL image](https://images.chainguard.dev/directory/image/postgres/overview) · [Python image](https://images.chainguard.dev/directory/image/python/overview)

On November 19, 2026 the Syft inventories for both Linux amd64 and arm64 contained Brotli v1.2.7 in `/opendesk`. Grype reported these counts per architecture with that day's vulnerability database.

| Trial service | Critical | High | Other package matches |
| --- | ---: | ---: | --- |
| Setrya build of OpenDesk v0.4.14 | 0 | 0 | 1 Medium |
| PostgreSQL | 0 | 0 | 1 Medium, 1 Low |
| Python model stub | 0 | 0 | 4 Medium, 3 Low, 1 Negligible |

The image audit runs weekly, on demand and when a pull request changes the Compose images, build recipe, accepted binary hashes or audit. It builds each platform, checks the actual application binary hash and Brotli inventory, and scans all three runtime images. Every High or Critical match fails the gate. A failed scan or missing inventory also fails. Each run retains the full SBOMs and reports. [Trial image audits](https://github.com/davidahma/setrya/actions/workflows/offboarding-trial-images.yml)

Dependabot checks the two Compose registry images weekly. For a new OpenDesk release, review the source and toolchain pins in the build recipe, rebuild both architectures, and update their expected hashes. Qualify the live application and retain the existing report targets for offline review. [Compose pins](compose.yaml) · [Chainguard signature verification](https://www.chainguard.dev/docs/chainguard/containers/security-and-compliance/verifying-chainguard-images-and-metadata-signatures-with-cosign)
