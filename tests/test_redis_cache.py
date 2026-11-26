"""Cross-process cache invariants against a disposable Redis primary."""

from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
import os
from threading import Event
from time import monotonic, sleep
import unittest
from uuid import uuid4

from setrya.local_cache import CacheKey
from setrya.redis_cache import RedisAnswerCache
from setrya.store_signals import passage_metadata


URL = os.environ.get("SETRYA_REDIS_URL")
KEY = CacheKey("org", "alice", "policy", "question")
POLICY = passage_metadata("policy", "A", "one")
GUIDE = passage_metadata("guide", "B", "one")


def _participant(url, namespace, role, entered, resume, result):
    from redis import Redis

    class Waiter(RedisAnswerCache):
        def _wait_for(self, fill, timeout):
            entered.set()
            return super()._wait_for(fill, timeout)

    with Redis.from_url(url, decode_responses=role == "waiter") as client:
        cls = Waiter if role == "waiter" else RedisAnswerCache
        cache = cls(client, namespace=namespace)

        def retrieve():
            if role != "owner":
                raise AssertionError("a follower started retrieval")
            entered.set()
            if not resume.wait(10):
                raise TimeoutError("parent did not release test owner")
            return [POLICY]

        hit = cache.fill(KEY, retrieve, lambda inputs: "shared")
        result.put((hit.answer, hit.dependencies.answer_id) if hit else None)


@unittest.skipUnless(URL, "set SETRYA_REDIS_URL to exercise the Redis integration")
class RedisCacheTests(unittest.TestCase):
    def setUp(self):
        from redis import Redis
        self.client = Redis.from_url(URL)
        self.addCleanup(self.client.close)
        self.namespace = uuid4().hex
        self.cache = RedisAnswerCache(self.client, namespace=self.namespace)
        self.addCleanup(self._clean)

    def _clean(self):
        keys = list(self.client.scan_iter(match=f"setrya:cache:v1:{self.namespace}:*"))
        if keys:
            self.client.unlink(*keys)

    def _warm(self, key, doc=POLICY):
        return self.cache.fill(key, lambda: [doc], lambda chunks: key.request)

    def test_processes_share_one_generation_and_an_invalidation_fences_it(self):
        ctx = get_context("spawn")
        for invalidate in (False, True):
            with self.subTest(invalidate=invalidate):
                self.cache.invalidate_access("org", "alice")
                resume, result = ctx.Event(), ctx.Queue()
                participants = []
                try:
                    for role in ("owner", "waiter", "waiter"):
                        entered = ctx.Event()
                        process = ctx.Process(target=_participant, args=(
                            URL, self.namespace, role, entered, resume, result,
                        ))
                        process.start()
                        participants.append(process)
                        self.assertTrue(entered.wait(10), f"{role} did not join the fill")
                    if invalidate:
                        self.assertEqual(self.cache.invalidate_document("org", "policy"), 0)
                    resume.set()
                    for process in participants:
                        process.join(10)
                        self.assertEqual(process.exitcode, 0)
                    rows = [result.get(timeout=2) for _ in participants]
                    if invalidate:
                        self.assertEqual(rows, [None] * len(rows))
                        self.assertIsNone(self.cache.get(KEY))
                    else:
                        self.assertEqual(len(set(rows)), 1)
                        self.assertEqual(rows[0], ("shared", self.cache.get(KEY).dependencies.answer_id))
                finally:
                    resume.set()
                    for process in participants:
                        if process.is_alive():
                            process.terminate()
                        process.join(5)
                        process.close()
                    result.close()
                    result.join_thread()

    def test_document_invalidation_tracks_replaced_dependencies_and_tenant(self):
        bob = CacheKey("org", "bob", "shipping", "shipping")
        other = CacheKey("other", "alice", "policy", "question")
        for key in (KEY, bob, other):
            self._warm(key)
        old_answer = self.cache.get(KEY)
        refresh = self.cache.begin(KEY)
        self.assertEqual(self.cache.get(KEY), old_answer)
        self.assertTrue(self.cache.publish(refresh, "guide", chunks=[GUIDE]))
        self.assertEqual(self.cache.invalidate_document("org", "policy"), 1)
        self.assertEqual(self.cache.get(KEY).answer, "guide")
        self.assertIsNone(self.cache.get(bob))
        self.assertIsNotNone(self.cache.get(other))
        self.assertEqual(self.cache.invalidate_document("org", "guide"), 1)
        self.assertIsNone(self.cache.get(KEY))

    def test_document_fence_only_rejects_older_dependent_reservations(self):
        a = self.cache.begin(KEY)
        other_key = CacheKey("org", "alice", "policy", "other-question")
        b = self.cache.begin(other_key)
        joined = self.cache.begin(other_key)
        self.assertEqual((b.answer_id, b.retrieval_version), (joined.answer_id, joined.retrieval_version))
        self.cache.invalidate_document("org", "policy")
        self.assertTrue(self.cache.publish(joined, "guide", chunks=[GUIDE]))
        self.assertFalse(self.cache.publish(b, "duplicate", chunks=[GUIDE]))
        self.assertFalse(self.cache.publish(a, "old", chunks=[POLICY]))
        self.assertIsNone(self.cache.get(KEY))
        self.assertIsNotNone(self._warm(KEY, passage_metadata("policy", "B", "one")))

    def test_admission_survives_an_idle_period_without_resetting_its_document_clock(self):
        # Move a previously-used tenant's clock near expiration before admission.
        # A reset while this lease is live could compare epoch 1 against epoch 2.
        for _ in range(2):
            self.cache.invalidate_document(KEY.tenant, "unrelated")
        clock = f"setrya:cache:v1:{self.namespace}:{KEY.tenant}:doc-clock"
        self.client.pexpire(clock, 200)
        admission = self.cache.begin(KEY)
        self.assertGreater(self.client.pttl(clock), 1000)
        self.cache.invalidate_document(KEY.tenant, "policy")
        self.assertFalse(self.cache.publish(admission, "old", chunks=[POLICY]))

    def test_refresh_scope_changes_and_stale_cancel(self):
        published = self._warm(KEY)
        old = self.cache.begin(KEY)
        colleague = CacheKey("org", "bob", "policy", "question")
        retained = self._warm(colleague)
        self.assertEqual(published.dependencies.retrieval_version, retained.dependencies.retrieval_version)
        self.assertEqual(self.cache.invalidate_access("org", "alice"), 1)
        new = self.cache.begin(KEY)
        self.assertNotEqual(old.access_version, new.access_version)
        self.assertEqual(old.retrieval_version, new.retrieval_version)
        self.assertFalse(self.cache.cancel(old))
        self.assertFalse(self.cache.publish(old, "stale", chunks=[POLICY]))
        self.assertTrue(self.cache.publish(new, "now public", chunks=[GUIDE]))
        self.assertEqual(self.cache.get(colleague), retained)
        self.assertEqual(self.cache.invalidate_retrieval("org", "policy", "policy"), 2)
        self.assertEqual(self.cache.invalidate_document("org", "policy"), 0)

    def test_joined_manual_producer_cannot_end_a_healthy_automatic_fill(self):
        from redis import Redis
        with Redis.from_url(URL) as other_client:
            other = RedisAnswerCache(other_client, namespace=self.namespace)

            def retrieve():
                joined = other.begin(KEY)
                self.assertFalse(other.cancel(joined))
                other.invalidate_document(KEY.tenant, "policy")
                self.assertFalse(other.publish(joined, "outdated", chunks=[POLICY]))
                return [GUIDE]

            hit = self.cache.fill(KEY, retrieve, lambda _: "guide only")
            self.assertEqual(hit.answer, "guide only")
            self.assertEqual(other.get(KEY), hit)

    def test_lease_expiry_recovers_a_departed_owner_and_allows_retry(self):
        cache = RedisAnswerCache(self.client, namespace=self.namespace, lease_seconds=1)
        old = cache.begin(KEY)
        with self.assertRaises(TimeoutError):
            cache.fill(KEY, lambda: [], lambda items: "unexpected", wait_timeout=0)
        deadline = monotonic() + 4
        while monotonic() < deadline:
            new = cache.begin(KEY)
            if new.answer_id != old.answer_id:
                break
            sleep(0.02)
        self.assertNotEqual(old.answer_id, new.answer_id)
        self.assertFalse(cache.publish(old, "too late", chunks=[GUIDE]))
        self.assertTrue(cache.publish(new, "recovered", chunks=[GUIDE]))

    def test_callback_failure_wakes_waiters_and_preserves_valid_refresh(self):
        preparing, resume, joined = Event(), Event(), Event()

        class Observed(RedisAnswerCache):
            def _wait_for(self, fill, timeout):
                joined.set()
                return super()._wait_for(fill, timeout)

        waiter = Observed(self.client, namespace=self.namespace)

        def fail():
            preparing.set()
            if not resume.wait(5):
                raise AssertionError("owner timeout")
            raise ValueError("retrieval failed")

        with ThreadPoolExecutor(max_workers=2) as workers:
            owner = workers.submit(self.cache.fill, KEY, fail, lambda items: "")
            self.assertTrue(preparing.wait(5))
            try:
                follower = workers.submit(waiter.fill, KEY, lambda: [], lambda items: "unexpected")
                self.assertTrue(joined.wait(5))
            finally:
                resume.set()
            with self.assertRaisesRegex(ValueError, "retrieval failed"):
                owner.result(timeout=5)
            self.assertIsNone(follower.result(timeout=5))
        published = self._warm(KEY)
        refresh = self.cache.begin(KEY)
        self.assertTrue(self.cache.cancel(refresh))
        self.assertEqual(self.cache.get(KEY), published)

    def test_answer_and_indices_expire_without_an_application_cleanup_job(self):
        cache = RedisAnswerCache(self.client, namespace=self.namespace, ttl_seconds=1, lease_seconds=1)
        self.assertIsNotNone(cache.fill(KEY, lambda: [POLICY], lambda items: "temporary"))
        deadline = monotonic() + 4
        pattern = f"setrya:cache:v1:{self.namespace}:*"
        while monotonic() < deadline and any(self.client.scan_iter(match=pattern)):
            sleep(0.02)
        self.assertEqual(list(self.client.scan_iter(match=pattern)), [])


@unittest.skipUnless(URL, "set SETRYA_REDIS_URL to exercise the Redis integration")
class RedisRevisionRegistryTests(unittest.TestCase):
    def test_revision_retry_preserves_new_answer_and_all_reverse_dependencies(self):
        from redis import Redis

        with Redis.from_url(URL) as client:
            namespace = uuid4().hex
            cache = RedisAnswerCache(client, namespace=namespace, require_current_sources=True)
            guide_key = CacheKey("org", "alice", "policy", "guide-only")
            current = passage_metadata("policy", "B", "one")
            try:
                cache.set_document_revision(KEY.tenant, "policy", "A")
                cache.set_document_revision(KEY.tenant, "guide", "B")
                original = cache.fill(KEY, lambda: [POLICY, GUIDE], lambda _: "old joint answer")
                guide = cache.fill(guide_key, lambda: [GUIDE], lambda _: "guide only")
                pending = cache.begin(KEY)

                self.assertEqual(cache.set_document_revision(KEY.tenant, "policy", "B"), 1)
                self.assertIsNone(cache.get(KEY))
                self.assertFalse(cache.publish(pending, "prior fill", chunks=[current, GUIDE]))
                self.assertEqual(cache.get(guide_key), guide)

                replacement = cache.fill(KEY, lambda: [current, GUIDE], lambda _: "new joint answer")
                self.assertNotEqual(replacement.dependencies.answer_id, original.dependencies.answer_id)
                self.assertEqual(cache.set_document_revision(KEY.tenant, "policy", "B"), 0)
                self.assertEqual(cache.get(KEY), replacement)
                self.assertEqual(cache.invalidate_document(KEY.tenant, "guide"), 2)
                self.assertIsNone(cache.get(KEY))
                self.assertIsNone(cache.get(guide_key))
            finally:
                keys = list(client.scan_iter(match=f"setrya:cache:v1:{namespace}:*"))
                if keys:
                    client.unlink(*keys)

    def test_strict_cross_process_authority_and_scoped_erasure(self):
        from redis import Redis
        from setrya.erasure.cache import SubjectAnswerCacheAdapter
        from setrya.erasure.subject import Subject

        with Redis.from_url(URL) as client:
            namespace = uuid4().hex
            writer = RedisAnswerCache(client, namespace=namespace, require_current_sources=True)
            reader = RedisAnswerCache(client, namespace=namespace, require_current_sources=True)
            subject = Subject.derive(b"t" * 32, "tenant", "synthetic-a", "restricted")
            control = Subject.derive(b"t" * 32, "tenant", "synthetic-b", "restricted")
            first = CacheKey("tenant", "doctor", subject.ref, "chart")
            second = CacheKey("tenant", "doctor", control.ref, "chart")
            try:
                legacy = RedisAnswerCache(client, namespace=namespace)
                self.assertTrue(legacy.publish(legacy.begin(first), "unregistered", chunks=[POLICY]))
                self.assertIsNone(reader.get(first))
                self.assertIsNone(reader.fill(first, lambda: [POLICY], lambda _: "unregistered"))
                self.assertFalse(reader.publish(reader.begin(first), "unknown", chunks=[POLICY]))
                writer.set_document_revision("tenant", "policy", "A")
                self.assertTrue(reader.publish(reader.begin(first), "first", chunks=[POLICY]))
                writer.set_document_revision("tenant", "policy", "A")
                self.assertEqual(reader.get(first).answer, "first")
                self.assertEqual(writer.set_document_revision("tenant", "policy", "B"), 1)
                self.assertFalse(reader.publish(reader.begin(first), "stale", chunks=[POLICY]))
                fresh = passage_metadata("policy", "B", "one")
                for key in (first, second):
                    self.assertTrue(reader.publish(reader.begin(key), "current", chunks=[fresh]))
                adapter = SubjectAnswerCacheAdapter(writer, tenant="tenant")
                self.assertEqual(adapter.find(subject), {"tenant:doctor:chart"})
                pending = reader.begin(first)
                adapter.delete(subject)
                self.assertEqual(adapter.verify_absent(subject), set())
                self.assertFalse(reader.publish(pending, "revived", chunks=[fresh]))
                self.assertTrue(adapter.find(control))
                writer.set_document_revision("tenant", "policy", None)
                self.assertFalse(reader.publish(reader.begin(second), "deleted", chunks=[fresh]))
            finally:
                keys = list(client.scan_iter(match=f"setrya:cache:v1:{namespace}:*"))
                if keys:
                    client.unlink(*keys)
