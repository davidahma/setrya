"""Portable, versioned evidence for the offline checker."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

from .drill import InputError, ProbeRules, Rules, integer, label, mapping

SCHEMA = 2
CHECKER = 2
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
_OPERATIONS = {"factory", "seed", "search", "start_old", "checkpoint", "publish", "release", "receipt", "close"}
_TRANSITIONS = {
    "seeded": None, "started": "seeded", "held": "started",
    "published": "held", "released": "published",
}
_STAGES = {"seeded": "baseline", "published": "freshness", "released": "final"}


@dataclass(frozen=True)
class Report:
    run_id: str
    rules: Rules
    events: tuple[dict, ...]
    schema: int = SCHEMA

    def save(self, path: str | Path) -> None:
        self.validate()
        target = Path(path)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent, prefix=".setrya-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                try:
                    json.dump({
                        "schema": self.schema, "checker": 1 if self.schema == 1 else CHECKER,
                        "run_id": self.run_id, "rules": asdict(self.rules), "events": self.events,
                    }, stream, indent=2)
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
        fields = mapping(obj, "report", {"schema", "checker", "run_id", "rules", "events"})
        schema = fields["schema"]
        if type(schema) is not int or schema not in {1, SCHEMA}:
            raise InputError("unsupported report schema")
        if type(fields["checker"]) is not int or fields["checker"] != (1 if schema == 1 else CHECKER):
            raise InputError("unsupported checker version")
        if not isinstance(fields["events"], list):
            raise InputError("report events must be a list")
        rules = Rules.parse(fields["rules"], legacy=schema == 1)
        report = cls(label(fields["run_id"], "run ID"), rules, tuple(fields["events"]), schema)
        report.validate()
        return report

    def validate(self) -> None:
        """Validate event types, contents and order for live and replayed reports."""
        if type(self.schema) is not int or self.schema not in {1, SCHEMA}:
            raise InputError("unsupported report schema")
        probes = {probe.id: probe for probe in self.rules.probes}
        previous_ns = 0
        stage = None
        receipt_seen = False
        baselines = set()
        errors = []
        for index, event in enumerate(self.events, 1):
            validate_observation(event, probes, legacy=self.schema == 1)
            kind = event["kind"]
            integer(event["seq"], "event sequence", index, index)
            ns = integer(event["ns"], "event time", previous_ns, _MAX_TIME)
            if errors and not (kind == "adapter_error" and event["operation"] == "close" and errors[-1] != "close"):
                raise InputError("unexpected evidence after adapter error")
            if kind in _TRANSITIONS:
                if stage != _TRANSITIONS[kind]:
                    raise InputError("invalid lifecycle event order")
                stage = kind
            if kind == "adapter_error":
                errors.append(event["operation"])
            if kind == "receipt":
                if stage != "released" or receipt_seen:
                    raise InputError("unexpected terminal write receipt")
                receipt_seen = True
            if kind == "probe":
                if event["stage"] != _STAGES.get(stage):
                    raise InputError("invalid probe evidence")
                integer(event["started_ns"], "probe start time", previous_ns, ns)
                if stage == "seeded":
                    if event["probe_id"] in baselines:
                        raise InputError("duplicate baseline probe")
                    baselines.add(event["probe_id"])
            previous_ns = ns


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
