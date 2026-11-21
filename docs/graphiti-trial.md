# Check selective deletion on two Graphiti revisions

Graphiti links source episodes to entity nodes and keeps summaries on those nodes. This trial asks whether removing one episode updates a node shared with a second episode. It seeds two fictional findings into a disposable Neo4j database, links both to the same investigation, and calls Graphiti’s native `remove_episode`. Setrya records the episode rows, the shared summary, their revision hashes and four reads per stage through a small two-user ASGI application.

| Graphiti source revision | Target episode after deletion | Shared summary when reopened | Technical verdict |
| --- | --- | --- | --- |
| [0.31.1 / 5961293](https://github.com/getzep/graphiti/commit/5961293865a57844fe13d4eb061deb3501971fb2) | Absent | Both analysts could still recover the quarantined laptop. The retained credential reset also remained. | `FAIL` |
| [0.31.2 / d72407d](https://github.com/getzep/graphiti/commit/d72407dcedac911e1be9fc15b55e8c05a3efcc36) | Absent | Both analysts lost the laptop finding and could still recover the credential reset from the same node. | `PASS` |

## Reproduce from the checkout

Use Python 3.11+, Git and Docker. Choose a new directory. The script installs each pinned upstream revision and Setrya from the current checkout in separate environments, and starts its own loopback Neo4j instance. A deterministic synthetic summarizer responds to the actual upstream regeneration prompt. The fixture uses no model-service credentials.

```sh
bash trials/graphiti/run.sh graphiti-run
setrya replay graphiti-run/0.31.2.receipt
```

The script verifies the installed Git commit and requires the native observations to agree with the live Setrya verdict, `FAIL` for the old revision and `PASS` for the fix. It then checks for exit 1 and 0 respectively on replay. Each lane writes its native before/after observations in `<version>.json` and a portable Setrya v3 evidence bundle in `<version>.receipt/`. A reviewer with only core Setrya can replay either bundle after the database shuts down.

The source-to-node relationships and the summary under review come from Graphiti. The small ASGI answer route belongs to the trial and authenticates two synthetic callers. This deletion-only test covers the shared-node regeneration behavior of those open-source revisions. It leaves worker races unexercised. Hosted Zep deployments and customer reuse are separate trials. [Zep’s published deletion behavior](https://help.getzep.com/deleting-data-from-the-graph)
