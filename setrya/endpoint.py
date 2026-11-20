"""Run a two-user cache revocation drill against an application's own test endpoint."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from dataclasses import asdict, dataclass, fields
from hashlib import sha256
import json
import math
import re

from .checker import Verdict
from .drill import InputError, label, mapping
from .endpoint_types import EndpointApplication, EndpointResponse, WorkerCapture

__all__ = ["EndpointApplication", "EndpointCase", "EndpointResponse", "MissingHook", "WorkerCapture", "check_endpoint"]


def _digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


_HOOKS = {
    "application": "open a disposable application for this run",
    "seed": "seed private and public answers for both test users",
    "answer": "query the authenticated endpoint and return the observed response",
    "start_old": "dispatch the stale worker and return its attempt ID",
    "checkpoint": "observe that attempt held after it read the private answer",
    "captured": "return the answer, question and sources captured by the held attempt",
    "revoke": "revoke the selected user's access and return the publication acknowledgment",
    "release": "release the same held attempt",
    "receipt": "observe whether that attempt committed or rejected its write",
}


class MissingHook(Exception):
    """A starter callback that still needs to be connected to the app."""

    def __init__(self, hook):
        if not isinstance(hook, str) or hook not in _HOOKS:
            raise InputError("unknown endpoint hook")
        self.hook = hook
        super().__init__(hook)


@dataclass(frozen=True)
class _Contract:
    """The portable identifiers and fingerprints that define an endpoint case."""

    route: str
    revoked: str
    retained: str
    private_source: str
    public_source: str
    document: str
    old_revision: str
    new_revision: str
    checkpoint: str
    question_sha256: str
    private_answer_sha256: str
    public_answer_sha256: str

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("_sha256"):
                if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
                    raise InputError("invalid endpoint case fingerprint")
            else:
                label(value, field.name)
        if self.revoked == self.retained:
            raise InputError("the revoked and retained callers must be distinct")
        if self.old_revision == self.new_revision:
            raise InputError("the old and new access revisions must be distinct")
        reserved = {"private-answer", "public-answer", "cache-hit", "cache-miss"}
        if self.private_source == self.public_source or reserved.intersection((self.private_source, self.public_source)):
            raise InputError("use distinct source markers outside the reserved answer and cache markers")
        if self.private_answer_sha256 == self.public_answer_sha256:
            raise InputError("the private and public answers must be distinct")

    @classmethod
    def load(cls, data):
        return cls(**mapping(data, "endpoint case", {f.name for f in fields(cls)}))

    def test_id(self):
        content = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "endpoint-" + _digest(content)


@dataclass(frozen=True)
class EndpointCase:
    """Synthetic question, expected HTTP answers and opaque identities for one drill."""

    route: str
    revoked: str
    retained: str
    question: str
    private_source: str
    public_source: str
    private_answer: str
    public_answer: str
    document: str = "policy"
    old_revision: str = "granted"
    new_revision: str = "revoked"
    checkpoint: str = "after-retrieval"

    def __post_init__(self):
        for field in ("question", "private_answer", "public_answer"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise InputError(f"endpoint {field} needs non-empty synthetic text")
        if self.private_answer in self.public_answer:
            raise InputError("the public answer must exclude the private answer")
        self._contract()

    def _contract(self):
        return _Contract(self.route, self.revoked, self.retained, self.private_source, self.public_source,
                         self.document, self.old_revision, self.new_revision, self.checkpoint,
                         _digest(self.question), _digest(self.private_answer), _digest(self.public_answer))


def check_endpoint(
    case: EndpointCase,
    application: Callable[[str], AbstractContextManager[EndpointApplication]],
    output: str | Path,
    *,
    wait_seconds: float = 30,
) -> tuple[Verdict, str]:
    """Drive one test app and return (verdict, text). Replay the whole output directory.

    application(run_id) must open a context manager for the app and clean it up on
    exit. The app supplies seed, answer, start_old, checkpoint, captured, revoke,
    release and receipt hooks. A pollable hook returns None while still pending.
    """
    from .examples.answer_cache.drill import run_endpoint

    if not isinstance(case, EndpointCase) or not callable(application):
        raise InputError("supply an EndpointCase and an application factory")
    if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 120 or not math.isfinite(wait_seconds):
        raise InputError("wait_seconds must be a finite number from 0 to 120")
    return run_endpoint(case, application, output, wait_seconds)
