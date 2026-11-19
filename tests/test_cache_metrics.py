"""Exercise live counters, local scraping and duplicate-safe evidence analysis."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from setrya.cache_metrics import CacheCounts, CacheMetrics, serve_metrics
from setrya.drill import InputError
from setrya.examples.answer_cache.drill import run
from setrya.examples.answer_cache.evidence import JOURNAL, MANIFEST, observation
from setrya.examples.answer_cache.metrics import bundle_metrics
from test_answer_cache_drill import SyntheticAnswers


class InstrumentedAnswers(SyntheticAnswers):
    def answer(self, caller, question):
        row = super().answer(caller, question)
        return dict(row, generation_skipped=row["cache_hit"])


class CacheMetricsTests(unittest.TestCase):
    def test_live_metrics_serve_atomic_aggregate_without_request_labels(self):
        metrics = CacheMetrics()
        self.assertIsNone(metrics.snapshot().hit_rate)
        with serve_metrics(metrics) as url:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i: metrics.observe_request(hit=True, stale=None, generation_skipped=False),
                              range(100)))
            metrics.observe_request(hit=False, stale=False)
            metrics.observe_request(hit=True, stale=True, generation_skipped=True)
            metrics.observe_request(hit=None, stale=None)
            metrics.rejected_fill()
            self.assertEqual(metrics.snapshot(), CacheCounts(requests=103, hits=101, misses=1, checked=2,
                             stale_serves=1, rejected_fills=1, skipped_generations=1, measured_generations=101))
            self.assertAlmostEqual(metrics.snapshot().hit_rate, 101/102)
            with urlopen(url, timeout=5) as response:
                body = response.read().decode()
                self.assertEqual(response.headers.get_content_type(), "text/plain")
            self.assertIn("setrya_cache_unverified_requests_total 101\n", body)
            self.assertIn("setrya_cache_stale_serves_total 1\n", body)
            self.assertIn("setrya_cache_measured_generations_total 101\n", body)
            with self.assertRaises(HTTPError) as error:
                urlopen(url + "/other", timeout=5)
            error.exception.close()
            self.assertEqual(error.exception.code, 404)
        for args in (dict(hit=False, stale=None, generation_skipped=True), dict(hit="no", stale=True)):
            with self.assertRaises(ValueError):
                metrics.observe_request(**args)
        self.assertEqual(metrics.snapshot().requests, 103)

    def test_counters_require_evidence_and_refuse_to_double_count_a_bundle(self):
        with TemporaryDirectory() as folder:
            broken, guarded, opaque = (Path(folder) / n for n in ("broken", "guarded", "opaque"))
            run(broken, "broken", application=InstrumentedAnswers, wait_seconds=0.05)
            run(guarded, "guarded", application=InstrumentedAnswers, wait_seconds=0.05)
            run(opaque, "guarded", application=type("MissingHTTP", (InstrumentedAnswers,), {"fault": "bad-http"}),
                wait_seconds=0.05)
            counts = bundle_metrics([broken, guarded]).snapshot()
            self.assertEqual(counts.requests, 18)
            self.assertEqual((counts.checked, counts.stale_serves, counts.rejected_fills), (18, 2, 1))
            self.assertEqual((counts.measured_generations, counts.skipped_generations), (18, counts.hits))
            self.assertEqual(bundle_metrics([broken, guarded]).snapshot(), counts)
            with self.assertRaisesRegex(InputError, "duplicate run"):
                bundle_metrics([guarded, guarded])
            unknown = bundle_metrics([opaque]).snapshot()
            self.assertEqual((unknown.requests, unknown.unverified, unknown.stale_serves), (9, 2, 0))
            self.assertIn("2 unverified", unknown.summary())

    def test_unwitnessed_release_cannot_count_a_rejected_late_fill(self):
        with TemporaryDirectory() as folder:
            out = Path(folder) / "guarded"
            run(out, "guarded", application=InstrumentedAnswers)
            path = out / MANIFEST
            manifest = json.loads(path.read_bytes())
            for missing in (True, None):
                with self.subTest(receipt_before_release=missing):
                    path.write_text(json.dumps(manifest | {"receipt_before_release": missing}))
                    counts = bundle_metrics([out]).snapshot()
                    self.assertEqual((counts.rejected_fills, counts.unverified), (0, 3))

    def test_coordinate_visibility_and_preserve_decisive_endpoint_leaks(self):
        with TemporaryDirectory() as folder:
            for fault, unverified, stale in (("unseen", 3, 0), ("wrong-user", 2, 0), ("body-leak", 0, 2)):
                with self.subTest(fault=fault):
                    directory = Path(folder) / fault
                    run(directory, "guarded", application=type("Probe", (InstrumentedAnswers,), {"fault": fault}),
                        wait_seconds=0.05)
                    counts = bundle_metrics([directory]).snapshot()
                    self.assertEqual((counts.unverified, counts.stale_serves), (unverified, stale))

    def test_v1_evidence_measures_freshness_without_inventing_generation_savings(self):
        with TemporaryDirectory() as folder:
            out = Path(folder) / "old"
            run(out, "guarded", application=InstrumentedAnswers)
            trace = json.loads((out / MANIFEST).read_bytes())
            journal = json.loads((out / JOURNAL).read_bytes())
            trace["observations"][0]["generation_skipped"] = None
            (out / MANIFEST).write_text(json.dumps(trace))
            with self.assertRaises(InputError):
                bundle_metrics([out])
            del trace["receipt_before_release"]
            trace["version"] = 1
            probes = [e for e in journal["events"] if e["kind"] == "probe"]
            for row, event in zip(trace["observations"], probes, strict=True):
                del row["generation_skipped"]
                event["origins"] = list(observation(row).origins)
            (out / JOURNAL).write_text(json.dumps(journal))
            trace["report_sha256"] = sha256((out / JOURNAL).read_bytes()).hexdigest()
            (out / MANIFEST).write_text(json.dumps(trace))
            counts = bundle_metrics([out]).snapshot()
            self.assertEqual((counts.checked, counts.stale_serves, counts.measured_generations), (9, 0, 0))
            self.assertGreater(counts.hits, 0)
            trace["version"] = 2
            (out / MANIFEST).write_text(json.dumps(trace))
            with self.assertRaises(InputError):
                bundle_metrics([out])
