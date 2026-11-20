# Cache an answer and invalidate it by document

`LocalAnswerCache` keeps an answer with the ordered chunk IDs used to generate it. On a miss, `fill()` reserves the key **before** calling your retrieval function. Concurrent misses for the same key wait for the owner’s answer.

```python
from setrya.local_cache import CacheKey, LocalAnswerCache
from setrya.store_signals import passage_metadata

cache = LocalAnswerCache(max_entries=256)
key = CacheKey("workspace-a", "alice", "policies", "refund-ceiling-en-v1")
rows = [{"text": "730 credits", "metadata": passage_metadata("refund-policy", "A", "0")}]

def retrieve():
    # In an app, authorize this caller and query the store here on every miss.
    return rows

def generate(chunks):
    return chunks[0]["text"]

hit = cache.fill(key, retrieve, generate, metadata=lambda chunk: chunk["metadata"])
assert hit.answer == "730 credits"
assert hit.dependencies.chunks[0].revision == "A"

# The application publishes revision B and delivers its change notification.
rows[:] = [{"text": "240 credits", "metadata": passage_metadata("refund-policy", "B", "0")}]
assert cache.invalidate_document("workspace-a", "refund-policy") == 1
hit = cache.fill(key, retrieve, generate, metadata=lambda chunk: chunk["metadata"])
assert hit.answer == "240 credits"
```

Pass a retrieval function that reads current sources under the caller’s permissions. Generating from a list retrieved before `fill()` loses the ordering guarantee. `generate` receives the retrieved items in order, as a tuple. For LangChain documents, use `metadata=lambda doc: doc.metadata`. The default accepts items containing the four passage metadata fields directly. Setrya snapshots those fields before calling the generator. [Dependency record format](answer-dependencies.md)

## Which change to report

| Change | Notification | Effect |
| --- | --- | --- |
| A cited document changed or was deleted | `invalidate_document(tenant, document)` | Drops every answer in the tenant that cites it across all access scopes, retrieval scopes and requests. An overlapping fill that cites the changed document cannot publish. Returns the number of cached answers removed. |
| A new or newly relevant passage could change retrieval ranking | `invalidate_retrieval(tenant, *scopes)` | Drops cached answers and pending fills in the affected retrieval scopes, including answers that never cited the changed source. On a move, supply both old and new scopes. |
| Someone lost access | `invalidate_access(tenant, *scopes)` | Drops cached answers and pending fills for those access scopes across the tenant’s retrieval routes. |

Authenticate and authorize every request before looking up its key. Derive the tenant and access scope from that authenticated state. A cache hit bypasses both callbacks, including any checks inside `retrieve()`. Give distinct permission sets distinct keys. Include the question, model, prompt, and settings that affect the answer in the request ID. The application must coordinate changes to its authoritative store and cache invalidation. Readers can reuse a previous answer until the change notification reaches the cache. A stale replica can still return an old revision to a fresh retrieval.

## Upgrading from 0.13

If your code calls `begin()` on every miss, switch to `fill()` to share the generation itself. Under 0.13 a later `begin()` superseded an earlier one. In 0.14 manual producers share the same admission and the first valid publication wins. Code using a bounded local cache can now receive `CacheCapacityError` when every slot is busy. Retry after an active fill finishes or return an unavailable result.

## Concurrent requests and refreshes

`fill()` returns a `CachedAnswer` on a hit or successful publication. It returns `None` if its admission was invalidated or cancelled, or the completed result was evicted before a waiter could read it. Discard that generation and repeat the full authorized retrieval on a subsequent attempt. An owner’s callback exception propagates to that request and releases the admission. Waiters return `None` when the admission disappears. A waiter that exceeds `wait_timeout` (default 30 seconds) raises `TimeoutError`; the owner continues. Work on other keys proceeds while retrieval and generation run.

To refresh a warm key in the background, authorize the request and call `begin(key)` **before** retrieving its sources, then `publish(token, answer, chunks=metadata)`. The previous answer remains readable until publication, invalidation, or eviction. Call `cancel(token)` if your manual producer abandons the fill. Only the original producer can cancel the admission. Cancelling a joined token returns `False` and lets the owner continue. Use `fill()` to avoid duplicate generation. A producer that already retrieved stale chunks before calling `begin()` cannot use a new token to establish when it read them.

The local cache holds at most `max_entries` slots, counting both warm answers and pending fills. When full, it evicts the least recently used idle answer. If every slot has an active fill, a request for a new key raises `CacheCapacityError` and the existing workers keep their admissions. The default maximum answer size is 65,536 UTF-8 bytes. If a pending fill spans more than 1,024 distinct document changes in its tenant, the local cache cancels it to bound its change history. Scope versions are opaque. Local instances share state only across threads using that same instance. For processes that share a cache, use the [Redis backend](redis-cache.md).
