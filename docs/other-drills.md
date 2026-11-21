# Other release checks

| Release question | First command or guide |
| --- | --- |
| Can yesterday’s cache fill restore a revoked user’s private answer while a colleague retains access? | Install the core package and run `setrya demo access`. [Connect an existing endpoint](endpoint.md) · [Celery and LangChain](endpoint-framework.md) |
| Did two application revisions exercise the same failing case and fix it? | [Download a replayable FIX VERIFIED example](https://raw.githubusercontent.com/davidahma/setrya/v0.17.0/docs/examples/endpoint-fix/endpoint-fix.zip) · [Two passing revisions from v0.15.0](https://github.com/davidahma/setrya/releases/download/v0.15.0/endpoint-review.zip) · [PR workflow](pr-endpoint.md) |
| Can the old indexing worker restore the 30-day refund policy? | Run `setrya demo refund`. [Worker schedules](scenarios.md) · [pgvector](pgvector.md) |
| What happened to a departing tenant and its retained neighbour? | Run `setrya erasure demo --scope tenant --output tenant-run`. [Tenant policies, gate and receipts](offboarding.md) |
| How do I instrument an existing test or cache? | [Python recording SDK](recording.md) · [Local cache](local-cache.md) · [Redis cache](redis-cache.md) · [PR coverage](coverage.md) |
| What did the original subject-exclusive LangGraph drill show? | Extract the original recordings and run `setrya replay guarded` from the extracted directory. [v2 contract](erasure.md) · [Original signed recordings](https://github.com/davidahma/setrya/releases/download/v0.16.0/subject-erasure.zip) |
