"""Append-only, local SQLite inventory of observed subject-linked copies."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from setrya.drill import InputError

from .subject import Subject


_ACTIONS = {"retrieved", "stored", "derived", "sent"}
_FIELDS = "store, copy_key, source, action, classification, observed_at, details"


def identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,1023}", value):
        raise InputError(f"{name} must be an opaque identifier of up to 1024 characters")
    return value


def validate_details(values: Mapping | None) -> dict:
    if values is not None and not isinstance(values, Mapping):
        raise InputError("copy details must be a mapping")
    fields = dict(values or {})
    if {"provider", "model", "region", "invocation_logging", "prompt_caching"} & fields.keys():
        if fields.get("evidence_level", "configuration") != "configuration":
            raise InputError("provider settings support configuration evidence only")
        fields["evidence_level"] = "configuration"
    for key, value in fields.items():
        if key in {"invocation_logging", "prompt_caching"}:
            if value is not None and type(value) is not bool:
                raise InputError(f"{key} must be a boolean or unknown (None)")
        elif key in {"model", "provider", "region", "tool", "direction", "stage", "evidence_level", "operation"}:
            if value is not None:
                identifier(value, key)
        elif key == "payload_hmac":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise InputError("invalid payload HMAC")
        elif key in {"sources", "components"}:
            if not isinstance(value, (list, tuple)) or len(value) > 1024:
                raise InputError(f"{key} must be a bounded list of opaque identifiers")
            fields[key] = [identifier(v, "source identifier") for v in value]
        else:
            raise InputError("unsupported copy detail field")
    return fields


@dataclass(frozen=True, slots=True)
class Copy:
    store: str
    key: str
    source: str
    action: str
    classification: str
    observed_at: str
    details: dict


class CopyJournal:
    def __init__(self, path: str | Path, *, export_trace_events: bool = False):
        if type(export_trace_events) is not bool:
            raise InputError("export_trace_events must be a boolean")
        self.export_trace_events = export_trace_events
        self.path = Path(path)
        # SQLite opens new files with the caller's umask. Restrict the new file
        # before SQLite creates its schema, including when two workers race.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS copy_events (
                event INTEGER PRIMARY KEY, subject TEXT NOT NULL, store TEXT NOT NULL,
                copy_key TEXT NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL,
                classification TEXT NOT NULL, observed_at TEXT NOT NULL, details TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS by_subject ON copy_events(subject, store, copy_key)")

    def record(self, subject: Subject, *, store: str, key: str, source: str, action: str,
               details: Mapping | None = None) -> Copy:
        if not isinstance(subject, Subject) or action not in _ACTIONS:
            raise InputError("expected a Subject and a copy action (retrieved, sent, stored, derived)")
        row = Copy(identifier(store, "store"), identifier(key, "copy key"), identifier(source, "source"),
                   action, subject.classification, datetime.now(timezone.utc).isoformat(), validate_details(details))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT INTO copy_events(subject, store, copy_key, source, action, classification,
                           observed_at, details) VALUES(?,?,?,?,?,?,?,?)""",
                       (subject.ref, row.store, row.key, row.source, row.action, row.classification,
                        row.observed_at, json.dumps(row.details, separators=(",", ":"), sort_keys=True)))
        if self.export_trace_events:
            from opentelemetry import trace
            attributes = {**subject.metadata(), "store": row.store, "copy_key": row.key,
                          "source": row.source, "action": row.action, "observed_at": row.observed_at,
                          "details_json": json.dumps(row.details, separators=(",", ":"), sort_keys=True)}
            trace.get_current_span().add_event("setrya.copy.v1", attributes)
        return row

    def _read(self, subject, latest, max_bytes=None):
        if not isinstance(subject, Subject):
            raise InputError("expected a Subject")
        where = ("event IN (SELECT MAX(event) FROM copy_events WHERE subject=? GROUP BY store, copy_key)"
                 if latest else "subject=?")
        order = "store, copy_key" if latest else "event"
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
            raise InputError("expected a positive journal byte budget")
        rows = []
        size = 0
        with closing(sqlite3.connect(self.path)) as db:
            for row in db.execute(f"SELECT {_FIELDS} FROM copy_events WHERE {where} ORDER BY {order}", (subject.ref,)):
                if max_bytes is not None:
                    size += sum(len(field.encode()) for field in row) + 100
                    if size > max_bytes:
                        raise InputError("subject history exceeds the drill journal budget")
                rows.append(Copy(*row[:-1], json.loads(row[-1])))
        return tuple(rows)

    def copies(self, subject: Subject) -> tuple[Copy, ...]:
        """Return one entry for each reported physical location, with its latest observation."""
        return self._read(subject, latest=True)

    def history(self, subject: Subject, *, max_bytes: int | None = None) -> tuple[Copy, ...]:
        """Keep the original sequence when several operations touched the same location."""
        return self._read(subject, latest=False, max_bytes=max_bytes)
