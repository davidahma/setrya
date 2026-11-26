"""Find possible customer-data copy sites without importing application code."""

import ast
import json
import re
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path, PurePosixPath

from setrya.core.inputs import (
    InvalidReport,
    keys_match,
    read_regular_bytes,
    require,
    unique_fields,
)
from setrya.core.provenance import verify_pins
from setrya.git_refs import git_bytes, resolve_commit
from setrya.receipt_format import MAX_BUNDLE_FILE_BYTES, RECEIPT
from setrya.route_coverage import _blob, _path

from .source_scan import (
    _MAX_FILE_BYTES,
    _MAX_FILES,
    _MAX_ENTRIES,
    _MAX_PATH,
    _MAX_TOTAL_BYTES,
    _SKIP_DIRECTORIES,
    MAX_SOURCE_CANDIDATE_BYTES,
    MAX_SOURCE_CANDIDATES,
    _argument,
    _definition_header,
    _expression,
    _parse_tree,
    _scan_sources,
    _Scanner,
    _writes,
)
from . import typescript

SCAN_FORMAT = "setrya.copy-scan.v1"
REVIEW_FORMAT = "setrya.copy-review.v1"
MAP_FORMAT = "setrya.copy-site-mapping.v1"
MAP_FILE = "setrya-copy-coverage.json"
LANGUAGES = {
    "python": ("python",), "typescript": ("typescript", "javascript"),
    "all": ("python", "typescript", "javascript"),
}
LANGUAGE_LABELS = {"python": "Python", "typescript": "TypeScript", "javascript": "JavaScript"}
KINDS = {
    "file": "file write", "object": "object storage", "vector": "vector store",
    "cache": "cache", "memory": "agent memory", "model-state": "stateful model", "trace": "trace export",
    "database": "database write",
}
_STARTER_STORES = {
    "file": "file-archive", "object": "object-storage", "vector": "vector-index",
    "cache": "redis-answer-cache", "memory": "agent-memory", "model-state": "provider-conversation",
    "trace": "trace-store",
    "database": "database-records",
}
_PYTHON_STORAGE = frozenset({
    "anthropic", "boto3", "chromadb", "crewai", "elasticsearch", "google.cloud.storage",
    "graphiti_core", "langfuse", "langgraph", "langmem", "letta", "mem0", "openai",
    "opentelemetry", "pinecone", "psycopg", "psycopg2", "qdrant_client", "redis",
    "sqlalchemy", "sqlite3", "zep_cloud",
})
_IMPORTS = frozenset({
    "asyncio", "backoff", "celery", "concurrent", "dramatiq", "google", "retry", "retrying",
    "rq", "stamina", "temporalio", "tenacity", "threading",
}) | _PYTHON_STORAGE
_CLIENTS = {
    "anthropic.Anthropic": "$anthropic", "anthropic.AsyncAnthropic": "$anthropic",
    "asyncio.get_running_loop": "$event-loop", "asyncio.get_event_loop": "$event-loop",
    "boto3.Session": "boto3", "boto3.session.Session": "boto3",
    "celery.Celery": "$celery", "celery.app.base.Celery": "$celery",
    "chromadb.Client": "$chroma", "chromadb.PersistentClient": "$chroma", "chromadb.HttpClient": "$chroma",
    "concurrent.futures.ThreadPoolExecutor": "$thread-pool",
    "concurrent.futures.thread.ThreadPoolExecutor": "$thread-pool",
    "crewai.Memory": "$crewai-memory", "crewai.memory.Memory": "$crewai-memory",
    "crewai.memory.unified_memory.Memory": "$crewai-memory",
    "elasticsearch.Elasticsearch": "$elasticsearch", "elasticsearch.AsyncElasticsearch": "$elasticsearch",
    "$elasticsearch.options": "$elasticsearch",
    "google.cloud.storage.Client": "$gcs", "$gcs.bucket": "$gcs-bucket", "$gcs-bucket.blob": "$gcs-blob",
    "graphiti_core.Graphiti": "$graphiti",
    "langfuse.get_client": "$langfuse", "langfuse.Langfuse": "$langfuse",
    "langgraph.store.memory.InMemoryStore": "$langgraph-store", "langgraph.store.postgres.PostgresStore": "$langgraph-store",
    "langgraph.store.postgres.AsyncPostgresStore": "$langgraph-store",
    "langgraph.store.postgres.PostgresStore.from_conn_string": "$langgraph-store",
    "langgraph.store.postgres.AsyncPostgresStore.from_conn_string": "$langgraph-store",
    "langmem.ReflectionExecutor": "$reflection", "langmem.reflection.ReflectionExecutor": "$reflection",
    "langmem.reflection.LocalReflectionExecutor": "$reflection",
    "langmem.reflection.RemoteReflectionExecutor": "$reflection",
    "letta.Letta": "$letta", "letta.AsyncLetta": "$letta",
    "mem0.Memory": "$mem0", "mem0.Memory.from_config": "$mem0", "mem0.MemoryClient": "$mem0",
    "mem0.AsyncMemory": "$mem0", "mem0.AsyncMemory.from_config": "$mem0",
    "openai.OpenAI": "$openai", "openai.AsyncOpenAI": "$openai",
    "openai.AzureOpenAI": "$openai", "openai.AsyncAzureOpenAI": "$openai",
    "opentelemetry.sdk.trace.TracerProvider": "$tracing",
    "opentelemetry.trace.get_tracer": "$tracer", "$tracer.start_span": "$span",
    "opentelemetry.sdk.trace.export.ConsoleSpanExporter": "$span-exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter": "$span-exporter",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter": "$span-exporter",
    "pinecone.Pinecone": "$pinecone", "$pinecone.Index": "$pinecone-index",
    "psycopg.connect": "$psycopg", "psycopg.Connection.connect": "$psycopg",
    "psycopg.AsyncConnection.connect": "$psycopg", "psycopg2.connect": "$psycopg",
    "$psycopg.cursor": "$psycopg",
    "qdrant_client.QdrantClient": "$qdrant", "qdrant_client.AsyncQdrantClient": "$qdrant",
    "redis.Redis": "$redis", "redis.StrictRedis": "$redis", "redis.from_url": "$redis",
    "redis.Redis.from_url": "$redis", "redis.StrictRedis.from_url": "$redis",
    "redis.asyncio.Redis": "$redis", "redis.asyncio.Redis.from_url": "$redis",
    "redis.asyncio.from_url": "$redis", "$redis.pipeline": "$redis",
    "rq.Queue": "$rq-queue", "rq.queue.Queue": "$rq-queue",
    "sqlite3.connect": "$sqlite3", "sqlite3.dbapi2.connect": "$sqlite3", "$sqlite3.cursor": "$sqlite3",
    "sqlalchemy.create_engine": "$sqlalchemy-engine", "sqlalchemy.ext.asyncio.create_async_engine": "$sqlalchemy-engine",
    "$sqlalchemy-engine.begin": "$sqlalchemy", "$sqlalchemy-engine.connect": "$sqlalchemy",
    "sqlalchemy.orm.Session": "$sqlalchemy", "sqlalchemy.ext.asyncio.AsyncSession": "$sqlalchemy",
    "sqlalchemy.insert": "$sqlalchemy-write", "sqlalchemy.update": "$sqlalchemy-write",
    "$sqlalchemy-write.values": "$sqlalchemy-write", "$sqlalchemy-write.where": "$sqlalchemy-write",
    "$sqlalchemy-write.returning": "$sqlalchemy-write",
    "zep_cloud.Zep": "$zep", "zep_cloud.AsyncZep": "$zep",
}
_CLIENTS.update(dict.fromkeys((f"$chroma.{method}" for method in (
    "get_collection", "get_or_create_collection", "create_collection",
)), "$chroma-collection"))
_CLIENTS.update({"$s3.Bucket": "$s3", "$s3.Object": "$s3"})
_OPERATIONS = {
    "object": {"$s3": ("upload_file", "upload_fileobj", "put_object", "copy_object", "copy", "put"),
               "$gcs-blob": ("upload_from_filename", "upload_from_file", "upload_from_string", "open"),
               "$anthropic.beta.files": ("upload",), "$openai.files": ("create",)},
    "vector": {"$chroma-collection": ("add", "upsert", "update"),
               "$elasticsearch": ("index", "create", "update", "update_by_query", "bulk", "reindex"),
               "elasticsearch.helpers": ("bulk", "async_bulk", "streaming_bulk", "async_streaming_bulk",
                                         "parallel_bulk", "reindex"),
               "$pinecone-index": ("upsert", "upsert_records", "update"),
               "$qdrant": ("upsert", "upload_points", "upload_collection"),
               "$openai.vector_stores.files": ("create", "create_and_poll", "upload", "upload_and_poll"),
               "$openai.vector_stores.file_batches": ("create", "create_and_poll", "upload_and_poll")},
    "cache": {"$redis": ("set", "setex", "psetex", "mset", "msetnx", "hset", "hmset", "lpush",
                         "rpush", "sadd", "zadd", "xadd", "getset", "setnx")},
    "memory": {"$graphiti": ("add_episode", "add_episode_bulk"), "$zep.graph": ("add", "add_batch"),
               "$mem0": ("add", "update"), "$langgraph-store": ("put", "aput"),
               "$letta.agents.passages": ("create",), "$letta.agents.blocks": ("update",),
               "$reflection": ("submit",), "$crewai-memory": ("remember", "remember_many")},
    "model-state": {"$openai.conversations": ("create",), "$openai.conversations.items": ("create",),
                    "$openai.responses": ("create",), "$openai.chat.completions": ("create",),
                    "$bedrock-agent": ("invoke_agent", "create_session", "put_invocation_step")},
    "trace": {"$langfuse": ("start_span", "start_observation", "start_as_current_span",
                           "start_as_current_observation", "trace"),
              "$tracing": ("add_span_processor",), "$span-exporter": ("export",),
              "$span": ("set_attribute", "set_attributes", "add_event")},
    "database": {"$sqlalchemy": ("add", "add_all", "merge", "bulk_save_objects", "bulk_insert_mappings",
                                  "bulk_update_mappings")},
}
_CALLS = {f"{receiver}.{name}": kind for kind, receivers in _OPERATIONS.items()
          for receiver, names in receivers.items() for name in names}
_CALLS.update({"opentelemetry.trace.set_tracer_provider": "trace"})
WRITERS = {
    "celery": "Celery task", "rq": "RQ job", "dramatiq": "Dramatiq actor", "asyncio": "asyncio task or callback",
    "retry": "retry decorator", "temporal": "Temporal activity", "langmem": "LangMem reflection",
    "crewai": "CrewAI background memory save",
    "timer": "Node timer callback", "bullmq": "BullMQ worker",
    "thread": "Python thread or thread pool", "thread-timer": "Python thread timer",
}
_WRITER_DECORATORS = {
    "celery.shared_task": "celery", "celery.app.shared_task": "celery", "celery.current_app.task": "celery",
    "$celery.task": "celery", "rq.decorators.job": "rq", "dramatiq.actor": "dramatiq",
    "temporalio.activity.defn": "temporal", "tenacity.retry": "retry", "backoff.on_exception": "retry",
    "backoff.on_predicate": "retry", "retry.retry": "retry", "retrying.retry": "retry", "stamina.retry": "retry",
}
_BACKGROUND_OPERATIONS = {"$reflection.submit": "langmem", "$crewai-memory.remember_many": "crewai"}
_TASK_SCHEDULERS = {"asyncio.create_task": "coro", "asyncio.ensure_future": "coro_or_future",
                    "$event-loop.create_task": "coro"}
_CALLBACK_SCHEDULERS = {
    "threading.Thread": ("thread", 1, "target"), "threading.Timer": ("thread-timer", 1, "function"),
    "$thread-pool.submit": ("thread", 0, "fn"),
    "$rq-queue.enqueue": ("rq", 0, "f"), "$rq-queue.enqueue_call": ("rq", 0, "func"),
    "$rq-queue.enqueue_at": ("rq", 1, "f"), "$rq-queue.enqueue_in": ("rq", 1, "f"),
    "$event-loop.call_soon": ("asyncio", 0, "callback"), "$event-loop.call_soon_threadsafe": ("asyncio", 0, "callback"),
    "$event-loop.call_later": ("asyncio", 1, "callback"), "$event-loop.call_at": ("asyncio", 1, "callback"),
    "$event-loop.run_in_executor": ("asyncio", 1, "func"),
}
_SERVICE_FACTORIES = {"s3": "$s3", "bedrock-agent-runtime": "$bedrock-agent"}
_OID = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}")
_UNSCANNED_CODE = frozenset({
    ".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".go", ".java", ".kt",
    ".cs", ".rs", ".rb", ".php", ".sql", ".scala", ".swift", ".sh", ".ps1", ".cpp", ".c",
    ".pyw", ".ipynb", ".vue", ".svelte", ".astro",
})
_EXTENSION = re.compile(r"\.[a-z0-9]{1,12}")
_SKIP_FILE_SAMPLES = 6
_UNKNOWN_FILE_SAMPLES = 24
_MAX_SQL_LITERAL = 8 * 1024
_SQL_WRITE = re.compile(r"\s*(?:INSERT\s+INTO|UPDATE)\s+", re.IGNORECASE)
_SQL_TEXT = re.compile(r"'(?:''|[^'])*'|--[^\r\n]*|/\*[\s\S]*?\*/")
_ORDINARY_SQL_WRITE = re.compile(
    r"\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|REPLACE\s+INTO|MERGE\s+INTO)\s+", re.IGNORECASE,
)
_SQL_COPY_TABLE = re.compile(
    r"\s*CREATE\s+(?:(?:TEMP(?:ORARY)?|UNLOGGED)\s+)?(?:TABLE|MATERIALIZED\s+VIEW)\b"
    r"[\s\S]*?\bAS\s*\(*\s*(?:SELECT|TABLE|WITH|VALUES|EXECUTE)\b",
    re.IGNORECASE,
)
_SQL_NON_COPY = re.compile(r"\s*(?:SELECT|DELETE|EXPLAIN|CREATE\s+TABLE|DROP|ALTER|PRAGMA)\b", re.IGNORECASE)
_SQL_CLIENTS = frozenset({"$psycopg", "$sqlite3", "$sqlalchemy"})
_SQL_METHODS = frozenset({"execute", "executemany", "exec_driver_sql", "executescript"})
_SQL_ARGUMENTS = frozenset({"query", "sql", "statement", "sql_statement"})
_VECTOR_CAST = re.compile(r'::\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*|"[A-Za-z_][A-Za-z0-9_]*")\s*\.\s*)?'
                          r'(?:vector|halfvec|sparsevec|"vector"|"halfvec"|"sparsevec")(?![A-Za-z0-9_"])', re.IGNORECASE)
_ELASTIC_ENDPOINT = re.compile(r"(?:https?://[^/?#\s]+)?(?:/[A-Za-z0-9_.~%+-]+)?/"
                               r"(?P<endpoint>_doc|_create|_update)(?P<record>/[A-Za-z0-9_.~%+-]+)?(?:\?[^#\s]*)?")


@dataclass(frozen=True)
class CopySite:
    id: str
    path: str
    line: int
    kind: str
    operation: str
    writer: str | None = None

    def wire(self):
        result = asdict(self)
        result["site_id"] = result.pop("id")
        test = ("setrya-case/case.yml" if self.writer is None and typescript.is_source(self.path)
                else "setrya-case/test_erasure.py")
        store = "sqlite-records" if self.operation.startswith("node:sqlite.") else _STARTER_STORES[self.kind]
        result["starter"] = {"site_id": self.id, "store": store,
                             "key": "replace-with-disposable-test-key", "test": test}
        if self.writer is None:
            result.pop("writer")
        else:
            result["starter"]["mode"] = "late-write"
        return result


@dataclass(frozen=True)
class _Reference:
    name: str
    anchor: str = ""
    function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | None = None
    async_api: bool = False


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class _CopyScanner(_Scanner):
    def __init__(self, path, tree):
        super().__init__(path, tree)
        self.function = None
        self.module_functions = {
            node.name: _Reference("$local-function", function=node) for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and self.names[node.name] == 1
            and node.name not in self.external and not self.star_import
        }
        self.enclosing_names = ()
        self.writers = {}
        self.scheduled_calls = set()
        self.local_calls = defaultdict(dict)
        self.site_owners = {}
        self.imports_storage = False
        self.unresolved_sql = False
        self.methods = {}
        self.receivers = {}
        self.method_fields = {}
        self.reassigned_fields = set()

    def _bind(self, name, value):
        reference = self._reference(value) if isinstance(value, ast.AST) else None
        super()._bind(name, reference if reference is not None else value)

    def visit_Import(self, node):
        super().visit_Import(node)
        for item in node.names:
            self._storage_import(item.name)
            root = item.name.split(".")[0]
            if root in _IMPORTS:
                self._bind(item.asname or root, _Reference(item.name if item.asname else root))

    def visit_ImportFrom(self, node):
        super().visit_ImportFrom(node)
        if node.level == 0 and node.module and node.module.split(".")[0] in _IMPORTS:
            for item in node.names:
                self._storage_import(node.module + "." + item.name)
                self._bind(item.asname or item.name, _Reference(node.module + "." + item.name))

    def _storage_import(self, module):
        self.imports_storage |= any(module == name or module.startswith(name + ".") for name in _PYTHON_STORAGE)

    def _known_arguments(self, node):
        args = [*node.args, *(row.value for row in node.keywords)]
        return [self._value(arg).anchors for arg in args]

    def _scope(self, body, name, *, arguments=None, class_scope=False):
        previous = self.enclosing_names
        if self.scope and self.class_parent is None:
            self.enclosing_names = (*previous, self.names)
        super()._scope(body, name, arguments=arguments, class_scope=class_scope)
        self.enclosing_names = previous

    def _reference(self, node):
        if isinstance(node, ast.Lambda):
            return _Reference("$local-function", function=node)
        if isinstance(node, ast.Await):
            return self._reference(node.value)
        if isinstance(node, ast.Name):
            value = self.values.get(node.id)
            return value if isinstance(value, _Reference) else None
        if isinstance(node, ast.Attribute):
            member = self._own_member(node)
            if member is not None and member in self.method_fields:
                return self.method_fields[member]
            base = self._reference(node.value)
            if base is not None:
                return _Reference(base.name + "." + node.attr, base.anchor, async_api=base.async_api)
        if isinstance(node, ast.Call):
            func = self._reference(node.func)
            if func is None:
                return None
            client = _CLIENTS.get(func.name)
            if func.name in {"boto3.client", "boto3.resource"}:
                client = _SERVICE_FACTORIES.get(self._value(_argument(node, 0, "service_name")).text)
            if client:
                async_api = func.async_api or func.name.startswith("redis.asyncio.") or ".Async" in func.name
                return _Reference(client, _digest([func.anchor, _expression(node), self._known_arguments(node)]),
                                  async_api=async_api)
        return None

    def _writer_reference(self, node):
        reference = self._reference(node)
        if isinstance(node, ast.Attribute) and reference is None:
            member = self._own_member(node)
            if member is not None and member not in self.reassigned_fields and member in self.methods:
                return _Reference("$local-function", function=self.methods[member])
        if reference is not None or self.function is None or not isinstance(node, ast.Name):
            return reference
        # Function bodies can see module definitions made after their own definition.
        # Keep forward lookup out of top-level statements and ordinary value binding.
        target = self.module_functions.get(node.id)
        if target and node.id not in self.names and all(node.id not in names for names in self.enclosing_names):
            return target
        return None

    @staticmethod
    def _receiver(method):
        args = (*method.args.posonlyargs, *method.args.args)
        return args[0].arg if args else None

    def _own_member(self, node):
        if (isinstance(node.value, ast.Name) and node.value.id == self.receivers.get(self.function)
                and self.names[node.value.id] == 1):
            return node.attr
        return None

    def visit_ClassDef(self, node):
        previous = self.methods, self.receivers, self.method_fields, self.reassigned_fields
        counts = _writes(node.body)
        methods = {item.name: item for item in node.body
                   if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and not item.decorator_list and counts[item.name] == 1}
        assignments = Counter()
        for method in methods.values():
            receiver = self._receiver(method)
            assignments.update(part.attr for stmt in method.body for part in ast.walk(stmt)
                               if isinstance(part, ast.Attribute) and isinstance(part.ctx, (ast.Store, ast.Del))
                               and isinstance(part.value, ast.Name) and part.value.id == receiver)
        fields = {}
        constructor = methods.get("__init__")
        if constructor is not None:
            receiver = self._receiver(constructor)
            bindings = _writes(constructor.body, constructor.args)
            for statement in constructor.body:
                if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                    target = statement.targets[0]
                elif isinstance(statement, ast.AnnAssign):
                    target = statement.target
                else:
                    continue
                value = statement.value
                if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id == receiver and bindings[receiver] == 1
                        and assignments[target.attr] == 1 and isinstance(value, ast.Call)):
                    continue
                root = value.func
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and not bindings[root.id]:
                    reference = self._reference(value)
                    if reference is not None and reference.name == "$thread-pool":
                        fields[target.attr] = reference
        self.methods, self.method_fields, self.reassigned_fields = methods, fields, set(assignments)
        self.receivers = {method: self._receiver(method) for method in methods.values()}
        super().visit_ClassDef(node)
        self.methods, self.receivers, self.method_fields, self.reassigned_fields = previous

    def visit_FunctionDef(self, node):
        writer = None
        for decorator in node.decorator_list:
            reference = self._reference(decorator.func if isinstance(decorator, ast.Call) else decorator)
            if reference and (kind := _WRITER_DECORATORS.get(reference.name)):
                # A retry wrapper can run in-line. Prefer an explicit worker decorator on the same function.
                writer = kind if writer in (None, "retry") else writer
        if writer:
            self.writers[node] = writer
        for expression in _definition_header(node):
            self.visit(expression)
        self._bind(node.name, _Reference("$local-function", function=node))
        previous, self.function = self.function, node
        self._scope(node.body, node.name, arguments=node.args)
        self.function = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.visit(node.args)
        previous, self.function = self.function, node
        self._scope((node.body,), "<lambda>", arguments=node.args)
        self.function = previous

    def visit_With(self, node):
        previous = self.values.copy()
        for item in node.items:
            self.visit(item.context_expr)
            if isinstance(item.optional_vars, ast.Name):
                self._bind(item.optional_vars.id, item.context_expr)
        for statement in node.body:
            self.visit(statement)
        self.values = previous

    visit_AsyncWith = visit_With

    def _scheduled_callback(self, node, reference):
        task_arg = _TASK_SCHEDULERS.get(reference.name)
        if task_arg:
            coroutine = _argument(node, 0, task_arg)
            if isinstance(coroutine, ast.Call):
                target = self._writer_reference(coroutine.func)
                if target and isinstance(target.function, ast.AsyncFunctionDef):
                    self.writers.setdefault(target.function, "asyncio")
                elif target and target.async_api:
                    self.scheduled_calls.add(coroutine)
        if signature := _CALLBACK_SCHEDULERS.get(reference.name):
            writer, index, keyword = signature
            target = self._writer_reference(_argument(node, index, keyword))
            if target and target.function is not None:
                self.writers.setdefault(target.function, writer)
            elif target and not target.async_api and (target.name in _CALLS or self._sql_binding(target)):
                return target, writer
        return None, None

    def scan(self):
        self.visit(self.tree)
        pending = deque(self.writers)
        while pending:
            caller = pending.popleft()
            for callee in self.local_calls.get(caller, ()):
                if callee not in self.writers:
                    self.writers[callee] = self.writers[caller]
                    pending.append(callee)
        return [replace(site, writer=site.writer or self.writers.get(self.site_owners[site.id])) for site in self.found]

    def _provider_call(self, node, name):
        if name in {"$openai.responses.create", "$openai.chat.completions.create"}:
            args = {kw.arg: kw.value for kw in node.keywords}
            store = args.get("store")
            if name == "$openai.chat.completions.create" and store is None and None not in args:
                return False
            if isinstance(store, ast.Constant) and store.value is False and "conversation" not in args:
                return False
        if name == "$gcs-blob.open":
            return self._value(_argument(node, 0, "mode")).text in {"w", "wb", "wt"}
        return True

    def _query_text(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value if len(node.value) <= _MAX_SQL_LITERAL else ""
        return self._value(node).text or ""

    def _endpoint_text(self, node):
        if isinstance(node, ast.JoinedStr):
            return "".join(part.value if isinstance(part, ast.Constant) else "record" for part in node.values)
        return self._value(node).text or ""

    def _vector_literal(self, node):
        # A static SQL vector cast or a document-write REST route makes these
        # specific calls review leads even when the receiver is an app wrapper.
        if not isinstance(node.func, ast.Attribute):
            return None
        method = node.func.attr
        if method in {"execute", "executemany"}:
            query = self._query_text(_argument(node, 0, "query"))
            masked = _SQL_TEXT.sub(" ", query)
            if _SQL_WRITE.match(masked) and _VECTOR_CAST.search(masked):
                return "pgvector." + method
        if method == "request":
            verb = self._value(_argument(node, 0, "method")).text
            if verb not in {"PUT", "POST"}:
                return None
            endpoint = self._endpoint_text(_argument(node, 1, "endpoint"))
            if not endpoint:
                endpoint = self._endpoint_text(_argument(node, 1, "url"))
            matched = _ELASTIC_ENDPOINT.fullmatch(endpoint) if len(endpoint) <= _MAX_PATH else None
            if matched:
                route = matched["endpoint"]
                if route == "_doc" and (matched["record"] or verb == "POST"):
                    return "elasticsearch-http.document"
                if matched["record"] and (route == "_create" or (route == "_update" and verb == "POST")):
                    return "elasticsearch-http." + route.removeprefix("_")
        return None

    @staticmethod
    def _sql_binding(reference):
        if reference is None:
            return None
        receiver, _, method = reference.name.rpartition(".")
        return (receiver, method) if receiver in _SQL_CLIENTS and method in _SQL_METHODS else None

    @staticmethod
    def _sql_argument(node, scheduler):
        if scheduler in ("threading.Thread", "threading.Timer"):
            index = 3 if scheduler == "threading.Thread" else 2
            args = _argument(node, index, "args")
            return args.elts[0] if isinstance(args, (ast.Tuple, ast.List)) and args.elts else None
        if scheduler not in (None, "$thread-pool.submit"):
            return None
        index = int(scheduler is not None)
        if index < len(node.args):
            return node.args[index]
        return next((row.value for row in node.keywords if row.arg in _SQL_ARGUMENTS), None)

    def _sql_operation(self, node, reference, scheduler):
        binding = self._sql_binding(reference)
        if binding is None:
            return None
        receiver, method = binding
        statement = self._sql_argument(node, scheduler)
        statement_ref = self._reference(statement)
        if statement_ref and statement_ref.name == "$sqlalchemy-write":
            return receiver[1:] + "." + method
        if isinstance(statement, ast.Call):
            function = self._reference(statement.func)
            if function and function.name == "sqlalchemy.text":
                statement = _argument(statement, 0, "text")
        query = _SQL_TEXT.sub(" ", self._query_text(statement))
        statements = [part for part in query.split(";") if part.strip()]
        if any(_ORDINARY_SQL_WRITE.match(part) or _SQL_COPY_TABLE.match(part) for part in statements):
            return receiver[1:] + "." + method
        if not statements or not all(_SQL_NON_COPY.match(part) for part in statements):
            self.unresolved_sql = True
        return None

    def visit_Call(self, node):
        reference = scheduler = self._writer_reference(node.func)
        target, writer = self._scheduled_callback(node, reference) if reference else (None, None)
        if reference and reference.function is not None and self.function is not None:
            self.local_calls[self.function][reference.function] = None
        reference = target or reference
        if reference and reference.name.startswith(("openai.responses.", "openai.conversations.",
                                                     "openai.chat.", "openai.files.", "openai.vector_stores.")):
            reference = _Reference("$" + reference.name, reference.anchor)
        kind = _CALLS.get(reference.name) if reference else None
        destination = self._destination(node)
        if destination:
            kind = "file"
            operation = destination[0]
            anchor = self._value(destination[1]).anchors
        elif kind and self._provider_call(node, reference.name):
            operation = reference.name.removeprefix("$")
            anchor = reference.anchor
        elif operation := self._vector_literal(node):
            kind = "vector"
            anchor = ""
        elif operation := self._sql_operation(node, reference, scheduler.name if target else None):
            kind = "database"
            anchor = reference.anchor
        else:
            self.generic_visit(node)
            return
        stem = _digest([self.path, self.scope, kind, operation, _expression(node), anchor, self._known_arguments(node)])
        occurrence = self.occurrences[stem]
        self.occurrences[stem] += 1
        identity = _digest([stem, occurrence])
        if reference and writer is None:
            writer = _BACKGROUND_OPERATIONS.get(reference.name)
        if node in self.scheduled_calls and writer is None:
            writer = "asyncio"
        self.site_owners[identity] = self.function
        self.found.append(CopySite(identity, self.path, node.lineno, kind, operation, writer))
        require(len(self.found) <= MAX_SOURCE_CANDIDATES, "Copy scan exceeded its candidate limit.")
        self.generic_visit(node)


def _inspect(raw, path, *, report_unknown=None):
    scanner = _CopyScanner(path, _parse_tree(raw, path))
    sites = scanner.scan()
    if report_unknown:
        if scanner.unresolved_sql:
            report_unknown(path, "unresolved-storage-write")
        elif scanner.imports_storage and not sites:
            report_unknown(path, "storage-import-without-write")
    return sites


class _SourceScope:
    def __init__(self, language):
        require(language in LANGUAGES, "Choose python, typescript, or all for the copy scan.")
        self.languages = LANGUAGES[language]
        self.label = ", ".join(LANGUAGE_LABELS[item] for item in self.languages)
        self.parsers = typescript.load_parser() if "typescript" in self.languages else None
        self.skip = _SKIP_DIRECTORIES | typescript.SKIP_DIRECTORIES
        self.skipped = Counter()
        self.skipped_samples = defaultdict(list)
        self.unknown = {}

    @property
    def metadata(self):
        metadata = {"languages": list(self.languages)} if self.parsers else {}
        if self.skipped or self.unknown:
            skipped = [
                {"extension": ext, "count": count, "paths": self.skipped_samples[ext],
                 "source": ext in _UNSCANNED_CODE}
                for ext, count in sorted(self.skipped.items())
            ]
            unknown_sources = len(self.unknown) + sum(row["count"] for row in skipped if row["source"])
            metadata["coverage"] = {
                "status": "unknown" if unknown_sources else "scanned",
                "unknown_sources": unknown_sources,
                "unresolved": [{"path": path, "reason": reason}
                               for path, reason in sorted(self.unknown.items())[:_UNKNOWN_FILE_SAMPLES]],
                "skipped_extensions": skipped,
            }
        return metadata

    def contains(self, path):
        return (("python" in self.languages and path.endswith(".py"))
                or (self.parsers is not None and typescript.is_source(path)))

    def skip_file(self, path):
        ext = PurePosixPath(path).suffix.lower()
        if path.endswith((".d.ts", ".d.cts", ".d.mts")):
            ext = ".declaration"
        if ext and not _EXTENSION.fullmatch(ext):
            ext = "(other)"
        if ext not in _UNSCANNED_CODE and ext not in self.skipped and len(self.skipped) >= 64:
            ext = "(other)"
        self.skipped[ext or "(none)"] += 1
        if (ext in _UNSCANNED_CODE and len(self.skipped_samples[ext]) < _SKIP_FILE_SAMPLES
                and len(path) <= _MAX_PATH and path.isprintable()):
            self.skipped_samples[ext].append(path)

    def report_unknown(self, path, reason):
        self.unknown[path] = reason

    def inspect(self, raw, path, *, report=True):
        report_unknown = self.report_unknown if report else None
        if path.endswith(".py"):
            return _inspect(raw, path, report_unknown=report_unknown)
        return typescript.inspect(raw, path, self.parsers, CopySite, _digest, MAX_SOURCE_CANDIDATES,
                                  report_unknown=report_unknown)


def scan(root, *, language="python"):
    scope = _SourceScope(language)
    sites = _scan_sources(root, scope.inspect, is_source=scope.contains,
                          skip_directories=scope.skip, source_label=scope.label, on_skip=scope.skip_file)
    return {"format": SCAN_FORMAT, **scope.metadata,
            "sites": [site.wire() for site in sorted(sites, key=lambda site: site.writer is None)]}


def _mapping(raw):
    if raw is None:
        return {}
    require(len(raw) <= MAX_SOURCE_CANDIDATE_BYTES, "Copy mapping exceeds its byte limit.")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
    require(keys_match(data, {"format", "sites"}) and data["format"] == MAP_FORMAT
            and isinstance(data["sites"], list) and len(data["sites"]) <= MAX_SOURCE_CANDIDATES,
            "Invalid copy mapping format.")
    sites = {}
    for site in data["sites"]:
        fields = {"site_id", "store", "key", "test"}
        require(keys_match(site, fields) or keys_match(site, fields | {"mode"}), "Invalid copy mapping entry.")
        identity = site["site_id"]
        require(isinstance(identity, str) and _OID.fullmatch(identity) and identity not in sites
                and all(isinstance(site[key], str) and _TOKEN.fullmatch(site[key]) for key in ("store", "key"))
                and site.get("mode", "deletion") in ("deletion", "late-write", "write-back"),
                "Invalid or duplicate copy mapping.")
        _path(site["test"])
        require(len(site["test"]) <= _MAX_PATH, "Copy mapping test path exceeds its limit.")
        sites[identity] = site
    return sites


def _covered(receipts, candidate):
    from setrya.receipts import replay_bytes

    subjects, incomplete = {}, set()
    for bundle in receipts:
        bundle = Path(bundle)
        receipt = read_regular_bytes(bundle / RECEIPT, "copy-site receipt", MAX_BUNDLE_FILE_BYTES)
        result = replay_bytes(bundle, receipt)
        try:
            verify_pins(result, expected_app_commit=candidate)
        except InvalidReport:
            continue
        entrypoint = next(row["path"] for row in result["provenance"]["test_files"] if row["role"] == "entrypoint")
        digest = sha256(receipt).hexdigest()
        for artifact in result.get("case", {}).get("artifacts", []):
            if artifact["action"] in {"delete", "rewrite"}:
                identity = artifact["store"], artifact["key"], entrypoint
                if result.get("technical_verdict") == "PASS":
                    modes = subjects.setdefault(identity, {})
                    modes.setdefault("deletion", digest)
                    if result.get("mode") == "late-write" and result.get("race") == "exercised":
                        modes.setdefault("late-write", digest)
                else:
                    incomplete.add(identity)
    return {identity: digest for identity, digest in subjects.items() if identity not in incomplete}


def _new_sites(repo, ancestor, candidate, scope):
    rows = git_bytes(repo, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--name-only",
                     "--diff-filter=AMT", "-z", ancestor, candidate, "--")
    require(len(rows) <= _MAX_TOTAL_BYTES, "Changed source paths exceed the scan byte limit.")
    require(rows.count(b"\0") <= _MAX_ENTRIES, "Changed paths exceed the scan entry limit.")
    suffixes = (b".py",) if "python" in scope.languages else ()
    if scope.parsers:
        suffixes += tuple(suffix.encode("ascii") for suffix in typescript.SUFFIXES)
    paths = []
    for raw in rows.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8", errors="surrogateescape")
        if scope.skip.intersection(path.split("/")[:-1]):
            continue
        if raw.endswith(suffixes) and scope.contains(path):
            paths.append(path)
        else:
            scope.skip_file(path)
    require(len(paths) <= _MAX_FILES, f"Changed {scope.label} files exceed the scan file limit.")
    total_bytes = report_bytes = 0
    found = []
    for path in paths:
        _path(path)
        require(len(path) <= _MAX_PATH, "Source path exceeds its scan limit.")
        before, after = (_blob(repo, ref, path) or b"" for ref in (ancestor, candidate))
        total_bytes += len(before) + len(after)
        require(max(len(before), len(after)) <= _MAX_FILE_BYTES and total_bytes <= _MAX_TOTAL_BYTES,
                f"Changed {scope.label} source exceeds the scan byte limit.")
        previous = {site.id: site.writer for site in scope.inspect(before, path, report=False)}
        added = [site.wire() for site in scope.inspect(after, path)
                 if site.id not in previous or (site.writer is not None and site.writer != previous[site.id])]
        report_bytes += sum(len(json.dumps(site)) for site in added)
        found.extend(added)
        require(len(found) <= MAX_SOURCE_CANDIDATES and report_bytes <= MAX_SOURCE_CANDIDATE_BYTES,
                "Copy review exceeded its candidate limit.")
    return sorted(found, key=lambda row: ("writer" not in row, row["path"], row["line"], row["site_id"]))


def check(repo, base_ref, candidate_ref="HEAD", *, map_path=MAP_FILE, receipts=(), language="python"):
    """Compare changed Git blobs and bind new sites to reviewed mappings and replayed evidence."""
    repo = Path(git_bytes(Path(repo), "rev-parse", "--show-toplevel").decode("utf-8").rstrip("\n"))
    base, candidate = (resolve_commit(repo, ref) for ref in (base_ref, candidate_ref))
    ancestor = resolve_commit(repo, git_bytes(repo, "merge-base", base, candidate).decode("ascii").strip())
    map_path = _path(map_path)
    approved = _mapping(_blob(repo, ancestor, map_path))
    declared = _mapping(_blob(repo, candidate, map_path))
    require(len(receipts) <= 32, "Supply up to 32 candidate receipts.")
    replayed = _covered(receipts, candidate) if receipts else {}
    scope = _SourceScope(language)
    sites = _new_sites(repo, ancestor, candidate, scope)
    for site in sites:
        baseline = approved.get(site["site_id"])
        binding = declared.get(site["site_id"])
        digest = None
        if binding and binding == baseline:
            mode = "late-write" if "writer" in site or binding.get("mode") in ("late-write", "write-back") else "deletion"
            digest = replayed.get((binding["store"], binding["key"], binding["test"]), {}).get(mode)
        site.update(status="evidence-replayed" if digest else "needs-review", binding=binding, receipt_sha256=digest)
    return {"format": REVIEW_FORMAT, **scope.metadata, "base_commit": base, "merge_base_commit": ancestor,
            "candidate_commit": candidate, "map_changed": approved != declared,
            "review_required": sum(row["status"] == "needs-review" for row in sites), "sites": sites}


def summarize(report):
    reviewed = report["format"] == REVIEW_FORMAT
    languages = ", ".join(LANGUAGE_LABELS[item] for item in report.get("languages", ("python",)))
    lines = ["Changed copy sites" if reviewed else "Possible copy sites", f"Source languages: {languages}"]
    if reviewed:
        lines.append(f"Candidate {report['candidate_commit']} · merge base {report['merge_base_commit']}")
        if report["map_changed"]:
            lines.append("The PR changes the copy mapping. Review the proposed bindings.")
    coverage = report.get("coverage", {})
    if coverage.get("status") == "unknown":
        lines.append(f"Coverage unknown · {coverage['unknown_sources']} source file(s) need review.")
    for row in coverage.get("unresolved", ()):
        lines.append(f"UNKNOWN  {ascii(row['path'])[1:-1]} · {row['reason']}")
    for row in coverage.get("skipped_extensions", ()):
        lines.append(f"SKIPPED  {row['extension']} · {row['count']} file(s)")
    for site in report["sites"]:
        status = site.get("status", "detected").upper()
        name = ascii(site["path"])[1:-1]
        lines.append(f"{status}  {name}:{site['line']} · {KINDS[site['kind']]} · {site['operation']}")
        if writer := site.get("writer"):
            lines.append(f"  Possible deferred writer · {WRITERS[writer]}. Hold it across a deletion.")
        if site.get("status") != "evidence-replayed":
            lines.append("  Starter mapping " + json.dumps(site["starter"], separators=(",", ":")))
    if not report["sites"] and coverage.get("status") != "unknown":
        lines.append("No supported copy operations found in the scanned scope.")
    return "\n".join(lines)


def review_needed(report):
    return bool(report.get("review_required") or report.get("coverage", {}).get("unknown_sources"))
