# Share an answer cache across Python processes

Use `RedisAnswerCache` when several web or worker processes serve the same answer cache. Each process opens a synchronous client to the same Redis primary and selects the same namespace. A hit includes the answer and the same ordered `AnswerDependencies` record used by [the local cache](local-cache.md).

Install the optional client with `python -m pip install 'setrya[redis]'`. Set `SETRYA_REDIS_URL` to your Redis connection URL. Use a dedicated standalone Redis 7.2+ primary with a memory budget for the working set and the `noeviction` policy. The answer, lease, and invalidation indices must expire together under the cache’s own rules. Application and Celery processes open their Redis clients after forking. Use your deployment’s authentication and TLS settings.

```python
import os

from redis import Redis

from setrya.local_cache import CacheKey
from setrya.redis_cache import RedisAnswerCache
from setrya.store_signals import passage_metadata

redis = Redis.from_url(os.environ["SETRYA_REDIS_URL"], socket_timeout=5)
cache = RedisAnswerCache(redis, namespace="support", ttl_seconds=600, lease_seconds=60)
key = CacheKey("workspace-a", "alice", "refunds", "refund-window-en-v1")

# In an application, query the authorized retriever inside this callback.
def retrieve():
    return [{"text": "Returns are accepted for 14 days.",
             "metadata": passage_metadata("refund-policy", "B", "terms")}]

hit = cache.fill(key, retrieve, lambda rows: rows[0]["text"],
                 metadata=lambda row: row["metadata"])
if hit is not None:
    print(hit.answer, hit.dependencies.markers)

# Run from any process connected to this namespace when this document changes.
cache.invalidate_document("workspace-a", "refund-policy")
redis.close()
```

A document update or deletion removes all cached answers that cite that document in the tenant. A fill that was already running can publish only if none of its inputs changed. A new document can change the top search result without appearing in an older answer. For that case, call `invalidate_retrieval(tenant, *affected_scopes)`. Use `invalidate_access(tenant, *affected_scopes)` when permissions change. Callers outside those scopes retain their cached answers.

## What happens to concurrent fills

The first miss acquires a Redis lease before retrieval. Other processes that miss the same key wait for that admission. Redis checks the owner, document changes and publication together. A revoked owner, a crashed process returning after lease expiry, or an expired admission cannot overwrite a newer answer. A manual `begin(key)` can refresh a good answer while it remains readable. Use `fill` for automatic coalescing and `cancel` to abandon a manual admission.

A waiter’s `wait_timeout` bounds its wait and leaves the owner running. When a lease expires, another request can start fresh. Set `lease_seconds` longer than the expected retrieval and generation time (up to one hour). If `fill` returns `None`, repeat authorization and retrieval or return an application-level unavailable result. Propagate Redis errors or use an independently authorized read path.

## Operating the cache

Answers expire after `ttl_seconds` (default one hour). Redis also expires reverse indices, abandoned leases and document-change markers. Invalidating a document removes its current dependents immediately, with work proportional to the affected entries and their sources. A change marker can outlive the last answer so older workers cannot restore it. A busy scope invalidation runs atomically on the primary; choose scope sizes and cache lifetimes that fit your Redis latency budget.

Redis stores the answer text. Keep its namespace and access controls appropriate for that data. Use a single writable primary for both reads and changes. Asynchronous replication, failover rollback and restoring old backups can lose a previous invalidation. After a rollback or restore, rebuild under a fresh namespace from authoritative permissions and content before serving cached answers. The cache cannot atomically commit your application database transaction and its Redis notification. Order and deliver those notifications as part of the application’s change protocol.

The live integration test starts independent Python processes that join a fill, then repeats the race with a document change between retrieval and publication. To run it against a disposable instance, export `SETRYA_REDIS_URL` and run `python -m unittest discover -s tests -p test_redis_cache.py -v` from a checkout.
