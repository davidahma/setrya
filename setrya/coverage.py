"""Review changed retrieval routes and jobs against candidate-commit evidence."""
import argparse
import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sys
import tomllib

from .checker import evaluate
from .drill import Drill, InputError, label, mapping
from .evidence import Report, bind_inputs
from .git_refs import git_bytes, resolve_commit
from .scenarios import KINDS, SCHEDULES, ScenarioEvidence, _config_digest, inspect_scenario
from .status import Status


def _path(value, *, root=False):
    if root and value == ".":
        return value
    if (not isinstance(value, str) or not value or not value.isprintable() or "\\" in value
            or any(part in ("", ".", "..", ".git") for part in value.split("/"))):
        raise InputError("coverage paths must be repository-relative")
    return value


def _blob(repo, sha, path):
    row = git_bytes(repo, "ls-tree", "-z", sha, "--", path)
    if not row:
        return None
    mode, kind, oid = row.partition(b"\t")[0].split()
    if mode not in (b"100644", b"100755") or kind != b"blob":
        raise InputError("coverage can only read regular files tracked by Git")
    key = oid.decode("ascii")
    if int(git_bytes(repo, "cat-file", "-s", key)) > 2 * 1024 * 1024:
        raise InputError("coverage input exceeds the 2 MiB file limit")
    return git_bytes(repo, "cat-file", "blob", key)


def _toml(raw):
    if raw is None:
        raise InputError("coverage map or mapped drill is missing from the commit")
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise InputError("invalid committed coverage map or drill TOML") from None


def _contains(root, path):
    return root == "." or path == root or path.startswith(root + "/")


@dataclass(frozen=True)
class Binding:
    drill: str
    probe: str
    test_id: str | None
    scenario: str | None
    schedule: str | None


@dataclass(frozen=True)
class Component:
    name: str
    owner: str
    root: str
    decorators: tuple[str, ...]
    bindings: dict


def _components(raw):
    config = mapping(_toml(raw), "coverage map", {"version", "component"})
    if type(config["version"]) is not int or config["version"] != 1 or not isinstance(config["component"], list):
        raise InputError("unsupported coverage map")
    result = {}
    for row in config["component"]:
        mapping(row, "component", {"name", "owner", "root"}, {"decorators", "binding"})
        name, root = label(row["name"], "component"), _path(row["root"], root=True)
        owner = row["owner"]
        if not isinstance(owner, str) or not owner or len(owner) > 128 or not owner.isprintable():
            raise InputError("provide a team or owner for each component")
        decorators = row.get("decorators", [])
        bindings = row.get("binding", [])
        if not isinstance(decorators, list) or not isinstance(bindings, list) or name in result:
            raise InputError("invalid or duplicate component")
        for deco in decorators:
            if not isinstance(deco, str) or not all(p.isidentifier() for p in deco.split(".")):
                raise InputError("use dotted Python names for registration decorators")
        entries = {}
        for entry in bindings:
            mapping(entry, "binding", {"file", "drill", "probe"}, {"surface", "test_id", "scenario", "schedule"})
            file, surface = _path(entry["file"]), entry.get("surface", "")
            if (not _contains(root, file) or not isinstance(surface, str) or bool(surface) != bool(decorators)
                    or (surface and not surface.isprintable()) or (file, surface) in entries):
                raise InputError("binding must select a unique file and surface inside its component")
            kind, schedule = entry.get("scenario"), entry.get("schedule")
            if kind is not None or schedule is not None:
                if kind not in KINDS or schedule not in (*SCHEDULES, "all") or "test_id" in entry:
                    raise InputError("a scenario binding needs a valid kind and schedule")
            entries[file, surface] = Binding(_path(entry["drill"]), label(entry["probe"], "probe"),
                                             label(entry["test_id"], "test ID") if "test_id" in entry else None,
                                             kind, schedule)
        result[name] = Component(name, owner, root, tuple(decorators), entries)
    if not result:
        raise InputError("declare at least one coverage component")
    return result


def _declarations(raw, decorators):
    """Return registered call sites and a fingerprint of shared module code."""
    if raw is None:
        return {}, ast.dump(ast.parse(""))
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError, UnicodeError, RecursionError):
        raise InputError("could not inspect a Python file in a declared component") from None
    found = {}

    class Registrations(ast.NodeTransformer):
        shared_position = 0

        def generic_visit(self, node):
            # Record the handler's position among the module's shared code.
            # Installing a router before a handler is registered can exclude it.
            self.shared_position += 1
            return super().generic_visit(node)

        def visit_FunctionDef(self, node):
            registered = False
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                name = ast.unparse(call.func if call else decorator)
                if name not in decorators:
                    continue
                argument = None
                if call:
                    arguments = {kw.arg: kw.value for kw in call.keywords}
                    argument = arguments.get("path")
                    if argument is None and call.args:
                        argument = call.args[0]
                    if argument is None:
                        if None in arguments:
                            raise InputError("dynamic registration needs a file-level coverage component")
                        argument = arguments.get("name")
                if argument is None:
                    route = node.name
                elif isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    route = argument.value
                else:
                    raise InputError("dynamic registration needs a file-level coverage component")
                key = name + " " + route
                if key in found or not key.isprintable():
                    raise InputError("ambiguous registration needs a file-level coverage component")
                found[key] = (self.shared_position, ast.dump(node))
                registered = True
            return None if registered else self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    shared = Registrations().visit(tree)
    return found, ast.dump(shared)


def _surfaces(component, before, after):
    old, old_shared = _declarations(before, component.decorators)
    new, new_shared = _declarations(after, component.decorators)
    # A first-match dispatcher can select a different handler anywhere after
    # a registration is inserted, removed, or moved. An append leaves the
    # existing registrations' precedence intact.
    old_order, new_order = list(old), list(new)
    prefix = 0
    for old_key, new_key in zip(old_order, new_order):
        if old_key != new_key:
            break
        prefix += 1
    changed_order = set(old_order[prefix:]) | set(new_order[prefix:])
    return [(key, key not in new) for key in sorted(old.keys() | new.keys())
            if key in changed_order or old.get(key) != new.get(key) or old_shared != new_shared]


def _match(binding, drill, revision, artifact):
    probe = next((p for p in drill.probes if p.rules.id == binding.probe), None)
    if probe is None:
        raise InputError("mapped probe is missing from its committed drill")
    if binding.scenario is None:
        if not isinstance(artifact, Report):
            return None
        expected = bind_inputs(drill, test_id=binding.test_id, revision=revision)
        if artifact.rules != drill.rules or artifact.inputs != expected:
            return None
        verdict, identity = evaluate(artifact), artifact.run_id
    else:
        if not isinstance(artifact, ScenarioEvidence) or (artifact.kind, artifact.schedule) != (
            binding.scenario, binding.schedule
        ):
            return None
        config = _config_digest(drill, drill.factory, None)
        reports = [report for member in artifact.members.values() for report in member.reports.values()]
        expected_questions = bind_inputs(drill, test_id="coverage")["questions"]
        if (not reports or any(member.comparison_id != config for member in artifact.members.values() if member.reports)
                or any(report.rules != drill.rules or report.inputs.get("revision") != revision
                       or report.inputs["questions"] != expected_questions for report in reports)):
            return None
        verdict, identity = artifact.verdict, f"{artifact.kind}/{artifact.schedule} ({len(reports)} deliveries)"
    detail = f"{probe.rules.id} · {probe.rules.route} · caller {probe.rules.scope} · {identity}"
    if verdict.evidence_tier:
        detail += f" · {verdict.evidence_tier}"
    if verdict.status is not Status.PASS:
        detail += " · " + verdict.detail
    return verdict.status, detail


def coverage(repo, base_ref, candidate_ref, map_path, evidence):
    repo = Path(git_bytes(Path(repo), "rev-parse", "--show-toplevel").decode().rstrip("\n"))
    base, candidate = (resolve_commit(repo, ref) for ref in (base_ref, candidate_ref))
    ancestor = git_bytes(repo, "merge-base", base, candidate).decode("ascii").strip()
    blob = lru_cache(maxsize=64)(lambda sha, path: _blob(repo, sha, path))
    map_path = _path(map_path)
    config = blob(candidate, map_path)
    components = _components(config)
    old_config = blob(ancestor, map_path)
    previous = _components(old_config) if old_config and old_config != config else {}
    drill_at = lru_cache(maxsize=64)(lambda path: Drill.from_mapping(_toml(blob(candidate, path))))
    changed = git_bytes(repo, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                        ancestor, candidate, "--").split(b"\0")[:-1]
    artifacts = [inspect_scenario(p) if p.is_dir() else Report.load(p) for p in evidence]
    rows = []
    statuses = []
    for change, encoded in zip(changed[::2], changed[1::2], strict=True):
        path = encoded.decode("utf-8", "surrogateescape")
        scopes = {}
        for comp in (*previous.values(), *components.values()):
            if not _contains(comp.root, path):
                continue
            if comp.decorators:
                if not path.endswith(".py"):
                    continue
                surfaces = _surfaces(comp, blob(ancestor, path), blob(candidate, path))
            else:
                surfaces = [("", change == b"D")]
            for surface, removed in surfaces:
                # If this PR narrows a component, retain its owner's gap for any
                # changed path that the base map would have sent for review.
                current = comp is components.get(comp.name)
                binding = comp.bindings.get((path, surface)) if current and not removed else None
                scopes[comp.name, surface] = (comp, binding, removed)
        for (_, surface), (comp, binding, removed) in scopes.items():
            matches = []
            if binding:
                drill = drill_at(binding.drill)
                if not any(p.rules.id == binding.probe for p in drill.probes):
                    raise InputError("mapped probe is missing from its committed drill")
                for artifact in artifacts:
                    result = _match(binding, drill, candidate, artifact)
                    if result is not None:
                        matches.append(result)
            priority = (Status.FAIL, Status.ERROR, Status.NOT_TESTED, Status.PASS)
            if matches:
                status, detail = min(matches, key=lambda item: priority.index(item[0]))
            else:
                status = Status.NOT_TESTED
                detail = f"missing candidate evidence for {binding.probe}" if binding else (
                    "removed surface needs owner review" if removed else "no mapped drill"
                )
            statuses.append(status)
            context = f"{comp.name} · owner {comp.owner} · {ascii(path)[1:-1]}" + (f" · {surface}" if surface else "")
            rows.append(f"{status.label if matches else 'GAP'}  {context} · {detail}")
    code = next((s.exit_code for s in (Status.FAIL, Status.ERROR, Status.NOT_TESTED) if s in statuses), 0)
    headline = "Changed-route coverage" if rows else "No changed routes or jobs found in the declared components."
    policy_note = ["The candidate changes the coverage map. Review its owners and bindings."] if config != old_config else []
    text = "\n".join([headline, f"Candidate {candidate} · merge base {ancestor}", *policy_note, *rows])
    # Keep repository-controlled names inside the Markdown code block.
    return code, text.replace("`", "\\u0060")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setrya coverage", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--map", default="setrya-coverage.toml")
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    from .cli import _write_summary
    summary = None
    try:
        if args.summary:
            dest = args.summary.resolve()
            repo = Path(git_bytes(args.repo, "rev-parse", "--show-toplevel").decode().rstrip("\n"))
            if dest.is_relative_to(repo):
                local = dest.relative_to(repo)
                if ".git" in local.parts or git_bytes(repo, "ls-files", "-z", "--", str(local)):
                    raise InputError("coverage summary must use a separate artifact path")
            inputs = [repo / _path(args.map), *args.evidence]
            if any(dest == p.resolve() or (p.is_dir() and dest.is_relative_to(p.resolve())) for p in inputs):
                raise InputError("coverage summary must be outside its input artifacts")
            summary = args.summary
        code, text = coverage(args.repo, args.base, args.candidate, args.map, args.evidence)
    except InputError as error:
        code, text = 2, f"ERROR  {error}"
    except (ValueError, OSError, TypeError, RecursionError):
        code, text = 2, "ERROR  Could not inspect the coverage map, changed source, drill or evidence."
    try:
        if summary:
            _write_summary(summary, "changed-route coverage", text)
    except InputError as error:
        print(f"ERROR  {error}", file=sys.stderr)
        return 2
    print(text, file=sys.stderr if code == 2 else sys.stdout)
    return code
