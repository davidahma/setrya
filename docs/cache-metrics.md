# Measure an answer cache

A revoked caller can receive a stale answer on a fast cache hit. A retained caller can receive a correct answer after an unnecessary cache miss. Setrya counts what the endpoint served, whether the drill could check it, and whether the cache spared a generation.

## Inspect a saved run

Keep the whole output directory from a late-fill or selective-invalidation drill. Pass one or more directories to the installed CLI.

```sh
setrya cache-metrics out/broken out/guarded
setrya cache-metrics out/guarded --prometheus
setrya cache-metrics out/guarded --listen 9464
```

The last command serves a fixed snapshot at `http://127.0.0.1:9464/metrics` until Ctrl-C. Replaying or scraping the same run leaves the counts unchanged. Supplying two copies of the same run ID returns an input error. The command exits successfully when it can measure the evidence, including evidence of a stale answer. Use the drill's replay command for its CI verdict.

| Measure | What counts |
| --- | --- |
| Hit rate | Hits divided by hits plus misses. Requests without a known lookup outcome are reported separately. |
| Stale answers | Checked requests that served forbidden content or stale dependencies. A response can be stale on either a hit or a miss. |
| Checked and unverified requests | A checked request has evidence for a fresh or stale decision under that drill. A missing caller, unreadable response or inconclusive observation remains unverified. Read the stale count alongside both denominators. |
| Skipped generations | A confirmed cache hit that actually bypassed answer generation. A hit with no generation signal contributes to the hit rate only. The generation denominator includes only observed decisions. |
| Rejected late fills | Confirmed rejections of writes by the stale worker. A rejected fill is a worker outcome and can protect several subsequent requests. |

The generation count measures work avoided in the observed endpoint. Estimating a monetary saving takes the model, token usage and price for the calls a hit displaced. Late-fill evidence recorded before the generation signal was introduced still replays and reports the generation decision as unmeasured.

## Instrument a running service

Create one counter per process. Call `observe_request` once per completed request, including requests whose freshness you could not check. Use `None` for each unknown signal. Call `rejected_fill` once when the write guard confirms a late fill was rejected.

```python
from setrya.cache_metrics import CacheMetrics, serve_metrics

metrics = CacheMetrics()

# In the branch that actually serves a cached answer without generating:
metrics.observe_request(hit=True, stale=None, generation_skipped=True)

# In a drill, after the endpoint serves a checked fresh answer on a miss:
metrics.observe_request(hit=False, stale=False, generation_skipped=False)

# In the worker branch that rejects the stale write:
metrics.rejected_fill()

# Run this alongside your service for a live scrape endpoint:
with serve_metrics(metrics, port=9464) as url:
    run_application()  # your service's blocking entry point
```

An existing HTTP server can mount `metrics.prometheus()` at `/metrics` as `text/plain; version=0.0.4; charset=utf-8`. The built-in server binds to IPv4 loopback. Both exporters expose aggregate counters with no caller IDs, answer text or per-query labels. Process-local counters reset on restart. [Prometheus exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/)

For live traffic, this PromQL expression gives the five-minute hit rate across scraped processes that saw known lookups. A static drill snapshot already includes its hit rate in the CLI summary.

```promql
sum(rate(setrya_cache_hits_total[5m]))
/
(sum(rate(setrya_cache_hits_total[5m])) + sum(rate(setrya_cache_misses_total[5m])))
```
