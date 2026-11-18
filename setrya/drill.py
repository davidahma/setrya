"""Read the versioned drill format and derive its text-free rules."""

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import tomllib

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}\Z")
_FACTORY = re.compile(r"(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*:[A-Za-z_]\w*\Z", re.ASCII)


class InputError(ValueError):
    """A safe, user-facing configuration or report error."""


def label(value: object, field: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise InputError(f"{field} must be a short identifier")
    return value


def integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def mapping(value: object, field: str, required: set, optional: set = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise InputError(f"{field} has missing or unsupported fields")
    return value


def labels(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InputError(f"{field} must be a list of identifiers")
    result = tuple(label(item, field) for item in value)
    if len(set(result)) != len(result):
        raise InputError(f"{field} contains duplicate identifiers")
    return result


@dataclass(frozen=True)
class ProbeRules:
    id: str
    route: str
    scope: str
    top_k: int
    before: tuple[str, ...]
    require: tuple[str, ...]
    forbid: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> "ProbeRules":
        obj = mapping(value, "probe rules", {"id", "route", "scope", "top_k", "require", "forbid"}, {"before"})
        probe = cls(
            label(obj["id"], "probe ID"), label(obj["route"], "route"),
            label(obj["scope"], "caller scope"), integer(obj["top_k"], "top_k", 1, 100),
            labels(obj.get("before", []), "before"), labels(obj["require"], "require"),
            labels(obj["forbid"], "forbid"),
        )
        if not probe.require or set(probe.require) & set(probe.forbid):
            raise InputError("each probe needs required passages disjoint from forbidden passages")
        if any(len(items) > probe.top_k for items in (probe.before, probe.require)):
            raise InputError("top_k must accommodate the required passages")
        return probe


@dataclass(frozen=True)
class Rules:
    id: str
    document: str
    old_revision: str
    new_revision: str
    checkpoint: str
    freshness_probe: str
    freshness_ms: int
    checkpoint_ms: int
    settle_ms: int
    sample_ms: int
    max_gap_ms: int
    probes: tuple[ProbeRules, ...]

    @classmethod
    def parse(cls, value: object, *, legacy: bool = False) -> "Rules":
        names = {"id", "document", "old_revision", "new_revision", "checkpoint", "freshness_probe"}
        times = {"freshness_ms", "checkpoint_ms", "settle_ms", "sample_ms", "max_gap_ms"}
        obj = mapping(value, "drill rules", names | times | {"probes"})
        if not isinstance(obj["probes"], (list, tuple)) or not 2 <= len(obj["probes"]) <= 16:
            raise InputError("declare a freshness probe and at least one separate control")
        probes = tuple(ProbeRules.parse(probe) for probe in obj["probes"])
        rules = cls(**{name: label(obj[name], name) for name in names},
                    **{name: integer(obj[name], name, 1, 120_000) for name in times},
                    probes=probes)
        selected = next((probe for probe in probes if probe.id == rules.freshness_probe), None)
        if len({probe.id for probe in probes}) != len(probes) or selected is None:
            raise InputError("probe IDs must be distinct and include freshness_probe")
        if rules.old_revision == rules.new_revision or not selected.before or not selected.forbid:
            raise InputError("freshness probe needs distinct revisions, before passages and forbidden passages")
        if not legacy and (not set(selected.require) - set(selected.before)
                           or not set(selected.before) & set(selected.forbid)):
            raise InputError("freshness probe needs a revision-specific required marker and a forbidden baseline marker")
        if not rules.sample_ms < rules.max_gap_ms <= min(rules.freshness_ms, rules.settle_ms):
            raise InputError("sample_ms must be less than max_gap_ms, within both observation windows")
        if (rules.freshness_ms + rules.settle_ms * len(probes)) // rules.sample_ms > 10_000:
            raise InputError("sampling configuration exceeds the evidence size limit")
        return rules


@dataclass(frozen=True)
class Probe:
    rules: ProbeRules
    question: str


@dataclass(frozen=True)
class Drill:
    rules: Rules
    probes: tuple[Probe, ...]
    factory: str
    options: dict

    def validate(self) -> None:
        """Reject inconsistent programmatic drills before loading adapter code."""
        if not isinstance(self.rules, Rules) or Rules.parse(asdict(self.rules)) != self.rules:
            raise InputError("invalid drill rules")
        if (not isinstance(self.probes, tuple)
                or not all(isinstance(probe, Probe) for probe in self.probes)
                or tuple(probe.rules for probe in self.probes) != self.rules.probes):
            raise InputError("execution probes must match the report rules, including result depth")
        if any(not isinstance(probe.question, str) or not probe.question.strip() for probe in self.probes):
            raise InputError("each probe needs a question")
        if not isinstance(self.factory, str) or not _FACTORY.fullmatch(self.factory) or not isinstance(self.options, dict):
            raise InputError("adapter needs a Python module:factory and an optional options table")

    @classmethod
    def load(cls, path: str | Path) -> "Drill":
        try:
            with Path(path).open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            raise InputError("could not read drill as TOML") from None
        obj = mapping(document, "drill", {"version", "adapter", "rules", "probe"})
        integer(obj["version"], "drill version", 1, 1)
        adapter = mapping(obj["adapter"], "adapter", {"factory"}, {"options"})
        factory = adapter["factory"]
        options = adapter.get("options", {})
        if not isinstance(factory, str) or not _FACTORY.fullmatch(factory) or not isinstance(options, dict):
            raise InputError("adapter needs a Python module:factory and an optional options table")

        if not isinstance(obj["probe"], list):
            raise InputError("declare probes using [[probe]] tables")
        parsed_probes = []
        questions = []
        for raw in obj["probe"]:
            probe = mapping(raw, "probe", {"id", "route", "scope", "top_k", "question", "require", "forbid"}, {"before"})
            question = probe["question"]
            if not isinstance(question, str) or not question.strip():
                raise InputError("each probe needs a question")
            questions.append(question)
            parsed_probes.append({key: value for key, value in probe.items() if key != "question"})
        if not isinstance(obj["rules"], dict):
            raise InputError("rules must be a TOML table")
        rules = Rules.parse({**obj["rules"], "probes": parsed_probes})
        return cls(rules, tuple(Probe(rule, question) for rule, question in zip(rules.probes, questions)),
                   factory, options)
