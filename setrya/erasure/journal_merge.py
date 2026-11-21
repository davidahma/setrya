"""Collect finalized, subject-scoped observations from independent durable workers."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from setrya.offboarding.inputs import keys_match, publish_new, read_regular_bytes, require, unique_fields
from .journal import Copy, CopyJournal, _ACTIONS, _FIELDS, identifier, validate_details
from .subject import Subject

FORMAT = "setrya.copy-journal.v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 8192


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bind(db, role, run_id, producer=None):
    # One process may restart on the same path. A different run or producer must get a fresh journal.
    db.execute("BEGIN IMMEDIATE")
    db.execute("CREATE TABLE IF NOT EXISTS journal_binding (id INTEGER PRIMARY KEY CHECK(id=1), role, run_id, producer)")
    stored = db.execute("SELECT role, run_id, producer FROM journal_binding").fetchone()
    if stored is None:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='copy_events'").fetchone():
            require(not db.execute("SELECT 1 FROM copy_events LIMIT 1").fetchone(), "Cannot claim an existing journal.")
        db.execute("INSERT INTO journal_binding VALUES(1,?,?,?)", (role, run_id, producer))
    else:
        require(stored == (role, run_id, producer), "Journal belongs to another role, run or producer.")


class WorkerJournal(CopyJournal):
    """One trusted producer per run. Seal only after its writes for this subject are finished."""

    def __init__(self, path, *, run_id, producer, export_trace_events=False):
        self.run_id = identifier(run_id, "run ID")
        self.producer = identifier(producer, "producer")
        super().__init__(path, export_trace_events=export_trace_events)
        with closing(sqlite3.connect(self.path)) as db, db:
            _bind(db, "producer", self.run_id, self.producer)
            db.execute("CREATE TABLE IF NOT EXISTS sealed_subjects (subject TEXT PRIMARY KEY)")
            db.execute("""CREATE TRIGGER IF NOT EXISTS reject_sealed_write BEFORE INSERT ON copy_events
                          WHEN EXISTS(SELECT 1 FROM sealed_subjects WHERE subject=NEW.subject)
                          BEGIN SELECT RAISE(ABORT,'worker has sealed this subject'); END""")

    def _trace_identity(self, sequence):
        return {"setrya_run": self.run_id, "setrya_producer": self.producer, "setrya_sequence": sequence}

    def seal(self, subject, destination):
        """Freeze this subject's rows and export a portable snapshot to a new path."""
        require(isinstance(subject, Subject), "Expected a scoped subject.")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            events = []
            used = 0
            for seq, *fields in db.execute(f"SELECT event, {_FIELDS} FROM copy_events WHERE subject=? ORDER BY event",
                                           (subject.ref,)):
                used += sum(len(value.encode()) for value in fields) + 128
                require(len(events) < MAX_EVENTS and used <= MAX_BYTES, "Worker packet exceeds the export limit.")
                events.append({"sequence": seq, **asdict(Copy(*fields[:-1], json.loads(fields[-1])))})
            packet = {"format": FORMAT, "run_id": self.run_id, "producer": self.producer, "subject_ref": subject.ref,
                      "events": events}
            require(len((json.dumps(packet, indent=2) + "\n").encode()) <= MAX_BYTES, "Worker packet exceeds the export limit.")
            db.execute("INSERT OR IGNORE INTO sealed_subjects VALUES(?)", (subject.ref,))
        return publish_new(packet, Path(destination), max_bytes=MAX_BYTES)


def _decode_event(event, ref):
    require(keys_match(event, {"sequence", "store", "key", "source", "action", "classification", "observed_at", "details"}),
            "Invalid copy event fields.")
    seq = event["sequence"]
    require(type(seq) is int and 0 < seq < 2**63 and event["action"] in _ACTIONS, "Invalid event sequence or action.")
    Subject(ref, event["classification"])
    for name in ("store", "key", "source"):
        identifier(event[name], name)
    require(isinstance(event["observed_at"], str) and datetime.fromisoformat(event["observed_at"]).utcoffset() is not None,
            "Expected an offset-aware event time.")
    require(isinstance(event["details"], dict) and event["details"] == validate_details(event["details"]),
            "Invalid or incomplete copy details.")
    return seq, _encode({key: value for key, value in event.items() if key != "sequence"})


class CollectedJournal:
    """Merge authenticated producer exports locally. A sampled trace can add a copy, never complete a producer."""

    def __init__(self, path, *, run_id):
        self.path = Path(path)
        self.run_id = identifier(run_id, "run ID")
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        with closing(sqlite3.connect(self.path)) as db, db:
            _bind(db, "collector", self.run_id)
            db.execute("""CREATE TABLE IF NOT EXISTS collected_events (
                producer TEXT NOT NULL, sequence INTEGER NOT NULL, ref TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(producer,sequence))""")
            db.execute("CREATE INDEX IF NOT EXISTS by_ref ON collected_events(ref,producer,sequence)")
            db.execute("""CREATE TABLE IF NOT EXISTS collected_seals (
                producer TEXT NOT NULL, ref TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(producer,ref))""")

    @staticmethod
    def _insert(db, producer, seq, ref, record):
        found = db.execute("SELECT ref,record FROM collected_events WHERE producer=? AND sequence=?",
                           (producer, seq)).fetchone()
        if found is None:
            db.execute("INSERT INTO collected_events VALUES(?,?,?,?)", (producer, seq, ref, record))
        else:
            require(found == (ref, record), "Producer reused a stable event identity with different observations.")

    def ingest(self, path, *, producer, expected_sha256=None):
        """Import an immutable packet received from the named producer's trusted artifact channel."""
        identifier(producer, "producer")
        raw = read_regular_bytes(path, "worker packet", MAX_BYTES)
        if expected_sha256 is not None:
            require(sha256(raw).hexdigest() == expected_sha256, "Worker packet differs from its trusted SHA-256.")
        packet = json.loads(raw.decode(), object_pairs_hook=unique_fields)
        require(keys_match(packet, {"format", "run_id", "producer", "subject_ref", "events"})
                and packet["format"] == FORMAT and packet["run_id"] == self.run_id and packet["producer"] == producer,
                "Unexpected worker packet identity.")
        ref = Subject(packet["subject_ref"], "journal").ref
        events = packet["events"]
        require(isinstance(events, list) and len(events) <= MAX_EVENTS, "Invalid or oversized worker packet.")
        digest = sha256(_encode(packet).encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT digest FROM collected_seals WHERE producer=? AND ref=?", (producer, ref)).fetchone()
            if existing:
                require(existing[0] == digest, "The sealed producer supplied conflicting packets.")
                return
            last = 0
            for event in events:
                seq, record = _decode_event(event, ref)
                require(seq > last, "Expected an ordered packet with unique event identities.")
                self._insert(db, producer, seq, ref, record)
                last = seq
            count, = db.execute("SELECT count(*) FROM collected_events WHERE producer=? AND ref=?", (producer, ref)).fetchone()
            require(count == len(events), "A trace event is missing from the durable producer packet.")
            db.execute("INSERT INTO collected_seals VALUES(?,?,?)", (producer, ref, digest))

    def ingest_trace(self, attributes, *, producer):
        """Import one setrya.copy.v1 span event emitted by WorkerJournal; call after authenticating its origin."""
        producer = identifier(producer, "producer")
        require(attributes.get("setrya_run") == self.run_id and attributes.get("setrya_producer") == producer,
                "Unexpected trace producer or run.")
        subject = Subject.from_metadata(attributes)
        details = attributes.get("details_json")
        require(isinstance(details, str) and len(details.encode()) <= MAX_BYTES, "Oversized or missing trace details.")
        seq, record = _decode_event({"sequence": attributes.get("setrya_sequence"), "store": attributes.get("store"),
                                     "key": attributes.get("copy_key"), "source": attributes.get("source"),
                                     "action": attributes.get("action"), "classification": subject.classification,
                                     "observed_at": attributes.get("observed_at"),
                                     "details": json.loads(details, object_pairs_hook=unique_fields)}, subject.ref)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM collected_seals WHERE producer=? AND ref=?", (producer, subject.ref)).fetchone():
                require(db.execute("SELECT 1 FROM collected_events WHERE producer=? AND sequence=?",
                                   (producer, seq)).fetchone(), "Trace event absent from finalized packet.")
            self._insert(db, producer, seq, subject.ref, record)

    def coverage(self, subject, *, run_id, expected_producers):
        require(run_id == self.run_id and isinstance(subject, Subject), "Wrong run or subject for worker coverage.")
        require(isinstance(expected_producers, (tuple, list, set, frozenset)) and expected_producers,
                "Declare the expected durable producers.")
        expected = sorted(identifier(p, "expected producer") for p in expected_producers)
        require(len(set(expected)) == len(expected), "Duplicate expected producer.")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN")
            received = [p for (p,) in db.execute("SELECT producer FROM collected_seals WHERE ref=? ORDER BY producer",
                                                  (subject.ref,))]
            observed = {p for (p,) in db.execute("SELECT DISTINCT producer FROM collected_events WHERE ref=?", (subject.ref,))}
        sealed = set(received)
        return {"run_id": run_id, "expected": expected, "received": received, "missing": sorted(set(expected) - sealed),
                "unsealed": sorted(observed - sealed)}

    def history(self, subject, *, max_bytes=MAX_BYTES):
        require(isinstance(subject, Subject) and type(max_bytes) is int and max_bytes > 0, "Invalid journal read.")
        rows, size = [], 0
        with closing(sqlite3.connect(self.path)) as db:
            for (record,) in db.execute("SELECT record FROM collected_events WHERE ref=? ORDER BY producer,sequence",
                                        (subject.ref,)):
                size += len(record.encode())
                require(size <= max_bytes, "Collected subject history exceeds the drill journal budget.")
                rows.append(Copy(**json.loads(record)))
        return tuple(rows)
