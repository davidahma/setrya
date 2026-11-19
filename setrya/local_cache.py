"""Bounded single-process answer cache with an invalidation guard on in-flight fills."""

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from .dependencies import AnswerDependencies
from .drill import InputError, label


@dataclass(frozen=True, slots=True)
class CacheKey:
    tenant: str
    access_scope: str
    retrieval_scope: str
    request: str

    def __post_init__(self):
        for name in ("tenant", "access_scope", "retrieval_scope", "request"):
            label(getattr(self, name), name)

    def _scopes(self):
        return (("access", self.tenant, self.access_scope), ("retrieval", self.tenant, self.retrieval_scope))


@dataclass(frozen=True, eq=False, slots=True)
class PendingFill:
    """One admission to publish the generation begun for this exact key."""

    key: CacheKey
    access_version: str
    retrieval_version: str
    answer_id: str


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    answer: str
    dependencies: AnswerDependencies


@dataclass(slots=True)
class _Slot:
    fill: PendingFill
    answer: CachedAnswer | None = None


@dataclass(slots=True)
class _Scope:
    version: str
    references: int = 0


class LocalAnswerCache:
    """An LRU of cached answers and outstanding fills sharing one capacity limit."""

    def __init__(self, max_entries: int = 256, *, max_answer_bytes: int = 65536):
        if any(type(n) is not int or n < 1 for n in (max_entries, max_answer_bytes)):
            raise InputError("cache capacity and answer size must be positive integers")
        self._capacity = max_entries
        self._max_answer_bytes = max_answer_bytes
        self._slots: OrderedDict[CacheKey, _Slot] = OrderedDict()
        self._scopes: dict[tuple[str, str, str], _Scope] = {}
        self._lock = Lock()

    def get(self, key: CacheKey) -> CachedAnswer | None:
        """Look up the exact tenant, caller scope, retrieval scope and request."""
        if not isinstance(key, CacheKey):
            raise InputError("expected a CacheKey")
        with self._lock:
            slot = self._slots.get(key)
            if slot is None or slot.answer is None:
                return None
            self._slots.move_to_end(key)
            return slot.answer

    def _discard(self, key):
        del self._slots[key]
        for scope_id in key._scopes():
            state = self._scopes[scope_id]
            state.references -= 1
            if state.references == 0:
                del self._scopes[scope_id]

    def begin(self, key: CacheKey) -> PendingFill:
        """Reserve a slot before authorizing, retrieving sources and generating."""
        if not isinstance(key, CacheKey):
            raise InputError("expected a CacheKey")
        with self._lock:
            scope_ids = key._scopes()
            if key not in self._slots:
                if len(self._slots) == self._capacity:
                    self._discard(next(iter(self._slots)))
                for scope_id in scope_ids:
                    if scope_id not in self._scopes:
                        self._scopes[scope_id] = _Scope(uuid4().hex)
                    self._scopes[scope_id].references += 1
            access, retrieval = (self._scopes[scope_id].version for scope_id in scope_ids)
            fill = PendingFill(key, access, retrieval, uuid4().hex)
            self._slots[key] = _Slot(fill)
            self._slots.move_to_end(key)
            return fill

    def publish(self, fill: PendingFill, answer: str, *, chunks: Iterable[Mapping]) -> bool:
        """Publish once. Return False when invalidation, eviction or another fill won."""
        if not isinstance(fill, PendingFill) or not isinstance(answer, str):
            raise InputError("expected a pending fill and answer text")
        if len(answer) > self._max_answer_bytes or len(answer.encode("utf-8")) > self._max_answer_bytes:
            raise InputError("answer exceeds this cache's byte limit")
        # Iterating user-supplied generation inputs must not hold the cache lock.
        record = AnswerDependencies.record(
            answer_id=fill.answer_id, tenant=fill.key.tenant, access_scope=fill.key.access_scope,
            access_version=fill.access_version, retrieval_scope=fill.key.retrieval_scope,
            retrieval_version=fill.retrieval_version, chunks=chunks,
        )
        with self._lock:
            slot = self._slots.get(fill.key)
            if slot is None or slot.fill is not fill or slot.answer is not None:
                return False
            slot.answer = CachedAnswer(answer, record)
            self._slots.move_to_end(fill.key)
            return True

    def _invalidate(self, tenant, field, scopes):
        label(tenant, "tenant")
        if not scopes:
            raise InputError("supply at least one affected scope")
        affected = {label(scope, field) for scope in scopes}
        with self._lock:
            keys = [key for key in self._slots if key.tenant == tenant and getattr(key, field) in affected]
            for key in keys:
                self._discard(key)
            return len(keys)

    def invalidate_retrieval(self, tenant: str, *scopes: str) -> int:
        """Drop answers and pending fills for changed retrieval scopes in this tenant."""
        return self._invalidate(tenant, "retrieval_scope", scopes)

    def invalidate_access(self, tenant: str, *scopes: str) -> int:
        """Drop answers and pending fills for changed caller scopes in this tenant."""
        return self._invalidate(tenant, "access_scope", scopes)
