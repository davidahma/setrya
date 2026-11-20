"""Direct erasure of answers in a subject-exclusive SDK cache retrieval scope."""

from setrya.drill import InputError, label
from setrya.local_cache import AnswerCache, CacheKey

from .subject import Subject


def answer_location(key: CacheKey, subject: Subject) -> str:
    if not isinstance(key, CacheKey) or key.retrieval_scope != subject.ref:
        raise InputError("use the subject reference as the cache retrieval scope for subject-linked answers")
    return f"{key.tenant}:{key.access_scope}:{key.request}"


class SubjectAnswerCacheAdapter:
    proof = "direct"

    def __init__(self, cache: AnswerCache, *, tenant: str):
        self.cache = cache
        self.tenant = label(tenant, "tenant")

    def find(self, subject: Subject) -> set[str]:
        return {answer_location(key, subject) for key in self.cache.cached_keys(self.tenant, subject.ref)}

    def delete(self, subject: Subject) -> None:
        self.cache.invalidate_retrieval(self.tenant, subject.ref)

    def verify_absent(self, subject: Subject) -> set[str]:
        return self.find(subject)
