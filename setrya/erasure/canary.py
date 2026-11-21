"""Read-only exact-marker discovery within explicit application boundaries."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping

from setrya.drill import InputError
from .adapters import _keys, _pg_connection, _sql_name
from .journal import identifier


LIMIT = 4096


def validate_probe(scope, marker):
    identifier(scope, "canary scope")
    if not isinstance(marker, str) or not marker.isprintable() or not 8 <= len(marker) <= 256:
        raise InputError("use a unique printable synthetic marker of 8 to 256 characters")


class SQLiteCanaryScanner:
    def __init__(self, db: sqlite3.Connection, *, table: str, key: str, content: str, scope_column: str):
        names = table, key, content, scope_column
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in names):
            raise InputError("expected explicit SQLite table and column names")
        self.db = db
        # Use a qualified table name on every column so SQLite cannot treat a
        # misspelled double-quoted column as a literal string and return no hits.
        self.query = f"""
            SELECT CASE WHEN typeof(t."{content}") = 'text' THEN t."{key}" END AS copy_key
            FROM "{table}" t WHERE t."{scope_column}" COLLATE BINARY=?
              AND (typeof(t."{content}") NOT IN ('text', 'null') OR instr(t."{content}",?)>0) LIMIT 4097
        """

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        if self.db.in_transaction:
            raise InputError("commit application writes before scanning")
        rows = self.db.execute(self.query, (scope, marker)).fetchall()
        if any((row["copy_key"] if isinstance(row, Mapping) else row[0]) is None for row in rows):
            raise InputError("expected a text canary column and non-null copy keys")
        keys = _keys(rows)
        if len(rows) > 4096 or len(rows) != len(keys):
            raise InputError("canary scan needs at most 4096 distinct scoped keys")
        return keys


class PostgresCanaryScanner:
    """Read one scoped text column, including pgvector rows absent from nearest neighbors."""

    def __init__(self, connection, *, table: str, key: str, content: str, scope_column: str):
        self.connection = connection
        self.query = (f"SELECT t.{_sql_name(key)}::text AS copy_key FROM {_sql_name(table)} AS t "
                      f"WHERE t.{_sql_name(scope_column)}::text COLLATE \"C\" = %s "
                      f"AND strpos(t.{_sql_name(content)}, %s::text) > 0 LIMIT 4097")

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        with _pg_connection(self.connection) as conn:
            keys = _keys(conn.execute(self.query, (scope, marker)))
        if len(keys) > LIMIT:
            raise InputError("canary scan exceeds the scoped key limit")
        return keys


def contains_marker(value, marker):
    """Match text leaves and field names without joining unrelated text across a boundary."""
    if isinstance(value, str):
        return marker in value
    if isinstance(value, (bytes, bytearray)):
        return marker.encode() in value
    if isinstance(value, dict):
        return any(contains_marker(k, marker) or contains_marker(v, marker) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_marker(item, marker) for item in value)
    if value is None or isinstance(value, (bool, int, float)):
        return False
    raise InputError("canary field is not readable as text or JSON")


class ElasticsearchCanaryScanner:
    """Read the declared _source field for all in-scope documents in one refreshed PIT."""

    def __init__(self, client, index: str, *, scope_field: str, content_field: str):
        from .elastic import ElasticsearchAdapter
        self.index = ElasticsearchAdapter(client, index, subject_field=scope_field)
        self.client = client
        if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", content_field):
            raise InputError("choose an explicit Elasticsearch content field")
        self.content_field = content_field

    def find(self, *, scope: str, marker: str) -> set[str]:
        from .elastic import _complete
        validate_probe(scope, marker)
        index = self.index.index
        query = self.index.keyword_query(scope)
        _complete(self.client.indices.refresh(index=index), refresh=True)
        opened = self.client.open_point_in_time(index=index, keep_alive="1m", allow_partial_search_results=False)
        pit = opened["id"]
        seen, found = set(), set()
        cursor = {}
        try:
            _complete(opened)
            while True:
                page = self.client.search(pit={"id": pit, "keep_alive": "1m"}, query=query, sort=["_shard_doc"],
                                          size=128, source_includes=[self.content_field], allow_partial_search_results=False,
                                          **cursor)
                pit = page.get("pit_id", pit)
                _complete(page)
                hits = page["hits"]["hits"]
                for hit in hits:
                    key = identifier(hit["_id"], "Elasticsearch copy key")
                    if key in seen or len(seen) >= LIMIT:
                        raise InputError("ambiguous or oversized Elasticsearch canary scan")
                    seen.add(key)
                    value = hit["_source"]
                    for field in self.content_field.split("."):
                        value = value[field]  # An unreadable field cannot establish absence.
                    if contains_marker(value, marker):
                        found.add(key)
                if len(hits) < 128:
                    return found
                cursor = {"search_after": hits[-1]["sort"]}
        finally:
            self.client.close_point_in_time(id=pit)


class RedisCanaryScanner:
    """Scan a bounded fixture database atomically for strings in prefix:scope:key."""

    def __init__(self, client, *, prefix: str):
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(:[A-Za-z0-9_.-]+)*", prefix):
            raise InputError("choose an explicit Redis key prefix")
        self.client, self.prefix = client, prefix

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", scope):
            raise InputError("use a single Redis namespace component for the scope")
        # KEYS is bounded here by the size of the *whole* dedicated database. A
        # single read-only server operation avoids holes between SCAN pages.
        if self.client.info("replication").get("role") != "master" or self.client.info("cluster").get("cluster_enabled") != 0:
            raise InputError("use a standalone Redis primary for the canary fixture")
        script = """
        if redis.call('DBSIZE') > 4096 then return redis.error_reply('canary database too large') end
        local result = {}
        local bytes = 0
        for _, k in ipairs(redis.call('KEYS', ARGV[1] .. '*')) do
          bytes = bytes + redis.call('STRLEN', k)
          if bytes > 16777216 then return redis.error_reply('canary content budget exceeded') end
          if string.find(redis.call('GET', k), ARGV[2], 1, true) then
            table.insert(result, string.sub(k, string.len(ARGV[1]) + 1))
          end
        end
        return result
        """
        keys = self.client.execute_command("EVAL_RO", script, 0, f"{self.prefix}:{scope}:", marker)
        return {identifier(key.decode() if isinstance(key, bytes) else key, "Redis copy key") for key in keys}


class AnswerCacheCanaryScanner:
    """Read published SDK cache answers in the app's declared tenant and retrieval scope."""

    def __init__(self, cache, *, tenant: str):
        self.cache, self.tenant = cache, identifier(tenant, "tenant")

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        from setrya.local_cache import CacheKey

        scan = getattr(self.cache, "matching_answer_keys", None)
        if not callable(scan):
            raise InputError("this cache does not provide a consistent canary scan")
        found = set()
        for key in scan(self.tenant, scope, marker, LIMIT):
            if not isinstance(key, CacheKey) or key.tenant != self.tenant or key.retrieval_scope != scope:
                raise InputError("cache canary returned a key outside the declared scope")
            location = identifier(f"{key.tenant}:{key.access_scope}:{key.request}", "answer key")
            if location in found or len(found) >= LIMIT:
                raise InputError("ambiguous or oversized answer cache canary scan")
            found.add(location)
        return found
