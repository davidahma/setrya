"""Find bounded, syntactic candidates for Python PDF writes."""

import ast
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from itertools import islice
from pathlib import Path, PurePosixPath

from setrya.core.inputs import InvalidReport, require

_SKIP_DIRECTORIES = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
_MAX_DEPTH = 32
_MAX_ENTRIES = 16_384
_MAX_FILES = 1024
_MAX_FILE_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_AST_NODES = 75_000
_MAX_AST_DEPTH = 100
MAX_SOURCE_CANDIDATES = 512
MAX_SOURCE_CANDIDATE_BYTES = 384 * 1024
_MAX_PATH = 1024
_MAX_ANCHORS = 64
_PATHLIB = object()
_PATH = object()
_PDF_PRODUCERS = {"generate_pdf", "make_pdf", "render_pdf"}
_WRITABLE_MODE = re.compile(r"(?:[wax][bt]?\+?|[wax]\+[bt]|r\+[bt]?|r[bt]\+)\Z")
_NONLINEAR = (ast.AsyncFor, ast.AsyncWith, ast.For, ast.If, ast.Match, ast.Try, ast.TryStar, ast.While, ast.With)
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class PdfWriteCandidate:
    id: str
    path: str
    line: int
    operation: str
    destination: str | None
    hint: str | None


@dataclass(frozen=True)
class _PathValue:
    text: str | None = None
    relative: str | None = None
    basename: str | None = None
    pdf: bool = False
    concrete_path: bool = False
    anchors: tuple[str, ...] = ()


def _literal(text):
    if len(text) > _MAX_PATH or (text and not text.isprintable()):
        return _PathValue()
    filename = text.replace("\\", "/").rsplit("/", 1)[-1]
    safe = "\\" not in text and ":" not in text and not text.startswith("/")
    path = PurePosixPath(text)
    relative = path.as_posix() if safe and ".." not in path.parts else None
    return _PathValue(text=text, relative=relative, basename=filename or None,
                      pdf=filename.lower().endswith(".pdf"), anchors=(text,))


def _join(left, right):
    if left.text is not None and right.text is not None:
        return replace(_literal((PurePosixPath(left.text) / right.text).as_posix()), concrete_path=True)
    return replace(right, text=None, relative=None, concrete_path=True, anchors=_combine_anchors(left, right))


def _combine_anchors(left, right):
    require(len(left.anchors) + len(right.anchors) <= _MAX_ANCHORS, "Source path exceeds its expression budget.")
    return left.anchors + right.anchors


def _expression(node):
    # Python 3.13 started omitting empty fields by default. Preserve IDs made on Python 3.11 and 3.12.
    return ast.dump(node, show_empty=True) if sys.version_info >= (3, 13) else ast.dump(node)


def _argument(node, index, name):
    if index < len(node.args):
        return node.args[index]
    return next((kw.value for kw in node.keywords if kw.arg == name), None)


def _pdf_payload(node):
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    return ((isinstance(function, ast.Name) and function.id in _PDF_PRODUCERS)
            or (isinstance(function, ast.Attribute) and function.attr in _PDF_PRODUCERS))


def _definition_header(node):
    for field, value in ast.iter_fields(node):
        if field == "body":
            continue
        if isinstance(value, ast.AST):
            yield value
        elif isinstance(value, list):
            yield from value


def _writes(body, arguments=None):
    """Count bindings in one scope without mixing in nested function or class bodies."""
    counts = Counter()
    if arguments is not None:
        counts.update(child.arg for child in ast.iter_child_nodes(arguments) if isinstance(child, ast.arg))
    pending = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, _DEFINITIONS):
            counts[node.name] += 1
            pending.extend(_definition_header(node))
            continue
        if isinstance(node, ast.Lambda):
            pending.append(node.args)
            continue
        pending.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            counts[node.id] += 1
        elif isinstance(node, (ast.ExceptHandler, ast.MatchStar, ast.MatchAs)):
            counts[node.name] += 1
        elif isinstance(node, ast.MatchMapping):
            counts[node.rest] += 1
        elif isinstance(node, ast.Import):
            counts.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            counts.update(alias.asname or alias.name for alias in node.names)
    return counts


class _Scanner(ast.NodeVisitor):
    def __init__(self, path, tree):
        self.path = path
        self.tree = tree
        self.names = _writes(tree.body)
        self.external = {name for node in ast.walk(tree) if isinstance(node, (ast.Global, ast.Nonlocal))
                         for name in node.names}
        self.star_import = self.names["*"] > 0
        self.builtin_open = not (self.names["open"] or "open" in self.external or self.star_import)
        self.class_parent = None
        self.values = {}
        self.scope = ()
        self.linear = True
        self.occurrences = Counter()
        self.found = []

    def _symbol(self, node):
        if isinstance(node, ast.Name):
            return self.values.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "Path" and self._symbol(node.value) is _PATHLIB:
            return _PATH
        return None

    def _value(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _literal(node.value)
        if isinstance(node, ast.Name) and isinstance(value := self.values.get(node.id), _PathValue):
            return value
        if isinstance(node, ast.Call) and self._symbol(node.func) is _PATH:
            if node.keywords:
                return _PathValue(concrete_path=True)
            path = replace(_literal("."), concrete_path=True)
            for part in node.args:
                path = _join(path, self._value(part))
            return path
        if isinstance(node, ast.BinOp):
            left, right = self._value(node.left), self._value(node.right)
            if isinstance(node.op, ast.Div) and left.concrete_path:
                return _join(left, right)
            if isinstance(node.op, ast.Add) and not (left.concrete_path or right.concrete_path):
                if left.text is not None and right.text is not None:
                    return _literal(left.text + right.text)
                return _PathValue(pdf=right.pdf, anchors=_combine_anchors(left, right))
        if (isinstance(node, ast.JoinedStr) and node.values and isinstance(last := node.values[-1], ast.Constant)
                and isinstance(last.value, str) and last.value.lower().endswith(".pdf")):
            return _PathValue(pdf=True, anchors=(last.value,))
        return _PathValue()

    def _bind(self, name, value):
        if self.linear and self.names[name] == 1 and name not in self.external and not self.star_import:
            if isinstance(value, ast.AST):
                inferred = self._value(value)
                if inferred.text is None:
                    provenance = json.dumps([_expression(value), inferred.anchors], separators=(",", ":"))
                    fingerprint = hashlib.sha256(provenance.encode()).hexdigest()
                    inferred = replace(inferred, anchors=(fingerprint,))
                value = inferred
            self.values[name] = value

    def visit_Import(self, node):
        for item in node.names:
            if item.name == "pathlib":
                self._bind(item.asname or "pathlib", _PATHLIB)

    def visit_ImportFrom(self, node):
        if node.level == 0 and node.module == "pathlib":
            for item in node.names:
                if item.name == "Path":
                    self._bind(item.asname or "Path", _PATH)

    def visit_Assign(self, node):
        self.generic_visit(node)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._bind(node.targets[0].id, node.value)

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind(node.target.id, node.value)

    def _scope(self, body, name, *, arguments=None, class_scope=False):
        previous = self.names, self.values, self.scope, self.linear, self.builtin_open, self.class_parent
        enclosing, open_available = self.class_parent or (self.values, self.builtin_open)
        self.names = _writes(body, arguments)
        self.values = {variable: value for variable, value in enclosing.items() if variable not in self.names}
        self.scope = (*self.scope, name)
        self.linear = not class_scope
        self.builtin_open = open_available and not self.names["open"]
        self.class_parent = (enclosing, open_available) if class_scope else None
        for statement in body:
            self.visit(statement)
        self.names, self.values, self.scope, self.linear, self.builtin_open, self.class_parent = previous

    def visit_FunctionDef(self, node):
        for expression in _definition_header(node):
            self.visit(expression)
        self._scope(node.body, node.name, arguments=node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for expression in _definition_header(node):
            self.visit(expression)
        self._scope(node.body, node.name, class_scope=True)

    def visit_Lambda(self, node):
        self.visit(node.args)
        self._scope((node.body,), "<lambda>", arguments=node.args)

    def generic_visit(self, node):
        previous = self.linear
        if isinstance(node, _NONLINEAR):
            self.linear = False
        super().generic_visit(node)
        self.linear = previous

    def _destination(self, node):
        function = node.func
        if isinstance(function, ast.Attribute) and self._value(function.value).concrete_path:
            if function.attr in {"write_bytes", "write_text"}:
                return "path." + function.attr, function.value, _pdf_payload(_argument(node, 0, "data"))
            if function.attr in {"rename", "replace"}:
                return "path." + function.attr, _argument(node, 0, "target"), False
            if function.attr == "open":
                mode = self._value(_argument(node, 0, "mode")).text or ""
                if _WRITABLE_MODE.fullmatch(mode):
                    return "path.open", function.value, False
        if isinstance(function, ast.Name) and function.id == "open" and self.builtin_open:
            mode = self._value(_argument(node, 1, "mode")).text or ""
            if _WRITABLE_MODE.fullmatch(mode):
                return "open", _argument(node, 0, "file"), False
        return None

    def visit_Call(self, node):
        site = self._destination(node)
        if site is not None:
            operation, target, pdf_payload = site
            value = self._value(target)
            if target is not None and (value.pdf or pdf_payload):
                hint = value.basename if value.pdf else None
                stem = [self.path, self.scope, operation, _expression(target), value.relative, hint, value.anchors]
                site_key = json.dumps(stem, ensure_ascii=True, separators=(",", ":"))
                occurrence = self.occurrences[site_key]
                self.occurrences[site_key] += 1
                identity = hashlib.sha256(f"{site_key}:{occurrence}".encode()).hexdigest()
                self.found.append(PdfWriteCandidate(identity, self.path, node.lineno, operation, value.relative, hint))
                require(len(self.found) <= MAX_SOURCE_CANDIDATES, "Source scan exceeded its candidate limit.")
        self.generic_visit(node)

    def scan(self):
        self.visit(self.tree)
        return self.found


def _parse_tree(raw, path):
    try:
        tree = ast.parse(raw, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        raise InvalidReport(f"Could not parse Python source: {path}.") from None
    count = 0
    pending = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        count += 1
        require(count <= _MAX_AST_NODES and depth <= _MAX_AST_DEPTH, f"Python AST exceeds the scan budget: {path}.")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree


def _parse(raw, path):
    return _Scanner(path, _parse_tree(raw, path)).scan()


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _open_at(name, mode, parent=None):
    fd = os.open(name, mode | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _scan_sources(root, inspect, *, is_source, skip_directories, source_label, on_skip=None):
    found = []
    entries = files = size = 0
    report_bytes = 0

    def walk(directory, parts):
        nonlocal entries, files, size, report_bytes
        require(len(parts) <= _MAX_DEPTH, "Source scan exceeded its directory depth limit.")
        before = _fingerprint(os.fstat(directory))
        with os.scandir(directory) as walk_entries:
            children = sorted(islice(walk_entries, _MAX_ENTRIES - entries + 1), key=lambda child: child.name)
        entries += len(children)
        require(entries <= _MAX_ENTRIES, "Source scan exceeded its directory entry limit.")
        for child in children:
            if child.name in skip_directories:
                continue
            path = (*parts, child.name)
            display = "/".join(path)
            info = os.stat(child.name, dir_fd=directory, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), "Source scan refuses symbolic links in scope.")
            if stat.S_ISDIR(info.st_mode):
                with _open_at(child.name, os.O_RDONLY | os.O_DIRECTORY, directory) as fd:
                    require(_fingerprint(info) == _fingerprint(os.fstat(fd)), "Source directory changed during scan.")
                    walk(fd, path)
            elif is_source(child.name):
                require(display.isprintable() and len(display) <= _MAX_PATH and "\\" not in display,
                        "Source path exceeds the scan budget or contains unsupported characters.")
                require(stat.S_ISREG(info.st_mode), f"Source scan requires regular {source_label} files.")
                files += 1
                size += info.st_size
                require(files <= _MAX_FILES and size <= _MAX_TOTAL_BYTES and info.st_size <= _MAX_FILE_BYTES,
                        "Source scan exceeded its file budget.")
                with _open_at(child.name, os.O_RDONLY, directory) as fd:
                    require(_fingerprint(info) == _fingerprint(os.fstat(fd)), "Source file changed during scan.")
                    with open(fd, "rb", closefd=False) as stream:
                        raw = stream.read(info.st_size + 1)
                    require(len(raw) == info.st_size and _fingerprint(info) == _fingerprint(os.fstat(fd)),
                            "Source file changed during scan.")
                sites = inspect(raw, display)
                found.extend(sites)
                report_bytes += sum(len(json.dumps(asdict(site))) for site in sites)
                require(len(found) <= MAX_SOURCE_CANDIDATES, "Source scan exceeded its candidate limit.")
                require(report_bytes <= MAX_SOURCE_CANDIDATE_BYTES, "Source scan exceeded its candidate byte limit.")
            elif on_skip is not None and stat.S_ISREG(info.st_mode):
                on_skip(display)
        require(before == _fingerprint(os.fstat(directory)), "Source directory changed during scan.")

    require(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"), "Source scan needs no-follow directory reads.")
    try:
        with _open_at(Path(root), os.O_RDONLY | os.O_DIRECTORY) as fd:
            walk(fd, ())
    except OSError:
        raise InvalidReport(f"Could not read the declared {source_label} source scope.") from None
    return tuple(sorted(found, key=lambda item: (item.path, item.line, item.id)))


def _scan_python(root, inspect):
    return _scan_sources(root, inspect, is_source=lambda path: path.endswith(".py"),
                         skip_directories=_SKIP_DIRECTORIES, source_label="Python")


def scan_pdf_writes(root: str | Path) -> tuple[PdfWriteCandidate, ...]:
    """Inspect regular Python sources beneath a directory without executing application code."""
    return _scan_python(root, _parse)
