"""Process-local counters for answer-cache requests, verification and worker outcomes."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread


@dataclass(frozen=True)
class CacheCounts:
    requests: int = 0
    hits: int = 0
    misses: int = 0
    checked: int = 0
    stale_serves: int = 0
    rejected_fills: int = 0
    skipped_generations: int = 0
    measured_generations: int = 0

    @property
    def unverified(self) -> int:
        return self.requests - self.checked

    @property
    def hit_rate(self) -> float | None:
        lookups = self.hits + self.misses
        return self.hits / lookups if lookups else None

    def summary(self) -> str:
        rate = self.hit_rate
        hit_rate = "unmeasured" if rate is None else f"{rate:.1%}"
        return "\n".join((
            f"{self.stale_serves} stale answers in {self.checked} checked requests "
            f"({self.unverified} unverified).",
            f"Cache lookups: {self.hits} hits, {self.misses} misses, "
            f"{self.requests - self.hits - self.misses} unknown. Hit rate {hit_rate}.",
            f"Generation skipped on {self.skipped_generations} of {self.measured_generations} "
            f"measured requests ({self.requests - self.measured_generations} unmeasured).",
            f"Rejected late fills: {self.rejected_fills}.",
        ))


class CacheMetrics:
    """Increment once per observed request or confirmed rejected worker fill."""

    def __init__(self):
        self._lock = Lock()
        self._counts = Counter()

    def observe_request(self, *, hit: bool | None, stale: bool | None,
                        generation_skipped: bool | None = None) -> None:
        """Use None for each signal that the instrumented path could not establish.

        stale=False requires a freshness check. generation_skipped=True requires a
        served cache hit whose execution path actually bypassed answer generation.
        """
        if any(value is not None and type(value) is not bool for value in (hit, stale, generation_skipped)):
            raise ValueError("request signals must be True, False or None")
        if generation_skipped is True and hit is not True:
            raise ValueError("a skipped generation must come from a confirmed cache hit")
        with self._lock:
            self._counts["requests"] += 1
            self._counts["hits"] += hit is True
            self._counts["misses"] += hit is False
            self._counts["checked"] += stale is not None
            self._counts["stale_serves"] += stale is True
            self._counts["measured_generations"] += generation_skipped is not None
            self._counts["skipped_generations"] += generation_skipped is True

    def rejected_fill(self) -> None:
        """Call once when the application confirms that it rejected a late write."""
        with self._lock:
            self._counts["rejected_fills"] += 1

    def snapshot(self) -> CacheCounts:
        with self._lock:
            return CacheCounts(**self._counts)

    @classmethod
    def combine(cls, snapshots):
        """Sum counts from disjoint observations, such as distinct saved runs."""
        combined = cls()
        for counts in snapshots:
            combined._counts.update(asdict(counts))
        return combined

    def prometheus(self) -> str:
        """Render Prometheus text exposition with bounded, aggregate series."""
        counts = self.snapshot()
        lines = []
        for name, help_text, value in (
            ("requests", "Answer requests observed.", counts.requests),
            ("hits", "Observed requests served from cache.", counts.hits),
            ("misses", "Observed cache misses.", counts.misses),
            ("checked_requests", "Requests checked for stale answers, including violations.", counts.checked),
            ("stale_serves", "Checked requests that served a stale answer.", counts.stale_serves),
            ("unverified_requests", "Requests without a conclusive freshness check.", counts.unverified),
            ("rejected_fills", "Confirmed rejections of late writes.", counts.rejected_fills),
            ("skipped_generations", "Measured requests that served a hit and skipped generation.",
             counts.skipped_generations),
            ("measured_generations", "Requests with a known generation decision.", counts.measured_generations),
        ):
            metric = f"setrya_cache_{name}_total"
            lines.extend((f"# HELP {metric} {help_text}", f"# TYPE {metric} counter", f"{metric} {value}"))
        return "\n".join(lines) + "\n"


@contextmanager
def serve_metrics(metrics: CacheMetrics, *, port: int = 0):
    """Serve a live GET /metrics endpoint on IPv4 loopback for the context's lifetime."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("use a local metrics port from 0 to 65535")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            payload = metrics.prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/metrics"
        finally:
            server.shutdown()
            thread.join()
