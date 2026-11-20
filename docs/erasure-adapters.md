# Connect a store to a subject erasure drill

An erasure adapter finds the copies of one synthetic subject in a declared store, deletes them, then reads the store again. Register the adapter under the name in the system’s `inventory.toml` entry. Journal each write with the same logical key that the adapter will return. Use opaque keys for rows, threads, and answers because they appear in the receipt.

| Store | Adapter in `setrya.erasure.adapters` | What one copy key identifies |
| --- | --- | --- |
| SQLite | `SQLiteAdapter(connection, "charts")` | A row with `id` and a scalar `setrya_subject` column. |
| PostgreSQL or pgvector | `PgvectorAdapter(connection, "chunks", metadata_column="metadata")` | A row ID. The adapter scans the subject field in the table, including vectors outside the top search results. Omit `metadata_column` to use a scalar `setrya_subject` column. |
| Elasticsearch 8 | `ElasticsearchAdapter(client, "chunks-001", subject_field="metadata.setrya_subject")` | A document’s `_id` in a single concrete index. Use an indexed `keyword` field with doc values. Setrya refreshes, pages through a point-in-time snapshot and checks the sequence number at deletion. A shared document or concurrent version change requires another run or an application-specific adapter. |
| Standalone Redis | `RedisAdapter(client)` | The opaque suffix supplied to `redis_key(subject, "answer-17")`. Store application data under the returned key on the same primary and database the adapter reads. The SDK answer cache has its own adapter, `setrya.erasure.cache.SubjectAnswerCacheAdapter`. |
| LangGraph SQLite and PostgreSQL checkpoints | `LangGraphSQLiteCheckpointAdapter(saver)` or `LangGraphPostgresCheckpointAdapter(saver)` | A full synchronous saver thread. Use `subject.langgraph_config("chart-chat")`; journal the resulting `thread_id`. Direct reads include pending writes and PostgreSQL checkpoint blobs, even when a checkpoint header is missing. |
| LangGraph long-term memory | `LangGraphMemoryAdapter(store)` for `InMemoryStore` or `LangGraphPostgresMemoryAdapter(store)` for `PostgresStore` | An item in `memory_namespace(subject)` or a child namespace. The root uses the stored key. For child namespaces, journal `memory_item_key(subject, namespace, key)`. The PostgreSQL adapter includes expired rows and their stored embeddings. |

## Set up a disposable copy

Give Setrya access to a test database or index. Pick a second synthetic subject for the retention control. Keep each row, key, thread, and memory namespace exclusive to its owner. A shared conversation or multi-person vector needs an application-specific adapter that knows how to redact only the selected person. For existing data, migrate the ownership field or thread naming scheme before relying on a drill.

Use a committed SQLite connection, or give the adapter a separate connection to the same file. Use a dedicated synchronous psycopg connection with `autocommit=True` or a pool configured that way. Its search path must resolve the application tables and the LangGraph schema used by the app. Run the store's normal migrations before discovery. The verifier needs visibility of the whole declared partition. PostgreSQL direct reads fail when row-level security would filter a query. For Elasticsearch, select an identity with complete access to the concrete index, including refresh and point-in-time search. Lists of matching copies reflect the systems and subject fields you have actually connected.

For example, to qualify the application’s SQLite rows in an integration test:

```python
import secrets
import sqlite3
from contextlib import closing

from setrya.erasure.adapters import SQLiteAdapter
from setrya.erasure.conformance import check_erasure_adapter
from setrya.erasure.subject import Subject

key = secrets.token_bytes(32)
alice = Subject.derive(key, "test-tenant", "person-a", "restricted")
bob = Subject.derive(key, "test-tenant", "person-b", "restricted")
with closing(sqlite3.connect(":memory:")) as db:
    db.execute("CREATE TABLE charts(id text PRIMARY KEY, setrya_subject text NOT NULL)")
    with db:
        db.executemany("INSERT INTO charts VALUES (?, ?)", [
            ("chart-1", alice.ref), ("chart-2", bob.ref)
        ])
    check_erasure_adapter(
        SQLiteAdapter(db, "charts"),
        subject=alice, retained=bob,
        expected={"chart-1"}, retained_expected={"chart-2"},
    )
```

## Contribute another direct adapter

Implement the synchronous `ErasureAdapter` protocol. Set `proof = "direct"` when the adapter can read the actual partition. `find(subject)` returns its logical copy keys. `delete(subject)` erases only that subject and can be retried. `verify_absent(subject)` performs a fresh read and returns the keys still present. An empty set means the read completed and found no copies. Raise an error on timeouts, missing credentials, or partial observations so replay can report the gap. Use `configuration` or `none` for systems where a direct read is unavailable.

Run `check_erasure_adapter` only against fixtures you can erase. It checks the exact key sets for two seeded subjects, deletes the first subject twice, and confirms the second subject survives both operations.
