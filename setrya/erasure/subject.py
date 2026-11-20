"""Tenant-scoped pseudonyms and explicit propagation across trusted app boundaries."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from setrya.drill import InputError


_CURRENT: ContextVar[Subject | None] = ContextVar("setrya_subject", default=None)
_REF = "setrya_subject"
_LABEL = "setrya_classification"


def _text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not value.isprintable():
        raise InputError(f"{name} must be nonempty printable text of at most {limit} characters")
    return value


def keyed_digest(secret: bytes, domain: bytes, data: bytes) -> str:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise InputError("use a private key of at least 32 bytes")
    return hmac.digest(secret, domain + b"\x00" + data, "sha256").hex()


@dataclass(frozen=True, slots=True)
class Subject:
    ref: str
    classification: str

    def __post_init__(self):
        if not isinstance(self.ref, str) or not re.fullmatch("[0-9a-f]{64}", self.ref):
            raise InputError("expected a tenant-bound HMAC subject reference")
        _text(self.classification, "classification", 128)

    @classmethod
    def derive(cls, secret: bytes, tenant: str, subject_id: str, classification: str) -> Subject:
        identity = [_text(tenant, "tenant", 1024), _text(subject_id, "subject ID", 4096)]
        digest = keyed_digest(secret, b"setrya.subject.v1", json.dumps(identity, separators=(",", ":")).encode())
        return cls(digest, classification)

    def metadata(self) -> dict[str, str]:
        """Fields for passage metadata, trusted Celery headers and LangGraph metadata."""
        return {_REF: self.ref, _LABEL: self.classification}

    @classmethod
    def from_metadata(cls, values: Mapping) -> Subject:
        if not isinstance(values, Mapping) or not {_REF, _LABEL} <= values.keys():
            raise InputError("missing Setrya subject metadata")
        return cls(values[_REF], values[_LABEL])

    def headers(self, existing: Mapping | None = None) -> dict:
        values = dict(existing or {})
        if any(k in values and values[k] != v for k, v in self.metadata().items()):
            raise InputError("conflicting subject metadata")
        return {**values, **self.metadata()}

    def langgraph_config(self, thread: str, config: Mapping | None = None) -> dict:
        """Make a subject-exclusive thread and preserve other runnable options."""
        from .adapters import checkpoint_thread
        result = dict(config or {})
        values = dict(result.get("configurable", {}))
        thread_id = checkpoint_thread(self, thread)
        if "thread_id" in values and values["thread_id"] != thread_id:
            raise InputError("LangGraph config already selects a different thread")
        result["configurable"] = {**values, "thread_id": thread_id}
        result["metadata"] = self.headers(result.get("metadata"))
        return result

    def otel_context(self, context=None):
        """Return an OpenTelemetry context with this subject in baggage; attach explicitly."""
        from opentelemetry import baggage
        values = self.headers(baggage.get_all(context=context))
        for name in (_REF, _LABEL):
            context = baggage.set_baggage(name, values[name], context=context)
        return context

    @classmethod
    def from_baggage(cls, context=None) -> Subject:
        from opentelemetry import baggage
        return cls.from_metadata(baggage.get_all(context=context))

    @contextmanager
    def activate(self):
        """Bind this subject to the current request or task and restore the previous one."""
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    @classmethod
    def current(cls) -> Subject:
        current = _CURRENT.get()
        if current is None:
            raise InputError("no Setrya subject bound to this request")
        return current
