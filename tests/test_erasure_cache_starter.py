"""The generated cache rejects unregistered or stale revisions and exposes registration gaps."""
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from setrya.erasure.starter import init_erasure
from setrya.local_cache import CacheKey
from setrya.store_signals import passage_metadata


class StrictCacheStarter(unittest.TestCase):
    def check_cache(self, client=None):
        with TemporaryDirectory() as tmp:
            root = init_erasure(Path(tmp) / "starter", shared=True)
            cache = runpy.run_path(str(root / "answer_cache.py"))["make_cache"](client, namespace=uuid4().hex)
        tenant = uuid4().hex
        key = CacheKey(tenant, "reader", "scope", "question")
        old = passage_metadata("doc", "A", "chunk")
        self.assertEqual(cache.unregistered_documents(tenant, "doc", "extra", "doc"), ("doc", "extra"))
        self.assertFalse(cache.publish(cache.begin(key), "unregistered", chunks=[old]))
        cache.set_document_revision(tenant, "doc", "A")
        self.assertTrue(cache.publish(cache.begin(key), "ok", chunks=[old]))
        # Simulate a pre-change read which arrives at publication after the update.
        cache.set_document_revision(tenant, "doc", "B")
        self.assertFalse(cache.publish(cache.begin(key), "stale", chunks=[old]))
        cache.set_document_revision(tenant, "doc", None)
        self.assertEqual(cache.unregistered_documents(tenant, "extra", "doc"), ("extra",))
        self.assertFalse(cache.publish(cache.begin(key), "deleted", chunks=[old]))
        self.assertEqual(cache.unregistered_documents("another", "doc"), ("doc",))

    def test_local(self):
        self.check_cache()

    @unittest.skipUnless(os.getenv("SETRYA_REDIS_URL"), "requires fixture Redis")
    def test_redis(self):
        import redis
        self.check_cache(redis.Redis.from_url(os.environ["SETRYA_REDIS_URL"]))
