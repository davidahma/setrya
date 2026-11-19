# A process-local answer cache

`LocalAnswerCache` stores answer text and the ordered chunk versions passed to generation. It uses the Python standard library. Use it to exercise invalidation in a local application or a regression test.

```python
from setrya.local_cache import CacheKey, LocalAnswerCache
from setrya.store_signals import passage_metadata

cache = LocalAnswerCache(max_entries=32)
key = CacheKey("workspace-a", "alice", "refunds", "refund-ceiling-en-v1")

# Reserve the fill before the application checks permissions and retrieves chunks.
original = cache.begin(key)
sources = [passage_metadata("refund-policy", "A", "0")]

# A source change arrives while that generation is running.
cache.invalidate_retrieval("workspace-a", "refunds")
published = cache.publish(original, "730 credits", chunks=sources)
assert published is False
assert cache.get(key) is None

fresh = cache.begin(key)
sources = [passage_metadata("refund-policy", "B", "0")]
published = cache.publish(fresh, "240 credits", chunks=sources)
assert published is True
hit = cache.get(key)
assert hit.answer == "240 credits"
assert hit.dependencies.chunks[0].revision == "B"

cache.invalidate_access("workspace-a", "alice")
assert cache.get(key) is None
```

Derive the tenant and access scope from the authenticated request. Give each distinct permission set its own scope. The request ID covers everything else that can change the answer, including the normalized question, model, prompt and settings. A digest of that input works as a short request ID.

On an insert, update or deletion, invalidate each retrieval scope whose results could change. A new higher-ranked source can displace an answer’s cited chunks. A move between scopes can be handled in one call, for example `cache.invalidate_retrieval("workspace-a", "refunds", "delivery")`. A revocation affects every retrieval scope for that caller. Invalidation returns the number of cached or pending slots removed.

A fill can publish once. Invalidation, eviction, or starting a newer fill for the same key causes its publication to return `False`. Discard the generated answer on that path and repeat authorization and retrieval before serving a response. The cache holds at most `max_entries` slots, counting in-flight fills. Each stored answer defaults to a 65,536-byte limit. Dependency records use the same format as the PostgreSQL example. [Answer dependency records](answer-dependencies.md)

Cache scope versions are opaque and local to one cache instance. A scope receives a new version when its last slot has been invalidated or evicted and a caller starts a new fill. Threads share the cache’s lock. The application owns change notification and must coordinate changes to its source of truth with invalidation. Separate processes have independent state. The PostgreSQL example places the version guard and cache write in a database transaction and checks the result through the answer endpoint. [PostgreSQL answer-cache drill](answer-cache.md)
