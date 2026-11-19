# Can a removed OpenDesk agent still find a team’s new summary?

An OpenDesk workspace has two support agents, a team ticket, and a public ticket. The owner removes one agent from the team while OpenDesk is summarizing the team ticket. Both agents stay signed in to the same workspace and search for the same term.

Making the team private changed the release decision. The public ticket appeared in every search below. Each request returned HTTP 200.

| Deployment and caller | Before removal | Removed, worker still held | Worker committed |
| --- | --- | --- | --- |
| Workspace-readable team, departing agent | Initial summary | Initial summary | Recomputed summary |
| Workspace-readable team, retained colleague | Initial summary | Initial summary | Recomputed summary |
| Private team, departing agent | Initial summary | Public ticket only | Public ticket only |
| Private team, retained colleague | Initial summary | Initial summary | Recomputed summary |

OpenDesk enforces team visibility on the search path. The worker captured ticket content before removal and wrote the new summary afterward. The workspace-readable deployment failed while the worker was still held. Membership removal had never hidden the ticket from the departing agent. On the private deployment the ticket disappeared for that agent before release, and the colleague could still read the new summary after the write. Setrya returned **FIX VERIFIED** for the change in deployment policy.

## Reproduce both deployments

From a Setrya checkout with Git, Python 3.11+, and Docker Compose, run the following command. Choose a fresh output directory. The initial build downloads the pinned source and images. Each deployment starts in its own disposable Compose project with synthetic accounts and an internal network.

```sh
python3 -m trials.opendesk_revocation.run --output opendesk-revocation
```

Both lanes run OpenDesk v0.4.14 at source commit `779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5` with the same pinned Brotli build dependency. The trial verifies the executable digest. It creates two reproducible Git revisions of the deployment manifest and applies the `private` field through OpenDesk’s team API. The change under review is a team setting. OpenDesk’s application code is identical across lanes. [Pinned build recipe](../setrya/examples/opendesk/Dockerfile.trial) · [Deployment revisions](../trials/opendesk_revocation/policy.py)

## Review the decision offline

Install Setrya and unpack the [release bundle](https://github.com/davidahma/setrya/releases/download/v0.12.0/opendesk-revocation-review.zip) into an `opendesk-revocation` directory. A local trial or the workflow artifact produces the same files. Read `decision.txt` for the two event numbers. Then open `base-observations.json` and `candidate-observations.json` and compare the departing agent with the colleague at `membership-removed-worker-held` and `worker-completed`. The rows distinguish a newly recomputed summary from the initial one. [Trial runs and artifacts](https://github.com/davidahma/setrya/actions/workflows/opendesk-revocation.yml)

```sh
setrya review opendesk-revocation/base.json opendesk-revocation/candidate.json \
  --base-commit eb71707cbb47915bf35c93dbd360cee7390339ac --base-exit 1 \
  --candidate-commit 3da56d7b53089eb18144bfd9a305c0c4dae6fd40 --candidate-exit 0
```

## How the trial establishes overlap

1. Both signed-in agents find the initial summary through OpenDesk’s `GET /v1/search`. The trial adds four synthetic replies through the ticket API. The last reply contains a marker that distinguishes the pending summary from the initial one.
2. The fixture queues one native `ai.summarize` delivery directly in the disposable River queue to skip the periodic scheduler’s debounce. OpenDesk’s actual worker reads the ticket and calls a deterministic local model provider. The provider holds that call after it receives the marker. A read-only database observer checks that exactly one matching job is running on its first attempt.
3. The owner removes the agent through the team API. The trial confirms the remaining membership, queries both agents, and checks that the same job is still running and its provider call is still held before releasing it.
4. The observer matches the same completed job to the updated summary in PostgreSQL and reads the primary’s WAL position. The trial then renames the public ticket with a unique title. A caller’s final search carries write visibility only if that title appears in the same native ticket result set. The retained colleague must also see the recomputed summary itself. [Upstream summary worker](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/internal/ai/triage.go) · [Upstream caller-scoped search](https://github.com/useopendesk/opendesk/blob/779e28e5ff0ac59cda609edabefa1ecb9cc9c4b5/internal/helpdesk/tickets/reports.go)

A protected summary that reappears for the departing agent after the write earns **FAIL**, even if that agent’s search correctly hid it while the worker was held. An observed candidate failure remains visible when the base evidence is incomplete. Missing worker overlap, an ambiguous delivery, an unreadable search, or a missing control leaves the trial decision **NOT TESTED** when no candidate failure has been established. The evidence covers one caller pair, one query, and the top five tickets on this version of OpenDesk. The fixture exercises neither an external model service nor OpenDesk’s periodic job scheduling. The [workspace offboarding trial](../setrya/examples/opendesk/README.md) covers a separate operation.
