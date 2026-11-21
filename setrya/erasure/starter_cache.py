"""Construct a cache for a disposable app. Register source revisions on its ingestion path."""
from setrya.local_cache import LocalAnswerCache


def make_cache(redis_client=None, *, namespace="erasure-test"):
    if redis_client is None:
        return LocalAnswerCache(require_current_sources=True)
    from setrya.redis_cache import RedisAnswerCache

    return RedisAnswerCache(redis_client, namespace=namespace, require_current_sources=True)
