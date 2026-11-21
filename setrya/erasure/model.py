"""Application observations for a scoped subject erasure drill."""
from dataclasses import dataclass
from hashlib import sha256

from setrya.offboarding.inputs import require
from .journal import identifier


def route_id(value):
    if isinstance(value, str) and value.startswith("/"):
        require(len(value) <= 1024 and not value.startswith("//"), "Expected a local endpoint route.")
        if value != "/":
            identifier(value[1:], "route")
        return value
    return identifier(value, "route")


def check_callers(callers):
    require(isinstance(callers, list) and callers, "Declare at least one authenticated control caller.")
    for caller in callers:
        identifier(caller, "caller")
    require(len(callers) == len(set(callers)), "Expected distinct authenticated callers.")


def question_digest(question: str) -> str:
    require(isinstance(question, str) and 0 < len(question) <= 8192, "Expected a bounded test question.")
    return sha256(question.encode()).hexdigest()


@dataclass(frozen=True)
class ErasureCase:
    route: str
    question: str
    callers: tuple[str, str]

    def spec(self):
        require(isinstance(self.callers, (tuple, list)) and len(self.callers) == 2
                and self.callers[0] != self.callers[1], "Supply two distinct test callers.")
        return {"route": route_id(self.route), "callers": [identifier(c, "caller") for c in self.callers],
                "question_sha256": question_digest(self.question)}


@dataclass(frozen=True)
class Hold:
    attempt: str


@dataclass(frozen=True)
class Drain:
    attempt: str
    drained: bool


@dataclass(frozen=True)
class Read:
    caller: str
    route: str
    question: str
    subject_present: bool | None
    control_present: bool | None


@dataclass(frozen=True)
class Disposition:
    """An external owner's claim. evidence is an opaque exhibit ID; until is ISO YYYY-MM-DD."""
    store: str
    key: str
    owner: str
    evidence: str
    subject_ref: str
    until: str | None = None
