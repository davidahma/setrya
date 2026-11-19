"""Portable, versioned evidence for the offline checker."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .drill import DRILL_VERSION, Drill, InputError, ProbeRules, Rules, integer, label, mapping

# Internal ordering for the seven supported layouts. The current wire version is
# namespaced so legacy numeric schema 1 is never mistaken for the frozen v1.
SCHEMA = 7
FORMAT = "setrya.search"
CHECKER = 1
_INPUT_SCHEMA = 3
_CHECKERS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, SCHEMA: CHECKER}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_REPORT_BYTES = 16 * 1024 * 1024
_MAX_TIME = 2**63 - 1
PUBLICATION_IDENTITY = {"run_id", "document", "revision"}
_IDENTITY = PUBLICATION_IDENTITY | {"attempt"}
_PROBE_FIELDS = {"probe_id", "stage", "started_ns", "passages", "origins", "view"}
_EVENT_FIELDS = {
    "seeded": set(), "started": {"attempt"}, "held": _IDENTITY | {"name"},
    "published": {"publication"}, "released": {"attempt"},
    "refreshed": {"run_id", "attempt", "generation", "route", "scope"},
    "receipt": _IDENTITY | {"outcome", "generation", "commit"},
    "probe": _PROBE_FIELDS | {"route", "scope"},
    "adapter_error": {"operation", "error"},
}
_OPERATIONS = {"factory", "seed", "search", "start_old", "checkpoint", "publish", "release", "receipt", "close", "test", "refresh"}
_TRANSITIONS = {
    "seeded": None, "started": "seeded", "held": "started",
    "published": "held", "released": "published",
}
_STAGES = {"seeded": "baseline", "published": "freshness", "released": "final"}


def bundle_manifest(directory: Path) -> str | None:
    """Identify one replayable bundle; never silently choose between two verdicts."""
    manifests = [name for name in ("answer-cache.json", "scenario.json")
                 if os.path.lexists(directory / name)]
    if len(manifests) > 1:
        raise InputError("ambiguous evidence directory: keep each answer or scenario bundle in its own directory")
    return manifests[0] if manifests else None


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def git_revision(value: object) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise InputError("use a full lowercase Git commit ID")
    return value


def bind_inputs(drill: Drill, *, test_id: str | None = None, revision: str | None = None) -> dict:
    def fingerprint(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()

    identity = drill.factory if test_id is None else f"recorded-test:{label(test_id, 'test ID')}"
    if identity is None:
        raise InputError("the drill needs an adapter or a recording test ID")
    if test_id is None and identity.partition(":")[0] in {
        "setrya.examples.support_search.adapter", "setrya.examples.pgvector_search.adapter"
    }:
        # These reviewed built-ins moved namespaces; retain the published input
        # identity so a release upgrade alone cannot make a pair incomparable.
        identity = identity.removeprefix("setrya.")
    inputs = {
        "drill_version": DRILL_VERSION,
        "adapter": fingerprint(identity),
        "questions": {probe.rules.id: fingerprint(probe.question) for probe in drill.probes},
    }
    if revision is not None:
        inputs["revision"] = git_revision(revision)
    return inputs


@dataclass(frozen=True)
class Report:
    run_id: str
    rules: Rules
    events: tuple[dict, ...]
    schema: int = 2
    inputs: dict | None = None

    @property
    def format(self) -> str | None:
        """The public wire-format identifier, absent on historical reports."""
        return FORMAT if self.schema == SCHEMA else None

    @property
    def version(self) -> int:
        """The version in the serialized artifact."""
        return 1 if self.format else self.schema

    def save(self, path: str | Path) -> None:
        self.validate()
        target = Path(path)
        try:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise InputError("report destination must be a regular file")
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent, prefix=".setrya-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                try:
                    bundle = {
                        "schema": self.schema, "checker": _CHECKERS[self.schema],
                        "run_id": self.run_id, "rules": asdict(self.rules), "events": self.events,
                    }
                    if self.schema == SCHEMA:
                        bundle.update(format=self.format, schema=self.version)
                    else:
                        bundle["rules"].pop("visibility", None)
                        for probe in bundle["rules"]["probes"]:
                            probe.pop("absence", None)
                    if self.schema >= _INPUT_SCHEMA:
                        bundle["inputs"] = self.inputs
                    json.dump(bundle, stream, indent=2)
                    stream.write("\n")
                    if stream.tell() > MAX_REPORT_BYTES:
                        raise InputError("report exceeds size limit")
                    stream.close()
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        except OSError:
            raise InputError("could not write report. Check the destination directory.") from None

    @classmethod
    def load(cls, path: str | Path, *, expected_sha256: str | None = None) -> "Report":
        def open_nonblocking(filename, flags):
            return os.open(filename, flags | getattr(os, "O_NONBLOCK", 0))

        try:
            with open(path, "rb", opener=open_nonblocking) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise InputError("report must be a regular file")
                if info.st_size > MAX_REPORT_BYTES:
                    raise InputError("report exceeds size limit")
                data = stream.read(MAX_REPORT_BYTES + 1)
        except OSError:
            raise InputError("could not read report as JSON") from None
        if len(data) > MAX_REPORT_BYTES:
            raise InputError("report exceeds size limit")
        if expected_sha256 is not None and sha256(data).hexdigest() != expected_sha256:
            raise InputError("report digest differs from the schedule manifest")
        try:
            obj = json.loads(data, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise InputError("could not read report as JSON") from None
        schema = obj.get("schema") if isinstance(obj, dict) else None
        current = isinstance(obj, dict) and "format" in obj
        if current:
            if obj["format"] != FORMAT or type(schema) is not int or schema != 1:
                raise InputError("unsupported report schema")
            schema = SCHEMA
        elif schema == SCHEMA:
            raise InputError("unsupported report schema")
        if type(schema) is not int or schema not in _CHECKERS:
            raise InputError("unsupported report schema")
        keys = {"schema", "checker", "run_id", "rules", "events"}
        if current:
            keys.add("format")
        fields = mapping(obj, "report", keys | ({"inputs"} if schema >= _INPUT_SCHEMA else set()))
        if type(fields["checker"]) is not int or fields["checker"] != _CHECKERS[schema]:
            raise InputError("unsupported checker version")
        if not isinstance(fields["events"], list):
            raise InputError("report events must be a list")
        rules = Rules.parse(fields["rules"], legacy=schema == 1)
        report = cls(label(fields["run_id"], "run ID"), rules, tuple(fields["events"]), schema, fields.get("inputs"))
        report.validate()
        return report

    def validate(self) -> None:
        """Validate event types, contents and order for live and replayed reports."""
        if type(self.schema) is not int or self.schema not in _CHECKERS:
            raise InputError("unsupported report schema")
        label(self.run_id, "run ID")
        if not isinstance(self.rules, Rules) or Rules.parse(asdict(self.rules), legacy=self.schema == 1) != self.rules:
            raise InputError("invalid report rules")
        if not isinstance(self.events, (tuple, list)):
            raise InputError("report events must be a sequence")
        if self.schema < _INPUT_SCHEMA and self.inputs is not None:
            raise InputError("report schema does not support input fingerprints")
        probes = {probe.id: probe for probe in self.rules.probes}
        if self.schema >= _INPUT_SCHEMA:
            optional = {"revision"} if self.schema >= 4 else set()
            inputs = mapping(self.inputs, "report inputs", {"drill_version", "adapter", "questions"}, optional)
            if "revision" in inputs:
                git_revision(inputs["revision"])
            integer(inputs["drill_version"], "report drill version", DRILL_VERSION, DRILL_VERSION)
            questions = mapping(inputs["questions"], "question fingerprints", set(probes))
            for digest in (inputs["adapter"], *questions.values()):
                if not isinstance(digest, str) or not _HEX.fullmatch(digest):
                    raise InputError("invalid input fingerprint")
        if self.schema < SCHEMA and (self.rules.visibility != "strict" or any(
                getattr(p, "absence", False) for p in self.rules.probes)):
            raise InputError("this rule requires search format v1")
        sequence = _EventSequence(probes, self.schema)
        for event in self.events:
            sequence.accept(event)


class _EventSequence:
    """Validate live and saved records with the same lifecycle and clock."""

    def __init__(self, probes, schema):
        self.probes = probes
        self.schema = schema
        self.previous_ns = 0
        self.seq = 0
        self.stage = None
        self.receipt = None
        self.refreshes = set()
        self.lifecycle = set()
        self.baselines = set()
        self.error = None

    def accept(self, event: dict) -> None:
        validate_observation(event, self.probes, legacy=self.schema == 1, current=self.schema == SCHEMA)
        kind = event["kind"]
        integer(event["seq"], "event sequence", self.seq + 1, self.seq + 1)
        ns = integer(event["ns"], "event time", self.previous_ns, _MAX_TIME)
        if (self.error and not (kind == "adapter_error" and event["operation"] == "close"
                               and self.error != "close")):
            raise InputError("unexpected evidence after adapter error")

        if kind in _TRANSITIONS:
            if kind in self.lifecycle:
                raise InputError(f"duplicate lifecycle event: {kind}")
            if self.stage != _TRANSITIONS[kind]:
                raise InputError("invalid lifecycle event order")
            self.lifecycle.add(kind)
            self.stage = kind
        elif kind == "adapter_error":
            if event["operation"] == "test" and self.schema < 4:
                raise InputError("test errors require report schema 4")
            self.error = event["operation"]
        elif kind == "receipt":
            if self.receipt is not None:
                raise InputError("duplicate terminal write receipt")
            if self.stage != "released":
                raise InputError("unexpected terminal write receipt")
            self.receipt = event
        elif kind == "refreshed":
            path = (event["route"], event["scope"])
            if (self.receipt is None or self.receipt["outcome"] != "committed" or path in self.refreshes
                    or any(event[k] != self.receipt[k] for k in ("run_id", "attempt", "generation"))
                    or not any((p.route, p.scope) == path for p in self.probes.values())):
                raise InputError("unexpected refresh acknowledgment")
            self.refreshes.add(path)
        elif kind == "probe":
            if event["stage"] != _STAGES.get(self.stage):
                raise InputError("invalid probe evidence")
            integer(event["started_ns"], "probe start time", self.previous_ns, ns)
            if self.stage == "seeded":
                if event["probe_id"] in self.baselines:
                    raise InputError("duplicate baseline probe")
                self.baselines.add(event["probe_id"])
        self.seq += 1
        self.previous_ns = ns


def validate_observation(event: dict, probes: dict, *, legacy: bool = False, current: bool = False) -> None:
    """Validate one payload before an adapter observation enters the report."""
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
        raise InputError("each evidence event needs a kind")
    kind = event["kind"]
    if kind not in _EVENT_FIELDS or (legacy and kind == "adapter_error"):
        raise InputError("unknown evidence event kind")
    fields = _EVENT_FIELDS[kind]
    if legacy and kind in {"published", "probe"}:
        fields = set() if kind == "published" else _PROBE_FIELDS
    mapping(event, "event", {"kind", "seq", "ns"} | fields)
    if kind in {"held", "receipt"}:
        for field in _IDENTITY:
            label(event[field], field)
    if kind in {"started", "released"}:
        label(event["attempt"], "attempt")
    if kind == "held":
        label(event["name"], "checkpoint name")
    if kind == "published" and event.get("publication") is not None:
        publication = mapping(event["publication"], "publication", PUBLICATION_IDENTITY)
        for field in PUBLICATION_IDENTITY:
            label(publication[field], field)
    if kind == "receipt":
        _validate_receipt(event, current=current)
    if kind == "refreshed":
        if not current:
            raise InputError("refresh evidence requires search format v1")
        for key in fields:
            label(event[key], key)
    if kind == "probe":
        _validate_probe(event, probes, legacy=legacy)
    if kind == "adapter_error":
        if label(event["operation"], "adapter operation") not in _OPERATIONS:
            raise InputError("unknown adapter operation")
        label(event["error"], "adapter error type")


def _validate_receipt(event: dict, *, current: bool) -> None:
    if event["outcome"] == "committed":
        label(event["generation"], "index generation")
        if event["commit"] is not None or not current:
            integer(event["commit"], "write commit", 0, 2**64 - 1 if current else _MAX_TIME)
    elif event["outcome"] != "rejected" or event["generation"] is not None or event["commit"] is not None:
        raise InputError("invalid terminal write receipt")


def _validate_probe(event: dict, probes: dict, *, legacy: bool) -> None:
    probe_id = label(event["probe_id"], "probe ID")
    if probe_id not in probes:
        raise InputError("unknown probe ID")
    passages, origins = event["passages"], event["origins"]
    if not isinstance(passages, (list, tuple)) or len(passages) > probes[probe_id].top_k:
        raise InputError("probe evidence exceeds the declared result depth")
    if not isinstance(origins, (list, tuple)) or len(origins) != len(passages):
        raise InputError("probe origins must align with passages")
    for marker in (*passages, *origins):
        if marker is not None:
            label(marker, "passage or write marker")
    if not legacy:
        for field in ("route", "scope"):
            if event[field] is not None:
                label(event[field], field)
    if event["view"] is not None:
        view = mapping(event["view"], "search view", {"generation", "commit", "route", "scope"})
        for field in ("generation", "route", "scope"):
            label(view[field], field)
        integer(view["commit"], "search commit", 0, 2**64 - 1)


def matches_search(event: dict, rules: ProbeRules) -> bool:
    """Require the actual route and caller, independently of write visibility."""
    declared = (rules.route, rules.scope)
    view = event.get("view")
    return (event.get("route"), event.get("scope")) == declared and (
        view is None or (view["route"], view["scope"]) == declared
    )


def establishes_baseline(event: dict, rules: ProbeRules, *, freshness: bool = False) -> bool:
    passages = set(event["passages"])
    new_markers = set(rules.require) - set(rules.before)
    return matches_search(event, rules) and set(rules.before) <= passages and (
        not freshness or (None not in passages and new_markers.isdisjoint(passages))
    )

def observed_change(event: dict, rules: ProbeRules) -> bool:
    """A removal must be observed by an available, identified search on the declared route."""
    passages = event["passages"]
    if rules.absence:
        return None not in passages and set(rules.forbid).isdisjoint(passages)
    return set(rules.require) <= set(passages)
