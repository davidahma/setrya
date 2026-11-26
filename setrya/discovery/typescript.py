"""Recognize copy operations on statically resolved TypeScript imports and clients."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from time import monotonic

from setrya.core.inputs import InvalidReport, require

from .source_scan import _MAX_AST_DEPTH, _MAX_AST_NODES

SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
SKIP_DIRECTORIES = {"node_modules", ".next", ".turbo"}
_FUNCTIONS = {
    "function_declaration", "function_expression", "arrow_function", "generator_function",
    "generator_function_declaration", "method_definition",
}
_SCOPES = _FUNCTIONS | {
    "program", "statement_block", "class", "class_body", "catch_clause", "for_statement",
    "for_in_statement", "switch_body",
}
_MODULES = {
    "fs": "fs", "node:fs": "fs", "fs/promises": "fs.promises", "node:fs/promises": "fs.promises",
    "timers": "timers", "node:timers": "timers", "node:sqlite": "node:sqlite",
    "redis": "redis", "ioredis": "ioredis", "@upstash/redis": "upstash",
    "@aws-sdk/client-s3": "s3", "@pinecone-database/pinecone": "pinecone",
    "@qdrant/js-client-rest": "qdrant", "@langchain/langgraph": "langgraph",
    "mem0ai": "mem0", "openai": "openai", "langfuse": "langfuse",
    "@opentelemetry/sdk-trace-node": "otel", "@opentelemetry/sdk-trace-base": "otel",
    "bullmq": "bullmq",
}
_DEFAULTS = {"openai": "OpenAI", "ioredis": "Redis"}
_CLIENTS = {
    "redis.createClient": "$redis", "redis.createCluster": "$redis", "$redis.duplicate": "$redis",
    "ioredis.Redis": "$redis", "ioredis.Cluster": "$redis", "upstash.Redis": "$redis",
    "upstash.Redis.fromEnv": "$redis", "s3.S3Client": "$s3", "s3.S3": "$s3",
    "pinecone.Pinecone": "$pinecone", "$pinecone.index": "$pinecone-index",
    "$pinecone.Index": "$pinecone-index", "$pinecone-index.namespace": "$pinecone-index",
    "qdrant.QdrantClient": "$qdrant", "langgraph.InMemoryStore": "$langgraph-store",
    "mem0.MemoryClient": "$mem0", "openai.OpenAI": "$openai", "openai.AzureOpenAI": "$openai",
    "langfuse.Langfuse": "$langfuse", "otel.NodeTracerProvider": "$otel",
    "otel.BasicTracerProvider": "$otel", "node:sqlite.DatabaseSync": "$sqlite",
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
):
    _CALLS.update({f"{receiver}.{method}": kind for method in methods})
_S3_WRITES = {"s3.PutObjectCommand", "s3.CopyObjectCommand", "s3.UploadPartCommand"}
_SQL_WRITE = re.compile(rb"\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE)\s", re.IGNORECASE)
_SCHEDULERS = {
    "timers.setTimeout": ("timer", 0), "timers.setImmediate": ("timer", 0),
    "timers.setInterval": ("timer", 0), "bullmq.Worker": ("bullmq", 1),
}


def is_source(path):
    return path.endswith(SUFFIXES) and not path.endswith((".d.ts", ".d.mts", ".d.cts"))


def load_parser():
    try:
        from tree_sitter import Language, Parser
        from tree_sitter_typescript import language_tsx, language_typescript
    except ImportError:
        raise InvalidReport("Install the TypeScript scanner with python -m pip install 'setrya[typescript]'.") from None
    return {".tsx": Parser(Language(language_tsx())), ".ts": Parser(Language(language_typescript()))}


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


@dataclass
class _Scope:
    parent: "_Scope | None"
    function: bool = False
    context: tuple = ()
    bindings: dict = field(default_factory=dict)
    writer: str | None = None

    def bind(self, name, value):
        self.bindings[name] = None if name in self.bindings else value

    def resolve(self, name):
        scope = self
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return None

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
        self.calls = []
        self.references = {}

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

    def _imports(self, node, scope):
        for item in node.named_children:
            if item.type == "import_require_clause":
                module = _MODULES.get(self.literal(item.child_by_field_name("source")))
                name = item.named_children[0]
                self._bind(scope, name, _Ref(module) if module else None)
            if item.type != "import_clause":
                continue
            module = _MODULES.get(self.literal(node.child_by_field_name("source")))
            if any(child.type == "type" for child in node.children):
                module = None
            for imported in item.named_children:
                if imported.type == "identifier":
                    target = _DEFAULTS.get(module)
                    self._bind(scope, imported, _Ref(f"{module}.{target}" if target else module) if module else None)
                elif imported.type == "namespace_import":
                    self._bind(scope, imported.named_children[0], _Ref(module) if module else None)
                elif imported.type == "named_imports":
                    for spec in imported.named_children:
                        name = spec.child_by_field_name("name")
                        if name is None:
                            continue
                        alias = spec.child_by_field_name("alias") or name
                        runtime = module and not any(child.type == "type" for child in spec.children)
                        self._bind(scope, alias, _Ref(f"{module}.{self.text(name)}") if runtime else None)

    def _collect(self, root):
        for node in _children(root, self.path):
            scope = self.scopes[node.parent] if node.parent is not None else self.module
            if node.type in ("function_declaration", "generator_function_declaration",
                             "class_declaration", "abstract_class_declaration", "enum_declaration",
                             "internal_module"):
                self._bind(scope, node.child_by_field_name("name"), node if node.type in _FUNCTIONS else None)
            if node.type in _SCOPES and node != root:
                owner = node.parent if node.parent.type in ("variable_declarator", "class_declaration") else node
                name = self.text(owner.child_by_field_name("name"))
                context = (*scope.context, name) if name and node.type != "statement_block" else scope.context
                scope = _Scope(scope, function=node.type in _FUNCTIONS, context=context)
            self.scopes[node] = scope
            if node.type in _FUNCTIONS:
                self.functions[node] = scope
                self._bind(scope, node.child_by_field_name("parameters") or node.child_by_field_name("parameter"))
                if node.type in ("function_expression", "generator_function"):
                    self._bind(scope, node.child_by_field_name("name"))
            elif node.type == "catch_clause":
                self._bind(scope, node.child_by_field_name("parameter"))
            elif node.type == "for_in_statement":
                self._bind(scope, node.child_by_field_name("left"))
            elif node.type == "import_statement":
                self._imports(node, scope)
            elif node.type == "variable_declarator":
                declaration = node.parent
                if declaration.type == "variable_declaration":
                    owner = declaration.parent
                    while owner.type not in _FUNCTIONS and owner != root:
                        owner = owner.parent
                    scope = self.scopes[owner]
                kind = declaration.child_by_field_name("kind")
                value = node.child_by_field_name("value") if kind is not None and kind.type == "const" else None
                self._bind(scope, node.child_by_field_name("name"), value)
            elif node.type in ("call_expression", "new_expression"):
                self.calls.append(node)

    def _resolve(self, node, depth=0):
        if node is None or depth >= _MAX_AST_DEPTH:
            return None
        if node not in self.references:
            self.references[node] = self._reference(node, depth)
        return self.references[node]

    def _reference(self, node, depth):
        if node.type == "identifier":
            binding = self.scopes[node].resolve(self.text(node))
            if isinstance(binding, _Ref) or binding is None:
                return binding
            if binding.end_byte <= node.start_byte:
                return self._resolve(binding, depth + 1)
        elif node.type == "member_expression":
            base = self._resolve(node.child_by_field_name("object"), depth + 1)
            member = node.child_by_field_name("property")
            if base and member is not None and member.type == "property_identifier":
                return _Ref(f"{base.name}.{self.text(member)}", base.origin)
        elif node.type in ("call_expression", "new_expression"):
            ref, arguments = self._call(node, depth + 1)
            if ref:
                client = _CLIENTS.get(ref.name)
                if ref.name == "$sqlite.prepare" and arguments:
                    sql = self.literal(arguments[0]).encode()
                    if len(sql) <= 8192 and _SQL_WRITE.match(sql):
                        client = "$sqlite-write"
                if client:
                    return _Ref(client, self.digest([ref.origin, self._signature(node)]))
        elif node.type in ("parenthesized_expression", "non_null_expression", "as_expression", "satisfies_expression"):
            if node.named_children:
                return self._resolve(node.named_children[0], depth + 1)
        return None

    def _call(self, node, depth=0):
        function = node.child_by_field_name("function") or node.child_by_field_name("constructor")
        args = node.child_by_field_name("arguments")
        return self._resolve(function, depth + 1), [n for n in args.named_children if n.type != "comment"] if args else []

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
        if ref.name == "$s3.send" and arguments and arguments[0].type == "new_expression":
            command, _ = self._call(arguments[0])
            if command and command.name in _S3_WRITES:
                return "object", command.name
        if ref.name in ("$openai.responses.create", "$openai.chat.completions.create"):
            options = arguments[0] if arguments else None
            if options is not None and options.type == "object":
                props = {}
                known = True
                for prop in options.named_children:
                    if prop.type == "comment":
                        continue
                    if prop.type == "shorthand_property_identifier":
                        props[self.text(prop)] = prop
                        continue
                    key = prop.child_by_field_name("key") if prop.type == "pair" else None
                    name = self.text(key) if key is not None and key.type == "property_identifier" else self.literal(key)
                    if not name:
                        known = False
                        break
                    props[name] = prop.child_by_field_name("value")
                if known and "conversation" not in props and (self.text(props.get("store")) == "false"
                        or (ref.name == "$openai.chat.completions.create" and "store" not in props)):
                    return None
        return (kind, ref.name.removeprefix("$")) if kind else None

    def scan(self, root, site_type, digest, candidate_limit):
        self.digest = digest
        self._collect(root)
        resolved = [(node, *self._call(node)) for node in self.calls]
        for _, ref, arguments in resolved:
            if ref and ref.name in _SCHEDULERS:
                writer, index = _SCHEDULERS[ref.name]
                if index < len(arguments):
                    callback = arguments[index]
                    if callback.type == "identifier":
                        callback = self.scopes[callback].resolve(self.text(callback))
                    if callback in self.functions:
                        self.functions[callback].writer = writer
        occurrences = Counter()
        found = []
        for node, ref, arguments in resolved:
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
            require(len(found) <= candidate_limit, "Copy scan exceeded its candidate limit.")
        return found


def inspect(raw, path, parsers, site_type, digest, candidate_limit):
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidReport(f"TypeScript source is not UTF-8: {path}.") from None
    dialect = ".tsx" if PurePosixPath(path).suffix == ".tsx" else ".ts"
    deadline = monotonic() + 2
    tree = parsers[dialect].parse(
        lambda offset, _point: raw[offset:offset + 4096], progress_callback=lambda *_: monotonic() > deadline
    )
    require(tree is not None, f"TypeScript parser exceeded its scan budget: {path}.")
    require(not tree.root_node.has_error, f"Could not parse TypeScript source: {path}.")
    return _Scanner(raw, path).scan(tree.root_node, site_type, digest, candidate_limit)
