"""Exact-marker reads of full LangGraph checkpoints and declared memory namespaces."""
from collections.abc import Mapping

from setrya.drill import InputError
from .adapters import _pg_connection, _scoped_memory_key
from .canary import LIMIT, contains_marker, validate_probe
from .journal import identifier


class SQLiteCheckpointCanaryScanner:
    """Scan raw checkpoint and pending-write payloads, including orphan writes."""

    def __init__(self, connection):
        self.db = connection

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        if self.db.in_transaction:
            raise InputError("commit application writes before scanning")
        rows = self.db.execute("""
            SELECT type, checkpoint, metadata FROM checkpoints WHERE thread_id=? COLLATE BINARY
            UNION ALL SELECT type, value, NULL FROM writes WHERE thread_id=? COLLATE BINARY LIMIT 4097
        """, (scope, scope))
        return _checkpoint_hits(rows, scope, marker)


class PostgresCheckpointCanaryScanner:
    """Scan a full PostgresSaver's JSONB checkpoint, plus all blobs and pending writes."""

    def __init__(self, connection):
        self.db = connection

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        with _pg_connection(self.db) as conn:
            rows = conn.execute("""
                SELECT 'json' AS type, NULL::bytea AS payload, jsonb_build_array(checkpoint, metadata) AS metadata
                    FROM checkpoints WHERE thread_id COLLATE "C" = %s
                UNION ALL SELECT type, blob, NULL FROM checkpoint_blobs WHERE thread_id COLLATE "C" = %s
                UNION ALL SELECT type, blob, NULL FROM checkpoint_writes WHERE thread_id COLLATE "C" = %s
                LIMIT 4097
            """, (scope, scope, scope))
            return _checkpoint_hits(rows, scope, marker)


def _checkpoint_hits(rows, scope, marker):
    found = False
    for index, row in enumerate(rows):
        kind, payload, metadata = row.values() if isinstance(row, Mapping) else row
        if index >= LIMIT or kind not in {"bytes", "msgpack", "json", "null", "empty"}:
            raise InputError("oversized checkpoint scan or unsupported checkpoint encoding")
        found |= contains_marker(payload, marker) or contains_marker(metadata, marker)
    return {scope} if found else set()


class LangGraphMemoryCanaryScanner:
    """Scan all readable memory values at and below a declared LangGraph namespace."""

    def __init__(self, store, *, namespace=()):
        self.store = store
        self.namespace = tuple(identifier(part, "memory namespace") for part in namespace)

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        root = (*self.namespace, scope)
        found, seen = set(), set()
        while True:
            page = self.store.search(root, limit=128, offset=len(seen))
            for item in page:
                key = _scoped_memory_key(root, item.namespace, item.key)
                if key in seen or len(seen) >= LIMIT:
                    raise InputError("duplicate key or oversized LangGraph memory scan")
                seen.add(key)
                if contains_marker(item.value, marker):
                    found.add(key)
            if len(page) < 128:
                return found


class PostgresMemoryCanaryScanner:
    """Read stored LangGraph JSON values, including expired rows the Store API omits."""

    def __init__(self, connection, *, namespace=()):
        self.connection = connection
        self.namespace = tuple(identifier(part, "memory namespace") for part in namespace)

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        root = (*self.namespace, scope)
        if any("." in part for part in root):
            raise InputError("LangGraph namespace components cannot contain periods")
        prefix = ".".join(root)
        with _pg_connection(self.connection) as conn:
            rows = conn.execute("""
                SELECT prefix, key, value FROM store WHERE prefix COLLATE "C" = %s
                   OR starts_with(prefix, %s) LIMIT 4097
            """, (prefix, prefix + "."))
            found = set()
            for index, row in enumerate(rows):
                if index >= LIMIT:
                    raise InputError("oversized LangGraph memory scan")
                location, key, value = row.values() if isinstance(row, Mapping) else row
                if contains_marker(value, marker):
                    found.add(_scoped_memory_key(root, tuple(location.split(".")), key))
            return found
