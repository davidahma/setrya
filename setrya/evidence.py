"""Portable, versioned evidence for the offline checker."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from .drill import DRILL_VERSION, Drill, InputError, ProbeRules, Rules, integer, label, mapping

SCHEMA = 4
CHECKER = 4
_INPUT_SCHEMA = 3
_CHECKERS = {1: 1, 2: 2, 3: 3, SCHEMA: CHECKER}
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
    "receipt": _IDENTITY | {"outcome", "generation", "commit"},
    "probe": _PROBE_FIELDS | {"route", "scope"},
    "adapter_error": {"operation", "error"},
}
_OPERATIONS = {"factory", "seed", "search", "start_old", "checkpoint", "publish", "release", "receipt", "close", "test"}
_TRANSITIONS = {
    "seeded": None, "started": "seeded", "held": "started",
    "published": "held", "released": "published",
}
_STAGES = {"seeded": "baseline", "published": "freshness", "released": "final"}


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

    def save(self, path: str | Path) -> None:
        self.validate()
        target = Path(path)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent, prefix=".setrya-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                try:
                    bundle = {
                        "schema": self.schema, "checker": _CHECKERS[self.schema],
                        "run_id": self.run_id, "rules": asdict(self.rules), "events": self.events,
                    }
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
    def load(cls, path: str | Path) -> "Report":
        try:
            with Path(path).open("rb") as stream:
                data = stream.read(MAX_REPORT_BYTES + 1)
            if len(data) > MAX_REPORT_BYTES:
                raise InputError("report exceeds size limit")
            obj = json.loads(data)
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise InputError("could not read report as JSON") from None
        schema = obj.get("schema") if isinstance(obj, dict) else None
        if type(schema) is not int or schema not in _CHECKERS:
            raise InputError("unsupported report schema")
        keys = {"schema", "checker", "run_id", "rules", "events"}
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
        self.receipt_seen = False
        self.baselines = set()
        self.error = None

    def accept(self, event: dict) -> None:
        validate_observation(event, self.probes, legacy=self.schema == 1)
        kind = event["kind"]
        integer(event["seq"], "event sequence", self.seq + 1, self.seq + 1)
        ns = integer(event["ns"], "event time", self.previous_ns, _MAX_TIME)
        if (self.error and not (kind == "adapter_error" and event["operation"] == "close"
                               and self.error != "close")):
            raise InputError("unexpected evidence after adapter error")

        if kind in _TRANSITIONS:
            if self.stage != _TRANSITIONS[kind]:
                raise InputError("invalid lifecycle event order")
            self.stage = kind
        elif kind == "adapter_error":
            if event["operation"] == "test" and self.schema < 4:
                raise InputError("test errors require report schema 4")
            self.error = event["operation"]
        elif kind == "receipt":
            if self.stage != "released" or self.receipt_seen:
                raise InputError("unexpected terminal write receipt")
            self.receipt_seen = True
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


def validate_observation(event: dict, probes: dict, *, legacy: bool = False) -> None:
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
        _validate_receipt(event)
    if kind == "probe":
        _validate_probe(event, probes, legacy=legacy)
    if kind == "adapter_error":
        if label(event["operation"], "adapter operation") not in _OPERATIONS:
            raise InputError("unknown adapter operation")
        label(event["error"], "adapter error type")


def _validate_receipt(event: dict) -> None:
    if event["outcome"] == "committed":
        label(event["generation"], "index generation")
        integer(event["commit"], "write commit", 0, _MAX_TIME)
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
        integer(view["commit"], "search commit", 0, _MAX_TIME)


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
