"""Run erasure with awaiting application hooks, native PostgreSQL I/O and shared replay rules."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
from contextvars import copy_context
from functools import partial
from threading import Event

from .adapters import (
    _MEMORY_SCHEMA, _checkpoint_rows, _keys, _memory_keys, _memory_relations, _memory_rows,
    _memory_tables, memory_namespace,
)


@asynccontextmanager
async def _connection(client):
    from psycopg.pq import TransactionStatus

    async with (client.connection() if hasattr(client, "getconn") else nullcontext(client)) as conn:
        if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
            raise ValueError("use a dedicated idle PostgreSQL connection with autocommit=True")
        async with conn.transaction():
            await conn.execute("SET LOCAL row_security = off")
            yield conn


class AsyncPostgresCheckpointAdapter:
    """Subject-exclusive LangGraph AsyncPostgresSaver threads and all their physical rows."""
    proof = "direct"

    def __init__(self, saver):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        if not isinstance(saver, AsyncPostgresSaver):
            raise TypeError("expected LangGraph's full AsyncPostgresSaver")
        self.saver = saver

    async def find(self, subject):
        async with self.saver.lock, _connection(self.saver.conn) as conn:
            cur = await conn.execute(*_checkpoint_rows(subject))
            return _keys(await cur.fetchall())

    async def delete(self, subject):
        for thread in await self.find(subject):
            await self.saver.adelete_thread(thread)

    async def verify_absent(self, subject):
        return await self.find(subject)


class AsyncPostgresMemoryAdapter:
    """Direct reads of subject-exclusive AsyncPostgresStore rows, including expired items and embeddings."""
    proof = "direct"

    def __init__(self, store):
        from langgraph.store.postgres.aio import AsyncPostgresStore
        if not isinstance(store, AsyncPostgresStore):
            raise TypeError("expected LangGraph's AsyncPostgresStore")
        self.store = store

    async def _tables(self, conn):
        cur = await conn.execute(_MEMORY_SCHEMA)
        table, vectors = _memory_relations(await cur.fetchone())
        cur = await conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (vectors,))
        return _memory_tables(table, vectors, await cur.fetchone(), self.store.index_config)

    async def find(self, subject):
        async with self.store.lock, _connection(self.store.conn) as conn:
            tables = await self._tables(conn)
            cur = await conn.execute(*_memory_rows(tables, subject))
            return _memory_keys(await cur.fetchall(), subject)

    async def delete(self, subject):
        root = ".".join(memory_namespace(subject))
        async with self.store.lock, _connection(self.store.conn) as conn:
            for table in reversed(await self._tables(conn)):
                await conn.execute(f"DELETE FROM {table} WHERE prefix = %s OR starts_with(prefix, %s)", (root, root + "."))

    async def verify_absent(self, subject):
        return await self.find(subject)


class _AsyncHooks:
    """The evaluator runs off-loop. Each app/network operation runs on its owning loop."""
    def __init__(self, target, loop, stopping, reads):
        self.target = target
        self.loop = loop
        self.stopping = stopping
        self.reads = reads

    def __getattr__(self, name):
        method = getattr(self.target, name)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            async def invoke():
                if self.stopping.is_set() and name != "release_and_drain":
                    raise RuntimeError("erasure run cancelled")
                task = asyncio.current_task()
                interruptible = name in {"read", "inspect", "scan", "find", "verify_absent", "lineage", "checkpoint"}
                if interruptible:
                    self.reads.add(task)
                try:
                    return await method(*args, **kwargs)
                finally:
                    self.reads.discard(task)
            return asyncio.run_coroutine_threadsafe(invoke(), self.loop).result()

        return call


async def _run(runner, subject, *, application, adapters=None, **kwargs):
    loop = asyncio.get_running_loop()
    stopping = Event()
    reads = set()
    if adapters is not None:
        # Preserve object identity: the runner rejects a single direct adapter bound as two stores.
        wrappers = {}
        mapped = {}
        for name, adapter in adapters.items():
            if id(adapter) not in wrappers:
                wrappers[id(adapter)] = _AsyncHooks(adapter, loop, stopping, reads)
            mapped[name] = wrappers[id(adapter)]
        kwargs["adapters"] = mapped
    # Hooks may themselves await blocking I/O on the loop's default executor.
    # Reserve a separate coordinator thread so a busy default pool can still make progress.
    coordinator = ThreadPoolExecutor(max_workers=1, thread_name_prefix="setrya-erasure")
    run = partial(runner, subject, application=_AsyncHooks(application, loop, stopping, reads), **kwargs)
    task = loop.run_in_executor(coordinator, copy_context().run, run)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopping.set()
        for active in tuple(reads):
            active.cancel()
        # Finish any in-flight write and let the evaluator release its held worker before returning.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise
    finally:
        coordinator.shutdown(wait=False)


async def arun_shared(subject, *, application, **kwargs):
    """Use async inspect, scan, read, erase and worker hooks with the shared-object v3 checker."""
    from .v3 import run_shared
    return await _run(run_shared, subject, application=application, **kwargs)


async def arun_erasure(subject, *, application, adapters, **kwargs):
    """Use async application and adapter methods with the exclusive-subject v2 checker."""
    from .runner import run_erasure
    return await _run(run_erasure, subject, application=application, adapters=adapters, **kwargs)
