"""Recognize copy operations on statically resolved JavaScript and TypeScript clients."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from time import monotonic

from setrya.core.inputs import InvalidReport, require

from .source_scan import _MAX_AST_DEPTH, _MAX_AST_NODES

SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".jsx")
JAVASCRIPT_SUFFIXES = frozenset((".js", ".mjs", ".cjs", ".jsx"))
SKIP_DIRECTORIES = {"node_modules", ".next", ".turbo"}
UNRECOGNIZED_STORAGE = "storage-import-without-write"
UNRESOLVED_WRITE = "unresolved-storage-write"
_FUNCTIONS = {
    "function_declaration", "function_expression", "arrow_function", "generator_function",
    "generator_function_declaration", "method_definition",
}
_SCOPES = _FUNCTIONS | {
    "program", "statement_block", "class", "class_body", "catch_clause", "for_statement",
    "for_in_statement", "switch_body", "field_definition", "public_field_definition",
}
_MODULES = {
    "fs": "fs", "node:fs": "fs", "fs/promises": "fs.promises", "node:fs/promises": "fs.promises",
    "timers": "timers", "node:timers": "timers", "process": "process", "node:process": "process",
    "node:sqlite": "node:sqlite", "pg": "pg", "postgres": "postgres", "@prisma/client": "prisma",
    "drizzle-orm": "drizzle", "kysely": "kysely",
    "redis": "redis", "ioredis": "ioredis", "@upstash/redis": "upstash",
    "@aws-sdk/client-s3": "s3", "@pinecone-database/pinecone": "pinecone",
    "@qdrant/js-client-rest": "qdrant", "@langchain/langgraph": "langgraph",
    "@langchain/langgraph/web": "langgraph", "@langchain/langgraph-checkpoint": "langgraph",
    "@langchain/langgraph-checkpoint-postgres": "langgraph",
    "@langchain/langgraph-checkpoint-sqlite": "langgraph",
    "@langchain/langgraph-checkpoint-redis": "langgraph",
    "mem0ai": "mem0", "mem0ai/oss": "mem0", "openai": "openai", "langfuse": "langfuse",
    "ai": "ai-sdk", "@ai-sdk/openai": "ai-openai", "@mastra/memory": "mastra",
    "@anthropic-ai/sdk": "anthropic", "@anthropic-ai/claude-agent-sdk": "claude-agent",
    "@letta-ai/letta-client": "letta", "chromadb": "chroma",
    "@supabase/supabase-js": "supabase", "@upstash/vector": "upstash-vector",
    "@opentelemetry/sdk-trace-node": "otel", "@opentelemetry/sdk-trace-base": "otel",
    "bullmq": "bullmq",
}
_STORAGE = frozenset(_MODULES.values()) - {"timers", "process", "bullmq", "ai-openai"}
_DEFAULTS = {"openai": "OpenAI", "ioredis": "Redis", "anthropic": "Anthropic", "letta": "Letta"}
_CLIENTS = {
    "redis.createClient": "$redis", "redis.createCluster": "$redis", "$redis.duplicate": "$redis",
    "ioredis": "$redis", "ioredis.Redis": "$redis", "ioredis.Cluster": "$redis", "upstash.Redis": "$redis",
    "upstash.Redis.fromEnv": "$redis", "s3.S3Client": "$s3", "s3.S3": "$s3",
    "pinecone.Pinecone": "$pinecone", "$pinecone.index": "$pinecone-index",
    "$pinecone.Index": "$pinecone-index", "$pinecone-index.namespace": "$pinecone-index",
    "qdrant.QdrantClient": "$qdrant", "langgraph.InMemoryStore": "$langgraph-store",
    "langgraph.StateGraph": "$langgraph-builder",
    "langgraph.MemorySaver": "$langgraph-checkpointer", "langgraph.PostgresSaver": "$langgraph-checkpointer",
    "langgraph.SqliteSaver": "$langgraph-checkpointer", "langgraph.RedisSaver": "$langgraph-checkpointer",
    "langgraph.PostgresSaver.fromConnString": "$langgraph-checkpointer",
    "langgraph.SqliteSaver.fromConnString": "$langgraph-checkpointer",
    "langgraph.RedisSaver.fromUrl": "$langgraph-checkpointer",
    "mem0.Memory": "$mem0", "mem0.MemoryClient": "$mem0",
    "openai": "$openai", "openai.OpenAI": "$openai", "openai.AzureOpenAI": "$openai",
    "langfuse.Langfuse": "$langfuse", "otel.NodeTracerProvider": "$otel",
    "otel.BasicTracerProvider": "$otel", "node:sqlite.DatabaseSync": "$sqlite",
    "pg.Client": "$pg", "pg.Pool": "$pg-pool", "$pg-pool.connect": "$pg",
    "postgres": "$postgres", "prisma.PrismaClient": "$prisma",
    "drizzle.drizzle": "$drizzle", "kysely.Kysely": "$kysely",
    "ai-openai.openai": "$ai-responses", "ai-openai.openai.responses": "$ai-responses",
    "ai-openai.openai.chat": "$ai-chat", "mastra.Memory": "$mastra",
    "anthropic": "$anthropic", "anthropic.Anthropic": "$anthropic",
    "letta": "$letta", "letta.Letta": "$letta",
    "chroma.ChromaClient": "$chroma", "chroma.CloudClient": "$chroma",
    "$chroma.getCollection": "$chroma-collection", "$chroma.createCollection": "$chroma-collection",
    "$chroma.getOrCreateCollection": "$chroma-collection",
    "supabase.createClient": "$supabase", "$supabase.from": "$supabase-query",
    "$supabase.schema": "$supabase", "$supabase.storage.from": "$supabase-bucket",
    "upstash-vector.Index": "$upstash-vector", "upstash-vector.Index.fromEnv": "$upstash-vector",
    "$upstash-vector.namespace": "$upstash-vector",
}
_TYPED_CLIENTS = {
    "redis.RedisClientType": "$redis", "redis.RedisClusterType": "$redis",
    "pg.PoolClient": "$pg", "postgres.Sql": "$postgres", "postgres.TransactionSql": "$postgres",
    "drizzle.NodePgDatabase": "$drizzle", "drizzle.PostgresJsDatabase": "$drizzle",
    "langgraph.BaseCheckpointSaver": "$langgraph-checkpointer", "langgraph.BaseStore": "$langgraph-store",
    "chroma.Collection": "$chroma-collection", "supabase.SupabaseClient": "$supabase",
}
_CALLS = {}
for receiver, kind, methods in (
    ("fs", "file", ("writeFile", "writeFileSync", "appendFile", "appendFileSync", "createWriteStream",
                    "copyFile", "copyFileSync")),
    ("fs.promises", "file", ("writeFile", "appendFile", "copyFile")),
    ("$redis", "cache", ("set", "setEx", "setex", "pSetEx", "psetex", "mSet", "mset", "hSet", "hset",
                         "lPush", "lpush", "rPush", "rpush", "sAdd", "sadd", "zAdd", "zadd", "xAdd", "xadd")),
    ("$s3", "object", ("putObject", "copyObject", "uploadPart")),
    ("$pinecone-index", "vector", ("upsert", "upsertRecords", "update")),
    ("$qdrant", "vector", ("upsert",)),
    ("$langgraph-store", "memory", ("put",)),
    ("$langgraph-checkpointer", "memory", ("put", "putWrites")),
    ("$langgraph-persistent", "memory", ("invoke", "stream", "updateState", "bulkUpdateState")),
    ("$mem0", "memory", ("add", "update")),
    ("$openai.files", "object", ("create",)),
    ("$openai.vectorStores.files", "vector", ("create", "createAndPoll", "upload", "uploadAndPoll")),
    ("$openai.vectorStores.fileBatches", "vector", ("create", "createAndPoll", "uploadAndPoll")),
    ("$openai.conversations", "model-state", ("create",)),
    ("$openai.conversations.items", "model-state", ("create",)),
    ("$openai.responses", "model-state", ("create",)),
    ("$openai.chat.completions", "model-state", ("create",)),
    ("$langfuse", "trace", ("trace", "span", "generation", "event")),
    ("$otel", "trace", ("addSpanProcessor",)),
    ("$drizzle", "database", ("insert", "update")),
    ("$kysely", "database", ("insertInto", "updateTable", "replaceInto", "mergeInto")),
    ("$mastra", "memory", ("saveMessages", "saveThread", "updateWorkingMemory")),
    ("$anthropic.beta.files", "object", ("upload",)),
    ("$anthropic.messages.batches", "model-state", ("create",)),
    ("$anthropic.beta.sessions", "model-state", ("create",)),
    ("$letta.agents.messages", "memory", ("create", "createAsync")),
    ("$letta.agents.passages", "memory", ("create", "update")),
    ("$letta.blocks", "memory", ("create", "update")),
    ("$chroma-collection", "vector", ("add", "upsert", "update")),
    ("$supabase-query", "database", ("insert", "update", "upsert")),
    ("$supabase-bucket", "object", ("upload", "uploadToSignedUrl", "update", "copy", "move")),
    ("$upstash-vector", "vector", ("upsert", "update")),
):
    _CALLS.update({f"{receiver}.{method}": kind for method in methods})
_S3_WRITES = {"s3.PutObjectCommand", "s3.CopyObjectCommand", "s3.UploadPartCommand"}
_SQL_COMMAND = re.compile(
    rb"(?:\s|--[^\r\n]*(?:[\r\n]|$)|/\*[\s\S]*?\*/)*"
    rb"(INSERT|UPDATE|REPLACE|MERGE|COPY|SELECT|SHOW|EXPLAIN|DELETE|CREATE|ALTER|DROP|BEGIN|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)
_RAW_WRITES = {
    "$pg.query", "$pg-pool.query", "$postgres", "$postgres.unsafe", "$sqlite.exec",
    "$drizzle.execute", "$prisma.$executeRaw", "$prisma.$executeRawUnsafe",
    "$prisma.$queryRaw", "$prisma.$queryRawUnsafe",
}
_PRISMA_WRITES = {"create", "createMany", "createManyAndReturn", "update", "updateMany", "updateManyAndReturn", "upsert"}
_SCHEDULERS = {
    "timers.setTimeout": ("timer", 0), "timers.setImmediate": ("timer", 0),
    "timers.setInterval": ("timer", 0), "bullmq.Worker": ("bullmq", 1),
    "timers.queueMicrotask": ("timer", 0), "process.nextTick": ("timer", 0),
}
_GLOBALS = {name: f"timers.{name}" for name in ("setTimeout", "setImmediate", "setInterval", "queueMicrotask")}
_GLOBALS.update({"require": "require", "process": "process", "globalThis": "globalThis", "global": "globalThis"})
_UNBOUND = object()
_MAX_INFERRED_PARAMETERS = 32
_SCHEDULED_ARGUMENTS = {
    "timers.setTimeout": 2, "timers.setInterval": 2, "timers.setImmediate": 1, "process.nextTick": 1,
}


def _module(source):
    if source.startswith("drizzle-orm/"):
        return "drizzle"
    return _MODULES.get(source)


def is_source(path):
    return path.endswith(SUFFIXES) and not path.endswith((".d.ts", ".d.mts", ".d.cts"))


def load_parser():
    try:
        from tree_sitter import Language, Parser
        from tree_sitter_javascript import language as language_javascript
        from tree_sitter_typescript import language_tsx, language_typescript
    except ImportError:
        raise InvalidReport("Install the TypeScript scanner with python -m pip install 'setrya[typescript]'.") from None
    return {".tsx": Parser(Language(language_tsx())), ".ts": Parser(Language(language_typescript())),
            ".js": Parser(Language(language_javascript()))}


def _children(root, path):
    pending = [(root, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        require(count <= _MAX_AST_NODES and depth <= _MAX_AST_DEPTH,
                f"TypeScript syntax tree exceeds the scan budget: {path}.")
        yield node
        pending.extend((child, depth + 1) for child in reversed(node.children))


def _names(pattern):
    if pattern is None:
        return
    if pattern.type in ("identifier", "type_identifier", "shorthand_property_identifier_pattern"):
        yield pattern
    elif pattern.type in ("required_parameter", "optional_parameter"):
        yield from _names(pattern.child_by_field_name("pattern"))
    elif pattern.type == "pair_pattern":
        yield from _names(pattern.child_by_field_name("value"))
    elif pattern.type in ("assignment_pattern", "object_assignment_pattern"):
        yield from _names(pattern.child_by_field_name("left"))
    elif pattern.type in ("formal_parameters", "rest_pattern", "object_pattern", "array_pattern"):
        for child in pattern.named_children:
            yield from _names(child)


@dataclass(frozen=True)
class _Ref:
    name: str
    origin: str = ""
    package: str | None = None

    def member(self, name):
        return _Ref(f"{self.name}.{name}", self.origin, self.package)


@dataclass(frozen=True)
class _Selector:
    value: object
    name: str


@dataclass(frozen=True)
class _Annotation:
    node: object


@dataclass
class _Scope:
    parent: "_Scope | None"
    function: bool = False
    context: tuple = ()
    bindings: dict = field(default_factory=dict)
    type_bindings: dict = field(default_factory=dict)
    writer: str | None = None
    receiver: tuple | None = None
    method: object | None = None

    def bind(self, name, value):
        self.bindings[name] = None if name in self.bindings else value

    def defining_scope(self, name, *, type_name=False):
        scope = self
        while scope is not None:
            bindings = scope.type_bindings if type_name else scope.bindings
            if name in bindings:
                return scope
            scope = scope.parent
        return None

    def resolve(self, name, *, type_name=False):
        owner = self.defining_scope(name, type_name=type_name)
        if owner is None:
            return _UNBOUND
        bindings = owner.type_bindings if type_name else owner.bindings
        return bindings[name]

    def enclosing_function(self):
        scope = self
        while scope is not None and not scope.function:
            scope = scope.parent
        return scope

    def enclosing_writer(self):
        scope = self
        while scope is not None:
            if scope.writer:
                return scope.writer
            if scope.function:
                break
            scope = scope.parent
        return None


class _Scanner:
    def __init__(self, raw, path):
        self.raw = raw
        self.path = path
        self.scopes = {}
        self.module = _Scope(None)
        self.functions = {}
        self.field_values = {}
        self.field_writes = {}
        self.dynamic_fields = set()
        self.methods = {}
        self.assignments = []
        self.rebound = set()
        self.calls = []
        self.references = {}
        self.storage_imports = set()
        self.unresolved_write = False

    def text(self, node):
        return self.raw[node.start_byte:node.end_byte].decode("utf-8") if node is not None else ""

    def literal(self, node):
        if node is not None and node.type == "string" and len(node.named_children) == 1:
            fragment = node.named_children[0]
            if fragment.type == "string_fragment":
                return self.text(fragment)
        return ""

    def _bind(self, scope, pattern, value=None):
        for name in _names(pattern):
            scope.bind(self.text(name), value if name == pattern and name.type == "identifier" else None)

    def _bind_variable(self, scope, pattern, value):
        if pattern is None or pattern.type != "object_pattern" or value is None:
            self._bind(scope, pattern, value)
            return
        for entry in pattern.named_children:
            if entry.type == "shorthand_property_identifier_pattern":
                scope.bind(self.text(entry), _Selector(value, self.text(entry)))
            elif entry.type == "pair_pattern":
                prop = entry.child_by_field_name("key")
                target = entry.child_by_field_name("value")
                if prop.type == "property_identifier" and target.type == "identifier":
                    self._bind(scope, target, _Selector(value, self.text(prop)))
                else:
                    self._bind(scope, target)
            else:
                self._bind(scope, entry)

    def _import_name(self, scope, name, ref, *, type_only=False):
        key = self.text(name)
        scope.type_bindings[key] = ref if key not in scope.type_bindings else None
        if not type_only:
            self._bind(scope, name, ref)
            if ref and ref.package in _STORAGE:
                self.storage_imports.add(ref.package)

    def _imports(self, node, scope):
        whole_type = any(child.type == "type" for child in node.children)
        source = _module(self.literal(node.child_by_field_name("source")))
        if source in _STORAGE and not whole_type and not any(
            item.type in ("import_clause", "import_require_clause") for item in node.named_children
        ):
            self.storage_imports.add(source)
        for item in node.named_children:
            if item.type == "import_require_clause":
                module = _module(self.literal(item.child_by_field_name("source")))
                name = item.named_children[0]
                self._import_name(scope, name, _Ref(module, package=module) if module else None)
            if item.type != "import_clause":
                continue
            ref = _Ref(source, package=source) if source else None
            for imported in item.named_children:
                if imported.type == "identifier":
                    target = _DEFAULTS.get(source)
                    self._import_name(scope, imported, ref.member(target) if target else ref, type_only=whole_type)
                elif imported.type == "namespace_import":
                    self._import_name(scope, imported.named_children[0], ref, type_only=whole_type)
                elif imported.type == "named_imports":
                    for spec in imported.named_children:
                        name = spec.child_by_field_name("name")
                        if name is None:
                            continue
                        alias = spec.child_by_field_name("alias") or name
                        spec_type = whole_type or any(child.type == "type" for child in spec.children)
                        self._import_name(scope, alias, ref.member(self.text(name)) if ref else None, type_only=spec_type)

    @staticmethod
    def _annotation(node):
        marker = node.child_by_field_name("type")
        return _Annotation(marker.named_children[0]) if marker and marker.named_children else None

    @staticmethod
    def _parameters(node):
        if params := node.child_by_field_name("parameters"):
            return [child for child in params.named_children if child.type != "comment"]
        param = node.child_by_field_name("parameter")
        return [param] if param else []

    @staticmethod
    def _parameter_name(param):
        return param.child_by_field_name("pattern") if param.type in ("required_parameter", "optional_parameter") else param

    def _collect_parameters(self, node, scope):
        for param in self._parameters(node):
            name = self._parameter_name(param)
            annotation = self._annotation(param)
            self._bind(scope, name, annotation)
            if (name is not None and name.type == "identifier" and scope.method
                    and self.text(scope.method.child_by_field_name("name")) == "constructor"
                    and any(child.type in ("accessibility_modifier", "readonly") for child in param.children)):
                self.field_values[scope.receiver, self.text(name)] = annotation

    def _new_scope(self, node, scope):
        owner = node.parent if node.parent.type in ("variable_declarator", "class_declaration") else node
        name = self.text(owner.child_by_field_name("name"))
        context = (*scope.context, name) if name and node.type != "statement_block" else scope.context
        receiver, method = scope.receiver, scope.method
        if node.type == "class_body":
            receiver, method = (node, False), None
        elif node.type in ("method_definition", "field_definition", "public_field_definition"):
            static = any(child.type == "static" for child in node.children)
            receiver = (node.parent, static) if node.parent.type == "class_body" else None
            method = node if receiver and node.type == "method_definition" else None
        elif node.type in _FUNCTIONS and node.type != "arrow_function":
            receiver, method = None, None
        return _Scope(scope, function=node.type in _FUNCTIONS, context=context, receiver=receiver, method=method)

    def _record_assignment(self, node, scope):
        left = node.child_by_field_name("left")
        if left is None:
            return
        if left.type == "identifier":
            name = self.text(left)
            owner = scope.defining_scope(name) or self.module
            owner.bind(name, None)
            self.rebound.add((id(owner), name))
        if not scope.receiver or left.type not in ("member_expression", "subscript_expression"):
            return
        obj = left.child_by_field_name("object")
        if obj is None or obj.type != "this":
            return
        prop = left.child_by_field_name("property")
        name = self.text(prop) if prop else self.literal(left.child_by_field_name("index"))
        if not name:
            self.dynamic_fields.add(scope.receiver)
            return
        # An unconditional constructor assignment can supply the client's origin.
        direct_constructor = (scope.method and self.text(scope.method.child_by_field_name("name")) == "constructor"
                              and node.parent.type == "expression_statement"
                              and node.parent.parent == scope.method.child_by_field_name("body"))
        key = scope.receiver, name
        self.field_writes[key] = (node.child_by_field_name("right") if direct_constructor
                                  and node.type == "assignment_expression" and key not in self.field_writes else None)

    def _function_variables(self, owner):
        while owner.parent is not None and owner.type not in _FUNCTIONS:
            owner = owner.parent
        return self.scopes[owner]

    def _collect(self, root):
        for node in _children(root, self.path):
            scope = self.scopes[node.parent] if node.parent is not None else self.module
            if node.type in ("function_declaration", "generator_function_declaration",
                             "class_declaration", "abstract_class_declaration", "enum_declaration",
                             "internal_module"):
                self._bind(scope, node.child_by_field_name("name"), node if node.type in _FUNCTIONS else None)
            if node.type in ("class_declaration", "abstract_class_declaration", "enum_declaration",
                             "interface_declaration", "type_alias_declaration"):
                scope.type_bindings[self.text(node.child_by_field_name("name"))] = None
            if node.type in _SCOPES and node != root:
                scope = self._new_scope(node, scope)
            self.scopes[node] = scope
            if node.type == "type_parameter":
                scope.type_bindings[self.text(node.child_by_field_name("name"))] = None
            if node.type in _FUNCTIONS:
                self.functions[node] = scope
                self._collect_parameters(node, scope)
                if scope.method == node:
                    self.methods[scope.receiver, self.text(node.child_by_field_name("name"))] = node
                if node.type in ("function_expression", "generator_function"):
                    self._bind(scope, node.child_by_field_name("name"))
            elif node.type == "catch_clause":
                self._bind(scope, node.child_by_field_name("parameter"))
            elif node.type == "for_in_statement":
                kind, left = node.child_by_field_name("kind"), node.child_by_field_name("left")
                if kind and kind.type == "var":
                    self._bind(self._function_variables(node.parent), left)
                elif kind:
                    self._bind(scope, left)
                else:
                    self.assignments.append((node, scope))
            elif node.type == "import_statement":
                self._imports(node, scope)
            elif node.type == "variable_declarator":
                declaration = node.parent
                if declaration.type == "variable_declaration":
                    scope = self._function_variables(declaration.parent)
                kind = declaration.child_by_field_name("kind")
                value = node.child_by_field_name("value") if kind is not None and kind.type == "const" else None
                self._bind_variable(scope, node.child_by_field_name("name"), value)
            elif node.type in ("field_definition", "public_field_definition") and scope.receiver:
                name = node.child_by_field_name("name") or node.child_by_field_name("property")
                if name and name.type in ("property_identifier", "private_property_identifier"):
                    key = scope.receiver, self.text(name)
                    value = node.child_by_field_name("value") or self._annotation(node)
                    self.field_values[key] = value if key not in self.field_values else None
            elif node.type in ("assignment_expression", "augmented_assignment_expression"):
                self.assignments.append((node, scope))
            elif node.type in ("call_expression", "new_expression"):
                self.calls.append(node)
        for node, scope in self.assignments:
            self._record_assignment(node, scope)

    def _resolve(self, node, depth=0):
        if node is None or depth >= _MAX_AST_DEPTH:
            return None
        if node not in self.references:
            self.references[node] = self._reference(node, depth)
        return self.references[node]

    def _type_reference(self, node, depth):
        if node.type == "generic_type":
            name = node.child_by_field_name("name")
            scope = self.scopes[node]
            if self.text(name) == "ReturnType" and scope.resolve("ReturnType", type_name=True) is _UNBOUND:
                args = node.child_by_field_name("type_arguments")
                if args and len(args.named_children) == 1 and args.named_children[0].type == "type_query":
                    target = args.named_children[0].named_children
                    return self._resolve(target[0], depth + 1) if len(target) == 1 else None
            return self._type_reference(name, depth + 1)
        if node.type == "type_identifier":
            ref = self.scopes[node].resolve(self.text(node), type_name=True)
            return ref if isinstance(ref, _Ref) else None
        if node.type == "nested_type_identifier":
            prefix = node.child_by_field_name("module")
            name = node.child_by_field_name("name")
            ref = self.scopes[node].resolve(self.text(prefix), type_name=True)
            if isinstance(ref, _Ref) and name is not None:
                return ref.member(self.text(name))
        return None

    def _client_type(self, annotation, depth):
        ref = self._type_reference(annotation.node, depth + 1)
        if ref:
            client = _TYPED_CLIENTS.get(ref.name) or _CLIENTS.get(ref.name)
            if client:
                if ref.package in _STORAGE:
                    self.storage_imports.add(ref.package)
                return _Ref(client, self.digest(["type", ref.name]), ref.package)
        return None

    def _binding(self, binding, depth, before=None):
        if isinstance(binding, _Annotation):
            return self._client_type(binding, depth + 1)
        if isinstance(binding, _Selector):
            ref = self._binding(binding.value, depth + 1, before)
            return ref.member(binding.name) if ref else None
        if isinstance(binding, _Ref) or binding is None:
            return binding
        if binding is not _UNBOUND and (before is None or binding.end_byte <= before):
            return self._resolve(binding, depth + 1)
        return None

    def _member(self, node, depth):
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        name = self.text(prop) if prop else self.literal(node.child_by_field_name("index"))
        if not name:
            return None
        if obj and obj.type == "this":
            scope = self.scopes[node]
            if scope.receiver is None or scope.receiver in self.dynamic_fields:
                return None
            field = scope.receiver, name
            value = self.field_writes.get(field, self.field_values.get(field))
            return self._binding(value, depth + 1)
        base = self._resolve(obj, depth + 1)
        if base and base.name == "globalThis" and name in _GLOBALS:
            return _Ref(_GLOBALS[name])
        return base.member(name) if base else None

    def _client(self, node, depth):
        ref, arguments = self._call(node, depth + 1)
        if ref is None:
            return None
        if ref.name == "require" and len(arguments) == 1:
            module = _module(self.literal(arguments[0]))
            if module in _STORAGE:
                self.storage_imports.add(module)
            return _Ref(module, package=module) if module else None
        client = _CLIENTS.get(ref.name)
        if ref.name == "$sqlite.prepare" and arguments:
            command = self._query_command(arguments[0], depth + 1)
            if command == "write":
                client = "$sqlite-write"
            elif command == "unknown":
                client = "$sqlite-unknown"
        elif ref.name == "$langgraph-builder.compile":
            options = self._properties(arguments[0]) if arguments else None
            saver = self._resolve(options.get("checkpointer"), depth + 1) if options is not None else None
            if saver and saver.name == "$langgraph-checkpointer":
                client = "$langgraph-persistent"
        if client:
            return _Ref(client, self.digest([ref.origin, self._signature(node)]), ref.package)
        return None

    def _reference(self, node, depth):
        if node.type == "identifier":
            name = self.text(node)
            binding = self.scopes[node].resolve(name)
            if binding is _UNBOUND and name in _GLOBALS:
                return _Ref(_GLOBALS[name])
            return self._binding(binding, depth + 1, node.start_byte)
        elif node.type in ("member_expression", "subscript_expression"):
            return self._member(node, depth + 1)
        elif node.type in ("call_expression", "new_expression"):
            return self._client(node, depth + 1)
        elif node.type in ("parenthesized_expression", "non_null_expression", "as_expression",
                           "satisfies_expression", "await_expression", "instantiation_expression"):
            if node.named_children:
                return self._resolve(node.named_children[0], depth + 1)
        return None

    def _call(self, node, depth=0):
        function = node.child_by_field_name("function") or node.child_by_field_name("constructor")
        args = node.child_by_field_name("arguments")
        arguments = []
        if args:
            arguments = [args] if args.type == "template_string" else [
                child for child in args.named_children if child.type != "comment"
            ]
        return self._resolve(function, depth + 1), arguments

    def _properties(self, node):
        if node is None or node.type != "object":
            return None
        properties = {}
        for prop in node.named_children:
            if prop.type == "comment":
                continue
            if prop.type == "shorthand_property_identifier":
                properties[self.text(prop)] = prop
                continue
            key = prop.child_by_field_name("key") if prop.type == "pair" else None
            name = self.text(key) if key is not None and key.type == "property_identifier" else self.literal(key)
            if not name:
                return None
            properties[name] = prop.child_by_field_name("value")
        return properties

    def _sql(self, node, depth=0):
        if node is None or depth >= _MAX_AST_DEPTH or node.end_byte - node.start_byte > 8192:
            return ""
        if node.type == "string":
            return self.literal(node)
        if node.type == "template_string":
            return "".join("?" if child.type == "template_substitution" else self.text(child)
                           for child in node.named_children)
        if node.type == "object":
            props = self._properties(node)
            return self._sql(props.get("text"), depth + 1) if props is not None else ""
        if node.type == "call_expression":
            ref, args = self._call(node, depth + 1)
            if ref and ref.name in ("drizzle.sql", "kysely.sql", "prisma.Prisma.sql") and args:
                return self._sql(args[0], depth + 1)
        if node.type == "identifier":
            binding = self.scopes[node].resolve(self.text(node))
            if hasattr(binding, "end_byte") and binding.end_byte <= node.start_byte:
                return self._sql(binding, depth + 1)
        return ""

    @staticmethod
    def _sql_command(statement):
        sql = statement.encode()
        match = _SQL_COMMAND.match(sql)
        if not match:
            return "unknown"
        command = match[1].upper()
        if command in (b"INSERT", b"UPDATE", b"REPLACE", b"MERGE"):
            return "write"
        if command == b"COPY" or b";" in sql.rstrip().removesuffix(b";"):
            return "unknown"
        if command == b"CREATE" and re.search(rb"\bAS\s+SELECT\b", sql, re.IGNORECASE):
            return "write"
        return "other"

    def _query_command(self, node, depth=0):
        if node is not None and node.type == "ternary_expression" and depth < _MAX_AST_DEPTH:
            branches = {self._query_command(node.child_by_field_name(field), depth + 1)
                        for field in ("consequence", "alternative")}
            if "unknown" in branches:
                return "unknown"
            return "write" if "write" in branches else "other"
        return self._sql_command(self._sql(node, depth))

    def _signature(self, node):
        tokens = []
        pending = [node]
        while pending:
            token = pending.pop()
            if token.type == "comment":
                continue
            if token.child_count:
                pending.extend(reversed(token.children))
            else:
                tokens.append(self.text(token))
        return self.digest(tokens)

    def _operation(self, ref, arguments):
        kind = _CALLS.get(ref.name)
        if ref.name == "$sqlite-write.run":
            return "database", "node:sqlite.StatementSync.run"
        if ref.name == "$sqlite-unknown.run":
            self.unresolved_write = True
        if ref.name in _RAW_WRITES:
            command = self._query_command(arguments[0] if arguments else None)
            if command == "write":
                operation = ref.name.removeprefix("$").replace("pg-pool.", "pg.")
                return "database", operation
            if command == "unknown":
                self.unresolved_write = True
        parts = ref.name.split(".")
        if len(parts) == 3 and parts[0] == "$prisma" and parts[-1] in _PRISMA_WRITES:
            return "database", f"prisma.model.{parts[-1]}"
        if ref.name == "$s3.send" and arguments and arguments[0].type == "new_expression":
            command, _ = self._call(arguments[0])
            if command and command.name in _S3_WRITES:
                return "object", command.name
        if ref.name in ("$openai.responses.create", "$openai.chat.completions.create"):
            props = self._properties(arguments[0]) if arguments else None
            if props is not None and "conversation" not in props and (self.text(props.get("store")) == "false"
                    or (ref.name == "$openai.chat.completions.create" and "store" not in props)):
                return None
        if ref.name in ("ai-sdk.generateText", "ai-sdk.streamText") and arguments:
            props = self._properties(arguments[0])
            if props is not None:
                model = self._resolve(props.get("model"))
                providers = self._properties(props.get("providerOptions")) if "providerOptions" in props else {}
                options = (self._properties(providers["openai"]) if "openai" in providers else {}
                           ) if providers is not None else None
                if model and model.name in ("$ai-responses", "$ai-chat"):
                    stored = self.text(options.get("store")) if options is not None else ""
                    if (options is None or "conversation" in options
                            or (stored != "false" and (model.name == "$ai-responses" or stored))):
                        return "model-state", ref.name
        if ref.name in ("claude-agent.query", "claude-agent.unstable_v2_createSession",
                        "claude-agent.unstable_v2_resumeSession", "claude-agent.unstable_v2_prompt"):
            props = self._properties(arguments[0]) if arguments else None
            options = self._properties(props.get("options")) if props else None
            if options is None or self.text(options.get("persistSession")) != "false":
                return "model-state", ref.name
        return (kind, ref.name.removeprefix("$")) if kind else None

    def _callback(self, node):
        if node is None:
            return None
        if node in self.functions:
            return node
        if node.type == "identifier":
            binding = self.scopes[node].resolve(self.text(node))
            if binding in self.functions and (binding.type in ("function_declaration", "generator_function_declaration")
                                               or binding.end_byte <= node.start_byte):
                return binding
        if node.type == "member_expression":
            scope = self.scopes[node]
            obj = node.child_by_field_name("object")
            if obj is not None and obj.type == "this" and scope.receiver not in self.dynamic_fields:
                key = scope.receiver, self.text(node.child_by_field_name("property"))
                if key not in self.field_writes:
                    return self.methods.get(key)
        return None

    def _connect_functions(self):
        received = defaultdict(list)
        direct = defaultdict(set)
        for node in self.calls:
            ref, args = self._call(node)
            function = node.child_by_field_name("function")
            owner = self.scopes[node].enclosing_function()
            target = self._callback(function)
            if target is not None:
                if owner:
                    direct[id(owner)].add(target)
                received[target].append(self._fixed_arguments(args))
            if ref and ref.name in _SCHEDULERS:
                writer, index = _SCHEDULERS[ref.name]
                if index < len(args) and (callback := self._callback(args[index])) is not None:
                    self.functions[callback].writer = writer
                    if start := _SCHEDULED_ARGUMENTS.get(ref.name):
                        received[callback].append(self._fixed_arguments(args[start:]))
        for target, inputs in received.items():
            scope = self.functions[target]
            for index, parameter in enumerate(self._parameters(target)[:_MAX_INFERRED_PARAMETERS]):
                node = self._parameter_name(parameter)
                if node is None or node.type != "identifier":
                    continue
                name = self.text(node)
                if scope.bindings[name] is not None or (id(scope), name) in self.rebound:
                    continue
                clients = [self._resolve(args[index]) if index < len(args) else None for args in inputs]
                if all(client and client.name.startswith("$") for client in clients):
                    kinds = {(client.name, client.package) for client in clients}
                    if len(kinds) == 1:
                        kind, package = kinds.pop()
                        origins = sorted({client.origin for client in clients})
                        scope.bindings[name] = _Ref(kind, self.digest(["parameter", origins]), package)
        # A scheduler wrapper sometimes delegates the actual write to a same-file function.
        pending = [scope for scope in self.functions.values() if scope.writer]
        while pending:
            scope = pending.pop()
            for target in direct.get(id(scope), ()):
                destination = self.functions[target]
                if destination.writer is None:
                    destination.writer = scope.writer
                    pending.append(destination)
        self.references.clear()

    @staticmethod
    def _fixed_arguments(args):
        end = next((index for index, arg in enumerate(args) if arg.type == "spread_element"), len(args))
        return args[:end]

    def scan(self, root, site_type, digest, candidate_limit, report_unknown):
        self.digest = digest
        self._collect(root)
        self._connect_functions()
        occurrences = Counter()
        found = []
        matched = set()
        for node in self.calls:
            ref, arguments = self._call(node)
            if ref and ref.name == "require":
                self._resolve(node)
            operation = self._operation(ref, arguments) if ref else None
            if operation is None:
                continue
            kind, name = operation
            scope = self.scopes[node]
            stem = digest(["typescript", self.path, scope.context, kind, name, ref.origin, self._signature(node)])
            identity = digest([stem, occurrences[stem]])
            occurrences[stem] += 1
            writer = scope.enclosing_writer()
            found.append(site_type(identity, self.path, node.start_point.row + 1, kind, name, writer))
            matched.add(ref.package)
            require(len(found) <= candidate_limit, "Copy scan exceeded its candidate limit.")
        if report_unknown is not None:
            if self.storage_imports - matched:
                report_unknown(self.path, UNRECOGNIZED_STORAGE)
            if self.unresolved_write:
                report_unknown(self.path, UNRESOLVED_WRITE)
        return found


def inspect(raw, path, parsers, site_type, digest, candidate_limit, *, report_unknown=None):
    suffix = PurePosixPath(path).suffix
    dialect = ".js" if suffix in JAVASCRIPT_SUFFIXES else ".tsx" if suffix == ".tsx" else ".ts"
    language = "JavaScript" if dialect == ".js" else "TypeScript"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidReport(f"{language} source is not UTF-8: {path}.") from None
    deadline = monotonic() + 2
    tree = parsers[dialect].parse(
        lambda offset, _point: raw[offset:offset + 4096], progress_callback=lambda *_: monotonic() > deadline
    )
    require(tree is not None, f"{language} parser exceeded its scan budget: {path}.")
    require(not tree.root_node.has_error, f"Could not parse {language} source: {path}.")
    return _Scanner(raw, path).scan(tree.root_node, site_type, digest, candidate_limit, report_unknown)
