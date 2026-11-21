"""Evaluate the actual answer text locally and carry bounded reasons into replay."""
from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Callable

from setrya.offboarding.inputs import keys_match, require
from .journal import identifier


def _digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def _hash(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class TextAnswer:
    """Identity from the serving response and its actual answer body."""
    caller: str
    conversation: str
    route: str
    question: str
    text: str


@dataclass(frozen=True)
class ModelDecision:
    """Use None for uncertainty. A positive decision needs a quote from the answer."""
    present: bool | None
    reason: str
    match: str | None = None


@dataclass(frozen=True)
class Marker:
    """Case-sensitive, literal substring."""
    value: str
    kind = "marker"

    def evaluate(self, text):
        return _local_decision(self.value if self.value in text else None)


@dataclass(frozen=True)
class Regex:
    """Trusted, application-authored Python expression. Empty matches do not identify a fact."""
    value: str
    kind = "regex"

    def __post_init__(self):
        require(_text(self.value, 1024), "Expected a bounded regex.")
        re.compile(self.value)

    def evaluate(self, text):
        return _local_decision(next((m.group() for m in re.finditer(self.value, text) if m.group()), None))


@dataclass(frozen=True)
class Entity:
    """A literal name, matched case-insensitively at word boundaries."""
    value: str
    kind = "entity"

    def evaluate(self, text):
        found = re.search(r"(?<!\w)" + re.escape(self.value) + r"(?!\w)", text, re.IGNORECASE)
        return _local_decision(found.group() if found else None)


def _local_decision(match):
    return ModelDecision(match is not None, "Matched the configured rule." if match else "No match for the configured rule.", match)


@dataclass(frozen=True)
class ModelJudge:
    """Application-supplied evaluator. The rule ID should identify the model and rubric revision."""
    value: str
    evaluate: Callable[[str], ModelDecision]
    kind = "model"

    def __post_init__(self):
        identifier(self.value, "model and rubric ID")
        require(callable(self.evaluate), "Expected a callable model judge.")


@dataclass(frozen=True)
class AnswerJudges:
    target: Marker | Regex | Entity | ModelJudge
    control: Marker | Regex | Entity | ModelJudge
    capture: str = "digest"

    def spec(self):
        spec = {"capture": self.capture}
        for role in ("target", "control"):
            judge = getattr(self, role)
            require(isinstance(judge, (Marker, Regex, Entity, ModelJudge)) and _text(judge.value, 1024),
                    "Each answer judge needs a nonempty bounded rule.")
            spec[role] = {"kind": judge.kind, "rule_sha256": _digest(judge.value)}
            if isinstance(judge, ModelJudge):
                spec[role]["model_rule"] = judge.value
        check_policy(spec)
        return spec

    def inspect(self, text, observe=None):
        require(isinstance(text, str) and len(text.encode("utf-8")) <= 65536, "Expected a bounded answer body.")
        result = {"answer_sha256": _digest(text)}
        for role in ("target", "control"):
            judge = getattr(self, role)
            def decide():
                return self._decision(judge, text)
            observed = decide() if observe is None else observe(role + "_judge", decide)
            result[role] = observed
        return result

    def _decision(self, judge, text):
        decision = judge.evaluate(text)
        require(isinstance(decision, ModelDecision) and type(decision.present) in (bool, type(None))
                and _text(decision.reason, 1024), "A judge must supply a decision and bounded reason.")
        match = decision.match
        if decision.present is True:
            require(_text(match, 65536) and match in text, "A positive judge decision must quote the answer.")
        else:
            require(match is None, "An absent or uncertain fact cannot have a matching quote.")
        reason = decision.reason
        if self.capture == "digest" and isinstance(judge, ModelJudge):
            reason = "Model decision recorded; rationale withheld."
        return {"present": decision.present, "reason": reason,
                        "reason_sha256": _digest(decision.reason), "match_sha256": _digest(match) if match else None,
                        "excerpt": match[:256] if match and self.capture == "excerpt" else None}


def check_policy(policy):
    require(keys_match(policy, ("target", "control", "capture")) and policy["capture"] in ("excerpt", "digest"),
            "Invalid answer judge policy.")
    for role in ("target", "control"):
        item = policy[role]
        require(isinstance(item, dict) and item.get("kind") in ("marker", "regex", "entity", "model"),
                "Unknown answer judge.")
        model = item["kind"] == "model"
        require(keys_match(item, ("kind", "rule_sha256", "model_rule") if model else ("kind", "rule_sha256"))
                and _hash(item["rule_sha256"]), "Invalid answer judge rule.")
        if model:
            identifier(item["model_rule"], "model and rubric ID")
            require(_digest(item["model_rule"]) == item["rule_sha256"], "Wrong model rule digest.")


def check_observation(row, policy):
    observed = row["judgments"]
    require(keys_match(observed, ("target", "control", "answer_sha256")) and _hash(observed["answer_sha256"]),
            "Missing judged answer digest.")
    for role in ("target", "control"):
        item = observed[role]
        if item is None:
            require(row[role + "_present"] is None, "Missing judge cannot support a fact observation.")
            continue
        require(keys_match(item, ("present", "reason", "reason_sha256", "match_sha256", "excerpt"))
                and item["present"] is row[role + "_present"] and _text(item["reason"], 1024)
                and _hash(item["reason_sha256"]), "Invalid judged fact.")
        present, excerpt = item["present"], item["excerpt"]
        require((_hash(item["match_sha256"]) if present is True else item["match_sha256"] is None)
                and (_text(excerpt, 256) if present is True and policy["capture"] == "excerpt" else excerpt is None),
                "Inconsistent matched answer evidence.")
        if present is True and policy[role]["kind"] == "marker":
            require(item["match_sha256"] == policy[role]["rule_sha256"], "Exact marker differs from its matched text.")
        if policy[role]["kind"] != "model":
            require(type(present) is bool and item["reason"] == _local_decision("match" if present else None).reason,
                    "Invalid deterministic judge decision.")
        if policy[role]["kind"] == "model" and policy["capture"] == "digest":
            require(item["reason"] == "Model decision recorded; rationale withheld.", "Unexpected private model rationale.")
        else:
            require(_digest(item["reason"]) == item["reason_sha256"], "Invalid answer rationale digest.")
        if excerpt and len(excerpt) < 256:
            require(_digest(excerpt) == item["match_sha256"], "Invalid answer quote digest.")
