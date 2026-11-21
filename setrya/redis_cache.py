"""A Redis-backed answer cache shared by application and worker processes."""

import json
from time import monotonic, sleep
from uuid import uuid4

from .dependencies import AnswerDependencies
from .drill import InputError, label
from .local_cache import AnswerCache, CachedAnswer, CacheKey, PendingFill


# A document change must remain visible for the entire lifetime of every
# reservation that could have preceded it, including ones issued by other clients.
_MAX_LEASE_SECONDS = 3600

# Each script runs on a standalone Redis primary. Labels exclude colons, so the
# composite slot and index names cannot alias another tenant, scope or request.
_COMMON = """
local prefix = KEYS[1]
local function now_ms()
    local t = redis.call('TIME')
    return t[1] * 1000 + math.floor(t[2] / 1000)
end
local function keep_for(key, ttl)
    if redis.call('PTTL', key) < ttl then redis.call('PEXPIRE', key, ttl) end
end
local function index(key, id, ttl, now)
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
    redis.call('ZADD', key, 'GT', now + ttl, id)
    keep_for(key, ttl)
end
local function scopes(id)
    local access, retrieval = string.match(id, '^([^:]+):([^:]+):')
    return prefix .. 'access:' .. access, prefix .. 'retrieval:' .. retrieval
end
local function index_scopes(id, ttl, now)
    local access, retrieval = scopes(id)
    index(access, id, ttl, now)
    index(retrieval, id, ttl, now)
end
local function unlink_scopes_if_empty(id)
    if redis.call('EXISTS', prefix .. 'answer:' .. id, prefix .. 'lease:' .. id) == 0 then
        local access, retrieval = scopes(id)
        redis.call('ZREM', access, id)
        redis.call('ZREM', retrieval, id)
    end
end
local function remove_answer(id)
    local key = prefix .. 'answer:' .. id
    local stored = redis.call('HMGET', key, 'docs', 'id')
    if not stored[1] then return false end
    for _, doc in ipairs(cjson.decode(stored[1])) do
        redis.call('ZREM', prefix .. 'doc:' .. doc, id .. '/' .. stored[2])
    end
    redis.call('DEL', key)
    return true
end
"""

_ACQUIRE = """
local id, refresh, owner = ARGV[1], ARGV[2], ARGV[3]
local lease_ttl = tonumber(ARGV[6])
if refresh == '0' then
    local item = redis.call('HMGET', prefix .. 'answer:' .. id, 'data', 'verified')
    if item[1] and (ARGV[8] == '0' or item[2] == '1') then return {'hit', item[1]} end
end
local lease = prefix .. 'lease:' .. id
local current = redis.call('GET', lease)
if current then
    local fill = cjson.decode(current)
    return {'join', fill[1], fill[2], fill[3]}
end
local function scope_version(key, suggested)
    local vkey = key .. ':version'
    local version = redis.call('GET', vkey)
    if not version then
        version = suggested
        redis.call('SET', vkey, version)
    end
    keep_for(vkey, lease_ttl)
    return version
end
local a, r = scopes(id)
local access = scope_version(a, ARGV[4])
local retrieval = scope_version(r, ARGV[5])
local clock = prefix .. 'doc-clock'
local epoch = redis.call('GET', clock) or '0'
-- An admission holding a nonzero epoch must expire before this counter resets.
keep_for(clock, lease_ttl)
redis.call('SET', lease, cjson.encode({owner, access, retrieval, epoch, ARGV[7]}), 'PX', lease_ttl)
index_scopes(id, lease_ttl, now_ms())
return {'owner', owner, access, retrieval}
"""

_FINISH = """
local id = ARGV[1]
local lease = prefix .. 'lease:' .. id
local value = redis.call('GET', lease)
if not value then return 0 end
local fill = cjson.decode(value)
if fill[1] ~= ARGV[2] or fill[2] ~= ARGV[3] or fill[3] ~= ARGV[4] then return 0 end
-- All participants may publish. Only the original acquirer can abandon the lease.
local is_owner = fill[5] == ARGV[6]
if ARGV[5] == 'cancel' then
    if not is_owner then return 0 end
    redis.call('DEL', lease)
    unlink_scopes_if_empty(id)
    return 1
end
local docs = cjson.decode(ARGV[7])
local verified = '1'
for _, chunk in ipairs(cjson.decode(ARGV[10])) do
    local doc = chunk[1]
    local revision = redis.call('HGET', prefix .. 'revisions', doc)
    if not revision then verified = '0' end
    local outdated = (revision and revision ~= chunk[2]) or (not revision and ARGV[11] == '1')
    local changed = redis.call('GET', prefix .. 'change:' .. doc)
    if outdated or (changed and tonumber(changed) > tonumber(fill[4])) then
        if is_owner then
            redis.call('DEL', lease)
            unlink_scopes_if_empty(id)
        end
        return 0
    end
end
redis.call('DEL', lease)
remove_answer(id)
local key = prefix .. 'answer:' .. id
local ttl = tonumber(ARGV[9])
local now = now_ms()
redis.call('HSET', key, 'data', ARGV[8], 'id', fill[1], 'docs', ARGV[7], 'verified', verified)
redis.call('PEXPIRE', key, ttl)
for _, doc in ipairs(docs) do index(prefix .. 'doc:' .. doc, id .. '/' .. fill[1], ttl, now) end
index_scopes(id, ttl, now)
local a, r = scopes(id)
keep_for(a .. ':version', ttl)
keep_for(r .. ':version', ttl)
return 1
"""

_WAIT = """
local id = ARGV[1]
local answer = prefix .. 'answer:' .. id
if redis.call('HGET', answer, 'id') == ARGV[2] then
    if ARGV[3] == '1' and redis.call('HGET', answer, 'verified') ~= '1' then return {'gone'} end
    return {'hit', redis.call('HGET', answer, 'data')}
end
local lease = redis.call('GET', prefix .. 'lease:' .. id)
if lease and cjson.decode(lease)[1] == ARGV[2] then return {'pending'} end
return {'gone'}
"""

_INVALIDATE_DOCUMENT = """
local doc, ttl = ARGV[1], tonumber(ARGV[2])
if ARGV[3] then
    if redis.call('HGET', prefix .. 'revisions', doc) == ARGV[3] then return 0 end
    redis.call('HSET', prefix .. 'revisions', doc, ARGV[3])
end
local clock = prefix .. 'doc-clock'
local epoch = redis.call('INCR', clock)
redis.call('PEXPIRE', clock, ttl * 2)
redis.call('SET', prefix .. 'change:' .. doc, epoch, 'PX', ttl)
local idx = prefix .. 'doc:' .. doc
local count = 0
for _, member in ipairs(redis.call('ZRANGE', idx, 0, -1)) do
    local id, publication = string.match(member, '^(.+)/([^/]+)$')
    if redis.call('HGET', prefix .. 'answer:' .. id, 'id') == publication then
        if remove_answer(id) then count = count + 1 end
        unlink_scopes_if_empty(id)
    end
end
redis.call('DEL', idx)
return count
"""

_INVALIDATE_SCOPES = """
local count = 0
for i = 2, #ARGV do
    local idx = prefix .. ARGV[1] .. ':' .. ARGV[i]
    for _, id in ipairs(redis.call('ZRANGE', idx, 0, -1)) do
        local removed = remove_answer(id)
        local pending = redis.call('DEL', prefix .. 'lease:' .. id)
        if removed or pending == 1 then count = count + 1 end
        unlink_scopes_if_empty(id)
    end
    redis.call('DEL', idx, idx .. ':version')
end
return count
"""


_SCAN_ANSWERS = """
local found = {}
local bytes = 0
for i, key in ipairs(KEYS) do
    bytes = bytes + redis.call('HSTRLEN', key, 'data')
    if bytes > 16777216 then return redis.error_reply('answer cache canary content budget exceeded') end
    local data = redis.call('HGET', key, 'data')
    if data then
        local answer = cjson.decode(data)[1]
        if type(answer) ~= 'string' then return redis.error_reply('unreadable cached answer') end
        if string.find(answer, ARGV[1], 1, true) then table.insert(found, i) end
    end
end
return found
"""


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


def _cached(encoded):
    answer, record = json.loads(encoded)
    return CachedAnswer(answer, AnswerDependencies.from_dict(record))


class RedisAnswerCache(AnswerCache):
    """Share answers and cancellable admissions on one synchronous Redis primary."""

    def __init__(self, client, *, namespace: str = "default", ttl_seconds: int = 3600,
                 lease_seconds: int = 60, max_answer_bytes: int = 65536, require_current_sources: bool = False):
        self._namespace = label(namespace, "Redis cache namespace")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 30 * 86400:
            raise InputError("ttl_seconds must be between 1 second and 30 days")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise InputError("lease_seconds must be between 1 and 3600")
        if type(max_answer_bytes) is not int or max_answer_bytes < 1:
            raise InputError("max_answer_bytes must be a positive integer")
        if type(require_current_sources) is not bool:
            raise InputError("require_current_sources must be a boolean")
        self._strict = require_current_sources
        self._client = client
        self._ttl = ttl_seconds * 1000
        self._lease = lease_seconds * 1000
        self._max_answer_bytes = max_answer_bytes
        self._admit = client.register_script(_COMMON + _ACQUIRE)
        self._finish = client.register_script(_COMMON + _FINISH)
        self._wait = client.register_script(_COMMON + _WAIT)
        self._drop_doc = client.register_script(_COMMON + _INVALIDATE_DOCUMENT)
        self._drop_scopes = client.register_script(_COMMON + _INVALIDATE_SCOPES)

    def _prefix(self, tenant):
        return f"setrya:cache:v1:{self._namespace}:{tenant}:"

    @staticmethod
    def _slot(key):
        if not isinstance(key, CacheKey):
            raise InputError("expected a CacheKey")
        return ":".join((key.access_scope, key.retrieval_scope, key.request))

    def get(self, key: CacheKey) -> CachedAnswer | None:
        slot = self._slot(key)
        data, verified = self._client.hmget(self._prefix(key.tenant) + "answer:" + slot, "data", "verified")
        return _cached(data) if data is not None and (not self._strict or _text(verified) == "1") else None

    @staticmethod
    def _answer_slots(client, prefix, scope):
        for raw in client.scan_iter(match=prefix + "*:" + scope + ":*"):
            yield _text(raw).removeprefix(prefix)

    def cached_keys(self, tenant: str, retrieval_scope: str) -> tuple[CacheKey, ...]:
        prefix = self._prefix(label(tenant, "tenant")) + "answer:"
        scope = label(retrieval_scope, "retrieval scope")
        keys = set()
        for slot in self._answer_slots(self._client, prefix, scope):
            access, retrieval, request = slot.split(":")
            if retrieval == scope and self._client.hexists(prefix + slot, "data"):
                keys.add(CacheKey(tenant, access, retrieval, request))
        return tuple(keys)

    def _matching_answer_keys(self, tenant, retrieval_scope, marker, limit):
        prefix = self._prefix(label(tenant, "tenant"))
        scope = label(retrieval_scope, "retrieval scope")
        if self._client.role()[0] not in ("master", b"master") or self._client.info("cluster").get("cluster_enabled") != 0:
            raise InputError("use a standalone Redis primary for the canary fixture")
        index = prefix + "retrieval:" + scope
        with self._client.pipeline() as pipe:
            # New reservations must enter this index before publication. Include
            # existing leases, plus stored hashes found independently of the index.
            pipe.watch(index)
            slots = {_text(slot) for slot in pipe.zrange(index, 0, limit)}
            for slot in self._answer_slots(pipe, prefix + "answer:", scope):
                slots.add(slot)
                if len(slots) > limit:
                    raise InputError("answer cache canary scan exceeds the key limit")
            if len(slots) > limit:  # The index can also contain unpublished leases.
                raise InputError("answer cache canary scan exceeds the key limit")
            keys = []
            for slot in sorted(slots):
                access, retrieval, request = slot.split(":")
                if retrieval != scope:
                    raise InputError("Redis retrieval index contains an out-of-scope key")
                keys.append(CacheKey(tenant, access, retrieval, request))
            # Read underlying publications together even when strict serving hides
            # an older answer. A concurrent index change aborts the transaction.
            pipe.multi()
            pipe.execute_command("EVAL_RO", _SCAN_ANSWERS, len(keys),
                                 *(prefix + "answer:" + self._slot(key) for key in keys), marker)
            matches, = pipe.execute()
        return tuple(keys[i - 1] for i in matches)

    def _acquire(self, key, *, refresh):
        slot = self._slot(key)
        cancel_token = uuid4().hex
        reply = self._admit(keys=[self._prefix(key.tenant)], args=[
            slot, int(refresh), uuid4().hex, uuid4().hex, uuid4().hex, self._lease, cancel_token, int(self._strict),
        ])
        status = _text(reply[0])
        if status == "hit":
            return _cached(reply[1])
        answer_id, access, retrieval = map(_text, reply[1:])
        owner = status == "owner"
        return PendingFill(key, access, retrieval, answer_id, _cancel_token=cancel_token if owner else None), owner

    def _wait_for(self, fill, timeout):
        deadline = monotonic() + timeout
        while True:
            reply = self._wait(keys=[self._prefix(fill.key.tenant)], args=[self._slot(fill.key), fill.answer_id, int(self._strict)])
            status = _text(reply[0])
            if status == "hit":
                return _cached(reply[1])
            if status == "gone":
                return None
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for this cache fill")
            sleep(min(0.05, remaining))

    def _complete(self, fill, operation, *args):
        return bool(self._finish(keys=[self._prefix(fill.key.tenant)], args=[
            self._slot(fill.key), fill.answer_id, fill.access_version, fill.retrieval_version,
            operation, fill._cancel_token or "", *args,
        ]))

    def cancel(self, fill: PendingFill) -> bool:
        if not isinstance(fill, PendingFill):
            raise InputError("expected a pending fill")
        return self._complete(fill, "cancel")

    def _publish_record(self, fill, answer, record):
        docs = sorted({chunk.document for chunk in record.chunks})
        data = json.dumps([answer, record.to_dict()], separators=(",", ":"), ensure_ascii=False)
        revisions = [(chunk.document, chunk.revision) for chunk in record.chunks]
        return self._complete(fill, "publish", json.dumps(docs), data, self._ttl, json.dumps(revisions), int(self._strict))

    def unregistered_documents(self, tenant, *documents):
        prefix = self._prefix(label(tenant, "tenant"))
        checked = sorted({label(doc, "document") for doc in documents})
        if not checked:
            return ()
        revisions = self._client.hmget(prefix + "revisions", checked)
        return tuple(doc for doc, revision in zip(checked, revisions) if revision is None)

    def set_document_revision(self, tenant: str, document: str, revision: str | None) -> int:
        return int(self._drop_doc(keys=[self._prefix(label(tenant, "tenant"))],
                                  args=[label(document, "document"), 1000 * _MAX_LEASE_SECONDS,
                                        "" if revision is None else label(revision, "revision")]))

    def invalidate_document(self, tenant: str, document: str) -> int:
        """Remove cached dependents; reject older pending fills that later cite this document."""
        return int(self._drop_doc(keys=[self._prefix(label(tenant, "tenant"))],
                                  args=[label(document, "document"), 1000 * _MAX_LEASE_SECONDS]))

    def _invalidate(self, tenant, kind, scopes):
        if not scopes:
            raise InputError("supply at least one affected scope")
        scope_ids = sorted({label(scope, kind + " scope") for scope in scopes})
        return int(self._drop_scopes(keys=[self._prefix(label(tenant, "tenant"))], args=[kind, *scope_ids]))

    def invalidate_access(self, tenant: str, *scopes: str) -> int:
        return self._invalidate(tenant, "access", scopes)

    def invalidate_retrieval(self, tenant: str, *scopes: str) -> int:
        return self._invalidate(tenant, "retrieval", scopes)
