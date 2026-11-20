import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from setrya.drill import InputError
from setrya.local_cache import CacheCapacityError, CacheKey, LocalAnswerCache
from setrya.store_signals import passage_metadata


class LocalCacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = LocalAnswerCache()
        self.primary = CacheKey("t1", "alice", "refund", "q1")
        self.alternate = CacheKey("t1", "alice", "refund", "q2")
        self.delivery = CacheKey("t1", "alice", "delivery", "q1")
        self.colleague = CacheKey("t1", "bob", "refund", "q1")
        self.other = CacheKey("t2", "alice", "refund", "q1")
        self.sources = [passage_metadata("guide", "1", "0"), passage_metadata("refund", "A", "2")]

    def warm(self, key):
        fill = self.cache.begin(key)
        self.assertTrue(self.cache.publish(fill, key.request, chunks=self.sources))
        return self.cache.get(key)

    def test_versioned_record_preserves_exact_key_scopes_and_generation_order(self):
        hit = self.warm(self.primary)
        self.assertEqual(hit.dependencies.markers, tuple(s["setrya_marker"] for s in self.sources))
        other = self.warm(self.alternate)
        self.assertEqual((hit.dependencies.retrieval_version, hit.dependencies.access_version),
                         (other.dependencies.retrieval_version, other.dependencies.access_version))
        self.assertNotEqual(hit.dependencies.answer_id, other.dependencies.answer_id)
        self.assertIsNone(self.cache.get(self.colleague))
        self.assertIsNone(self.cache.get(self.other))

    def test_source_invalidation_covers_uncited_insert_and_preserves_unaffected_hits(self):
        keys = (self.primary, self.alternate, self.delivery, self.colleague, self.other)
        before = {key: self.warm(key) for key in keys}
        old = self.cache.begin(self.primary)
        self.assertEqual(self.cache.invalidate_retrieval("t1", "refund"), 3)
        self.assertFalse(self.cache.publish(old, "old", chunks=self.sources))
        for key in (self.primary, self.alternate, self.colleague):
            self.assertIsNone(self.cache.get(key))
        for key in (self.delivery, self.other):
            self.assertIs(self.cache.get(key), before[key])
        new = self.warm(self.primary)
        self.assertNotEqual(old.retrieval_version, new.dependencies.retrieval_version)

    def test_delete_or_move_and_access_revocation_remove_only_affected_slots(self):
        retained = self.warm(self.colleague)
        other_tenant = self.warm(self.other)
        self.warm(self.primary)
        old = self.cache.begin(self.delivery)
        self.assertEqual(self.cache.invalidate_access("t1", "alice"), 2)
        self.assertFalse(self.cache.publish(old, "old", chunks=self.sources))
        self.assertIs(self.cache.get(self.colleague), retained)
        self.assertIs(self.cache.get(self.other), other_tenant)
        self.warm(self.primary)
        self.warm(self.delivery)
        self.assertEqual(self.cache.invalidate_retrieval("t1", "refund", "delivery"), 3)
        self.assertIs(self.cache.get(self.other), other_tenant)

    def test_lru_and_one_shot_publication_fence_evicted_and_already_published_fills(self):
        self.cache = LocalAnswerCache(max_entries=2, max_answer_bytes=3)
        old = self.cache.begin(self.primary)
        joined = self.cache.begin(self.primary)
        self.assertEqual(joined.answer_id, old.answer_id)
        self.assertTrue(self.cache.publish(joined, "old", chunks=self.sources))
        self.assertFalse(self.cache.publish(old, "dup", chunks=self.sources))
        active = self.cache.begin(self.delivery)
        self.cache.get(self.primary)
        other = self.cache.begin(self.other)
        self.assertIsNone(self.cache.get(self.primary))
        self.assertFalse(self.cache.publish(old, "old", chunks=self.sources))
        self.assertEqual(self.cache.begin(self.delivery).answer_id, active.answer_id)
        with self.assertRaises(CacheCapacityError):
            self.cache.begin(self.colleague)
        with self.assertRaises(CacheCapacityError):
            self.cache.fill(self.colleague, self.fail, self.fail)
        self.assertTrue(self.cache.publish(active, "new", chunks=self.sources))
        self.assertTrue(self.cache.cancel(other))
        with self.assertRaises(InputError):
            self.cache.publish(self.cache.begin(self.primary), "éé", chunks=self.sources)

    def test_invalidation_while_materializing_inputs_rejects_queued_generation(self):
        fill = self.cache.begin(self.primary)
        entered, resume = Event(), Event()

        def chunks():
            entered.set()
            if not resume.wait(5):
                raise AssertionError("timed out waiting for the invalidation")
            yield from self.sources

        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(self.cache.publish, fill, "old", chunks=chunks())
            self.assertTrue(entered.wait(5))
            try:
                self.cache.invalidate_access("t1", "alice")
                replacement = self.warm(self.primary)
            finally:
                resume.set()
            self.assertFalse(job.result(timeout=5))
        self.assertIs(self.cache.get(self.primary), replacement)

    def test_document_change_evicts_citing_answers_across_scopes_and_fences_only_citing_fills(self):
        for key in (self.primary, self.colleague, self.delivery, self.other):
            self.warm(key)
        refresh = self.cache.begin(self.delivery)
        unrelated = self.cache.begin(self.alternate)
        other_tenant = self.cache.begin(self.other)
        self.assertEqual(self.cache.invalidate_document("t1", "refund"), 3)
        for key in (self.primary, self.colleague, self.delivery):
            self.assertIsNone(self.cache.get(key))
        self.assertIsNotNone(self.cache.get(self.other))
        self.assertFalse(self.cache.publish(refresh, "stale", chunks=self.sources))
        self.assertTrue(self.cache.publish(unrelated, "guide only", chunks=self.sources[:1]))
        self.assertTrue(self.cache.publish(other_tenant, "new generation", chunks=self.sources))
        self.assertEqual(self.cache.invalidate_document("t1", "refund"), 0)
        self.assertEqual(self.cache.invalidate_document("t1", "guide"), 1)

    def test_refresh_keeps_last_good_answer_until_replacement_or_invalidation(self):
        hit = self.warm(self.primary)
        self.assertTrue(self.cache.cancel(self.cache.begin(self.primary)))
        self.assertIs(self.cache.get(self.primary), hit)
        refresh = self.cache.begin(self.primary)
        self.assertIs(self.cache.fill(self.primary, self.fail, self.fail), hit)
        self.assertTrue(self.cache.publish(refresh, "guide only", chunks=self.sources[:1]))
        self.assertEqual(self.cache.invalidate_document("t1", "refund"), 0)
        self.assertEqual(self.cache.get(self.primary).answer, "guide only")
        self.assertEqual(self.cache.invalidate_document("t1", "guide"), 1)

    def test_earliest_concurrent_miss_generates_once_and_releases_every_waiter(self):
        entered = Event()
        resume = Event()
        waiting = Barrier(5, timeout=5)
        class WaitingCache(LocalAnswerCache):
            def _wait_for(self, fill, timeout):
                waiting.wait()
                return super()._wait_for(fill, timeout)
        cache = WaitingCache()
        def generate(chunks):
            self.assertEqual(tuple(self.sources), chunks)
            entered.set()
            self.assertTrue(resume.wait(5))
            return "first answer"
        with ThreadPoolExecutor(max_workers=5) as pool:
            owner = pool.submit(cache.fill, self.primary, lambda: self.sources, generate)
            self.assertTrue(entered.wait(5))
            waiters = [pool.submit(cache.fill, self.primary, self.fail, self.fail) for _ in range(4)]
            try:
                waiting.wait()
            finally:
                resume.set()
            hit = owner.result(5)
            self.assertEqual(hit.answer, "first answer")
            self.assertEqual([f.result(5) for f in waiters], [hit] * 4)

    def test_fill_reserves_before_retrieving_and_records_before_generating(self):
        def retrieve():
            self.cache.invalidate_document("t1", "guide")
            return self.sources
        self.assertIsNone(self.cache.fill(self.primary, retrieve, lambda _: "old"))
        self.assertIsNone(self.cache.get(self.primary))

        def mutate_inputs(items):
            items[0]["metadata"].update(passage_metadata("unrelated", "2", "0"))
            return "answer"
        items = [{"metadata": dict(self.sources[0])}]
        hit = self.cache.fill(self.primary, lambda: iter(items), mutate_inputs, metadata=lambda item: item["metadata"])
        self.assertEqual(hit.dependencies.chunks[0].document, "guide")
        self.assertEqual(self.cache.invalidate_document("t1", "guide"), 1)

    def test_cancelled_owner_wakes_waiters_and_timeout_leaves_owner_running(self):
        fill = self.cache.begin(self.primary)
        with self.assertRaises(TimeoutError):
            self.cache.fill(self.primary, self.fail, self.fail, wait_timeout=0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(self.cache._wait_for, fill, 5)
            self.assertTrue(self.cache.cancel(fill))
            self.assertIsNone(waiting.result(5))
        self.assertFalse(self.cache.publish(fill, "old", chunks=self.sources))
        def failed(_):
            raise ValueError("generation failed")
        with self.assertRaisesRegex(ValueError, "generation failed"):
            self.cache.fill(self.primary, lambda: self.sources, failed)
        self.assertEqual(self.cache.fill(self.primary, lambda: self.sources, lambda _: "fresh").answer, "fresh")

    def test_joined_manual_producer_cannot_end_a_healthy_automatic_fill(self):
        def retrieve():
            joined = self.cache.begin(self.primary)
            self.assertFalse(self.cache.cancel(joined))
            self.cache.invalidate_document("t1", "refund")
            self.assertFalse(self.cache.publish(joined, "outdated", chunks=self.sources))
            return self.sources[:1]

        hit = self.cache.fill(self.primary, retrieve, lambda _: "guide only")
        self.assertEqual(hit.answer, "guide only")
        self.assertEqual(self.cache.get(self.primary), hit)


if __name__ == "__main__":
    unittest.main()
