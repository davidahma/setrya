import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from setrya.drill import InputError
from setrya.local_cache import CacheKey, LocalAnswerCache
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

    def test_lru_and_one_shot_publication_prevent_evicted_or_replaced_fill_from_writing(self):
        self.cache = LocalAnswerCache(max_entries=2, max_answer_bytes=3)
        old = self.cache.begin(self.primary)
        winner = self.cache.begin(self.primary)
        self.assertFalse(self.cache.publish(old, "old", chunks=self.sources))
        self.assertTrue(self.cache.publish(winner, "new", chunks=self.sources))
        self.assertFalse(self.cache.publish(winner, "dup", chunks=self.sources))
        evicted = self.cache.begin(self.delivery)
        self.cache.get(self.primary)
        self.cache.begin(self.other)
        self.assertFalse(self.cache.publish(evicted, "old", chunks=self.sources))
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


if __name__ == "__main__":
    unittest.main()
