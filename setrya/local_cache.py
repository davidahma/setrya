"""Cache answers against the exact inputs used to generate them."""

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Set
from dataclasses import dataclass, field, replace
from itertools import islice
from threading import Condition, TIMEOUT_MAX
from typing import TypeVar
from uuid import uuid4

from .dependencies import MAX_CHUNKS, AnswerDependencies
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
    _cancel_token: str | None = field(default=None, repr=False, kw_only=True)


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    answer: str
    dependencies: AnswerDependencies


_Item = TypeVar("_Item")


class CacheCapacityError(RuntimeError):
    """A new key cannot be admitted while all bounded cache slots have active fills."""


class AnswerCache:
    """The fill protocol shared by the local and Redis caches."""

    _max_answer_bytes: int

    def get(self, key: CacheKey) -> CachedAnswer | None:
        raise NotImplementedError

    def cached_keys(self, tenant: str, retrieval_scope: str) -> tuple[CacheKey, ...]:
        """Enumerate stored answers visible in this tenant and exact retrieval scope."""
        raise NotImplementedError

    def invalidate_document(self, tenant: str, document: str) -> int:
        raise NotImplementedError

    def set_document_revision(self, tenant: str, document: str, revision: str | None) -> int:
        """Register the current revision, or None on deletion, at the write boundary."""
        raise NotImplementedError

    def unregistered_documents(self, tenant: str, *documents: str) -> tuple[str, ...]:
        """Audit a batch of known document IDs before enabling strict publication."""
        raise NotImplementedError

    def invalidate_retrieval(self, tenant: str, *scopes: str) -> int:
        raise NotImplementedError

    def invalidate_access(self, tenant: str, *scopes: str) -> int:
        raise NotImplementedError

    def _acquire(self, key: CacheKey, *, refresh: bool) -> CachedAnswer | tuple[PendingFill, bool]:
        raise NotImplementedError

    def _wait_for(self, fill: PendingFill, timeout: float) -> CachedAnswer | None:
        raise NotImplementedError

    def _publish_record(self, fill: PendingFill, answer: str, record: AnswerDependencies) -> bool:
        raise NotImplementedError

    def cancel(self, fill: PendingFill) -> bool:
        """Relinquish an owned admission and wake its waiters. A joiner returns False."""
        raise NotImplementedError

    def begin(self, key: CacheKey) -> PendingFill:
        """Reserve a manual fill or join the current one. Keep an existing answer readable."""
        if not isinstance(key, CacheKey):
            raise InputError("expected a CacheKey")
        admission = self._acquire(key, refresh=True)
        if isinstance(admission, CachedAnswer):
            raise RuntimeError("a refresh must reserve a fill")
        return admission[0]

    def _validate_answer(self, answer: str) -> None:
        if not isinstance(answer, str):
            raise InputError("expected answer text")
        if len(answer) > self._max_answer_bytes or len(answer.encode("utf-8")) > self._max_answer_bytes:
            raise InputError("answer exceeds this cache's byte limit")

    def publish(self, fill: PendingFill, answer: str, *, chunks: Iterable[Mapping]) -> bool:
        """Publish once. A superseded, invalidated or evicted admission returns False."""
        if not isinstance(fill, PendingFill):
            raise InputError("expected a pending fill")
        self._validate_answer(answer)
        record = AnswerDependencies.record(
            answer_id=fill.answer_id, tenant=fill.key.tenant, access_scope=fill.key.access_scope,
            access_version=fill.access_version, retrieval_scope=fill.key.retrieval_scope,
            retrieval_version=fill.retrieval_version, chunks=chunks,
        )
        return self._publish_record(fill, answer, record)

    def fill(
        self, key: CacheKey, retrieve: Callable[[], Iterable[_Item]], generate: Callable[[tuple[_Item, ...]], str],
        *, metadata: Callable[[_Item], Mapping] | None = None, wait_timeout: float = 30.0,
    ) -> CachedAnswer | None:
        """Get a hit, wait for its owner, or reserve before retrieving and generating.

        A waiter returns None if its owner cancels or loses admission. Timeouts
        raise TimeoutError without cancelling the owner. Inputs are in generation
        order. metadata selects passage metadata from each item (default: the item).
        """
        if not isinstance(key, CacheKey):
            raise InputError("expected a CacheKey")
        if type(wait_timeout) not in (int, float) or not 0 <= wait_timeout <= TIMEOUT_MAX:
            raise InputError("wait_timeout must be a finite nonnegative duration within the platform limit")
        admission = self._acquire(key, refresh=False)
        if isinstance(admission, CachedAnswer):
            return admission
        pending, owner = admission
        if not owner:
            return self._wait_for(pending, wait_timeout)
        try:
            retrieved = retrieve()
            if not isinstance(retrieved, Iterable) or isinstance(retrieved, (str, bytes, Mapping, Set)):
                raise InputError("retrieve must return an ordered iterable of generation inputs")
            items = tuple(islice(retrieved, MAX_CHUNKS + 1))
            if len(items) > MAX_CHUNKS:
                raise InputError(f"an answer record supports at most {MAX_CHUNKS} generation inputs")
            chunks = items if metadata is None else (metadata(item) for item in items)
            record = AnswerDependencies.record(
                answer_id=pending.answer_id, tenant=key.tenant, access_scope=key.access_scope,
                access_version=pending.access_version, retrieval_scope=key.retrieval_scope,
                retrieval_version=pending.retrieval_version, chunks=chunks,
            )
            answer = generate(items)
            self._validate_answer(answer)
            if self._publish_record(pending, answer, record):
                return CachedAnswer(answer, record)
            return None
        except BaseException as error:
            try:
                self.cancel(pending)
            except Exception as cleanup_error:
                error.add_note(f"Cache fill cleanup failed ({type(cleanup_error).__name__}).")
            raise


@dataclass(slots=True)
class _Slot:
    pending: PendingFill | None = None
    joined: PendingFill | None = None
    answer: CachedAnswer | None = None
    changed_documents: set[str] = field(default_factory=set)

    def admits(self, fill: PendingFill) -> bool:
        return fill is self.pending or fill is self.joined


@dataclass(slots=True)
class _Scope:
    version: str
    references: int = 0


class LocalAnswerCache(AnswerCache):
    """A process-local LRU of finished answers and outstanding admissions."""

    def __init__(self, max_entries: int = 256, *, max_answer_bytes: int = 65536, require_current_sources: bool = False):
        if any(type(n) is not int or n < 1 for n in (max_entries, max_answer_bytes)):
            raise InputError("cache capacity and answer size must be positive integers")
        if type(require_current_sources) is not bool:
            raise InputError("require_current_sources must be a boolean")
        self._strict = require_current_sources
        self._revisions: dict[tuple[str, str], str | None] = {}
        self._capacity = max_entries
        self._max_answer_bytes = max_answer_bytes
        self._slots: OrderedDict[CacheKey, _Slot] = OrderedDict()
        self._scopes: dict[tuple[str, str, str], _Scope] = {}
        self._documents: dict[tuple[str, str], set[CacheKey]] = {}
        self._changed = Condition()

    def get(self, key: CacheKey) -> CachedAnswer | None:
        """Look up the exact tenant, caller scope, retrieval scope and request."""
        if not isinstance(key, CacheKey):
            raise InputError("expected a CacheKey")
        with self._changed:
            slot = self._slots.get(key)
            if slot is None or slot.answer is None:
                return None
            self._slots.move_to_end(key)
            return slot.answer

    def cached_keys(self, tenant: str, retrieval_scope: str) -> tuple[CacheKey, ...]:
        label(tenant, "tenant")
        label(retrieval_scope, "retrieval scope")
        with self._changed:
            return tuple(key for key, slot in self._slots.items()
                         if key.tenant == tenant and key.retrieval_scope == retrieval_scope and slot.answer is not None)

    def _matching_answer_keys(self, tenant, retrieval_scope, marker, limit):
        # Keep a point-in-time view without promoting inspected entries in the LRU.
        with self._changed:
            keys = self.cached_keys(tenant, retrieval_scope)
            if len(keys) > limit:
                raise InputError("answer cache canary scan exceeds the key limit")
            answers = [(key, self._slots[key].answer) for key in keys]
        return [key for key, answer in answers if marker in answer.answer]

    def _unlink_answer(self, key, slot):
        if slot.answer is not None:
            for document in {chunk.document for chunk in slot.answer.dependencies.chunks}:
                document_id = key.tenant, document
                keys = self._documents[document_id]
                keys.remove(key)
                if not keys:
                    del self._documents[document_id]
            slot.answer = None

    def _discard(self, key):
        self._unlink_answer(key, self._slots.pop(key))
        for scope_id in key._scopes():
            state = self._scopes[scope_id]
            state.references -= 1
            if state.references == 0:
                del self._scopes[scope_id]

    def _acquire(self, key, *, refresh):
        with self._changed:
            slot = self._slots.get(key)
            if slot is None:
                if len(self._slots) == self._capacity:
                    idle = next((other for other, state in self._slots.items() if state.pending is None), None)
                    if idle is None:
                        raise CacheCapacityError("all cache slots have active fills")
                    self._discard(idle)
                slot = self._slots[key] = _Slot()
                for scope_id in key._scopes():
                    if scope_id not in self._scopes:
                        self._scopes[scope_id] = _Scope(uuid4().hex)
                    self._scopes[scope_id].references += 1
            self._slots.move_to_end(key)
            if not refresh and slot.answer is not None:
                return slot.answer
            if slot.pending is not None:
                if slot.joined is None:
                    slot.joined = replace(slot.pending)
                return slot.joined, False
            access, retrieval = (self._scopes[scope_id].version for scope_id in key._scopes())
            slot.pending = PendingFill(key, access, retrieval, uuid4().hex)
            return slot.pending, True

    def _wait_for(self, fill, timeout):
        def completed():
            slot = self._slots.get(fill.key)
            return slot is None or not slot.admits(fill)

        with self._changed:
            if not self._changed.wait_for(completed, timeout=timeout):
                raise TimeoutError("timed out waiting for this cache fill")
            slot = self._slots.get(fill.key)
            if slot and slot.answer and slot.answer.dependencies.answer_id == fill.answer_id:
                self._slots.move_to_end(fill.key)
                return slot.answer
            return None

    def _end_pending(self, key, slot):
        slot.pending = None
        slot.joined = None
        slot.changed_documents.clear()
        if slot.answer is None:
            self._discard(key)
        self._changed.notify_all()

    def cancel(self, fill: PendingFill) -> bool:
        if not isinstance(fill, PendingFill):
            raise InputError("expected a pending fill")
        with self._changed:
            slot = self._slots.get(fill.key)
            if slot is None or slot.pending is not fill:
                return False
            self._end_pending(fill.key, slot)
            return True

    def _publish_record(self, fill, answer, record):
        with self._changed:
            slot = self._slots.get(fill.key)
            if slot is None or not slot.admits(fill):
                return False
            documents = {chunk.document for chunk in record.chunks}
            outdated = any(
                self._revisions.get((fill.key.tenant, chunk.document), None if self._strict else chunk.revision)
                != chunk.revision for chunk in record.chunks
            )
            if outdated or not documents.isdisjoint(slot.changed_documents):
                if slot.pending is fill:
                    self._end_pending(fill.key, slot)
                return False
            self._unlink_answer(fill.key, slot)
            slot.answer = CachedAnswer(answer, record)
            for document in documents:
                self._documents.setdefault((fill.key.tenant, document), set()).add(fill.key)
            self._slots.move_to_end(fill.key)
            self._end_pending(fill.key, slot)
            return True

    def unregistered_documents(self, tenant, *documents):
        tenant = label(tenant, "tenant")
        checked = sorted({label(doc, "document") for doc in documents})
        with self._changed:
            return tuple(doc for doc in checked if (tenant, doc) not in self._revisions)

    def set_document_revision(self, tenant: str, document: str, revision: str | None) -> int:
        key = label(tenant, "tenant"), label(document, "document")
        if revision is not None:
            label(revision, "revision")
        with self._changed:
            if key in self._revisions and self._revisions[key] == revision:
                return 0
            self._revisions[key] = revision
            return self.invalidate_document(tenant, document)

    def invalidate_document(self, tenant: str, document: str) -> int:
        """Evict answers citing this document and fence those now in generation."""
        label(tenant, "tenant")
        label(document, "document")
        with self._changed:
            cited = tuple(self._documents.get((tenant, document), ()))
            for key in cited:
                slot = self._slots[key]
                self._unlink_answer(key, slot)
                if slot.pending is None:
                    self._discard(key)
            for key, slot in list(self._slots.items()):
                if key.tenant == tenant and slot.pending is not None:
                    # The low-level producer only supplies dependencies at publish.
                    # Keep a bounded change history for its lifetime.
                    slot.changed_documents.add(document)
                    if len(slot.changed_documents) > MAX_CHUNKS:
                        self._end_pending(key, slot)
            return len(cited)

    def _invalidate(self, tenant, field, scopes):
        label(tenant, "tenant")
        if not scopes:
            raise InputError("supply at least one affected scope")
        affected = {label(scope, field) for scope in scopes}
        with self._changed:
            keys = [key for key in self._slots if key.tenant == tenant and getattr(key, field) in affected]
            for key in keys:
                self._discard(key)
            self._changed.notify_all()
            return len(keys)

    def invalidate_retrieval(self, tenant: str, *scopes: str) -> int:
        """Drop answers and pending fills for changed retrieval scopes in this tenant."""
        return self._invalidate(tenant, "retrieval_scope", scopes)

    def invalidate_access(self, tenant: str, *scopes: str) -> int:
        """Drop answers and pending fills for changed caller scopes in this tenant."""
        return self._invalidate(tenant, "access_scope", scopes)
