"""Immutable, tenant-scoped source revisions and the artifacts built from them."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
import sqlite3

from setrya.drill import InputError
from .journal import CopyJournal, identifier
from .subject import Subject


@dataclass(frozen=True, order=True)
class VersionRef:
    store: str
    key: str
    revision: str

    def __post_init__(self):
        for name in ("store", "key", "revision"):
            identifier(getattr(self, name), name)


class VersionLineage:
    """Share the copy journal's SQLite file. A version's input set is immutable."""

    def __init__(self, journal: CopyJournal, namespace: str):
        self.path = journal.path
        self.namespace = identifier(namespace, "lineage namespace")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS lineage_versions (
                    id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, store TEXT NOT NULL,
                    key TEXT NOT NULL, revision TEXT NOT NULL,
                    UNIQUE(namespace,store,key,revision)
                );
                CREATE TABLE IF NOT EXISTS lineage_subjects (
                    version INTEGER NOT NULL REFERENCES lineage_versions, subject TEXT NOT NULL,
                    PRIMARY KEY(version,subject)
                );
                CREATE INDEX IF NOT EXISTS lineage_subject ON lineage_subjects(subject,version);
                CREATE TABLE IF NOT EXISTS lineage_inputs (
                    artifact INTEGER NOT NULL REFERENCES lineage_versions,
                    input INTEGER NOT NULL REFERENCES lineage_versions, PRIMARY KEY(artifact,input)
                );
                CREATE INDEX IF NOT EXISTS lineage_downstream ON lineage_inputs(input,artifact);
            """)

    def record(self, version: VersionRef, *, subjects=(), inputs=()):
        """Register an original or derived revision. Register its inputs first."""
        if not isinstance(version, VersionRef):
            raise InputError("expected a VersionRef")
        inputs, subjects = tuple(inputs), tuple(subjects)
        if (not inputs and not subjects) or len(inputs) > 256 or len(subjects) > 256:
            raise InputError("register a bounded set of source subjects or input versions")
        if any(not isinstance(v, VersionRef) for v in inputs) or any(not isinstance(s, Subject) for s in subjects):
            raise InputError("expected VersionRef inputs and Subject owners")
        owners = {s.ref for s in subjects}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")

            def locate(ref):
                return db.execute("SELECT id FROM lineage_versions WHERE namespace=? AND store=? AND key=? AND revision=?",
                                  (self.namespace, ref.store, ref.key, ref.revision)).fetchone()

            parent_ids = set()
            for ref in inputs:
                parent = locate(ref)
                if parent is None or ref == version:
                    raise InputError("record each distinct input version before the derived version")
                parent_ids.add(parent[0])
            previous = locate(version)
            if previous:
                version_id = previous[0]
                old_parents = {i for i, in db.execute("SELECT input FROM lineage_inputs WHERE artifact=?", (version_id,))}
                old_subjects = {s for s, in db.execute("SELECT subject FROM lineage_subjects WHERE version=?", (version_id,))}
                if old_parents != parent_ids or old_subjects != owners:
                    raise InputError("a recorded revision cannot change subjects or inputs")
                return
            cursor = db.execute("INSERT INTO lineage_versions(namespace,store,key,revision) VALUES(?,?,?,?)",
                                (self.namespace, version.store, version.key, version.revision))
            version_id = cursor.lastrowid
            db.executemany("INSERT INTO lineage_inputs VALUES(?,?)", [(version_id, i) for i in parent_ids])
            db.executemany("INSERT INTO lineage_subjects VALUES(?,?)", [(version_id, s) for s in owners])

    def affected(self, subject: Subject) -> dict:
        """Return observed roots and their downstream revisions, with retained inputs on shared artifacts."""
        if not isinstance(subject, Subject):
            raise InputError("expected a Subject")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN")
            roots = db.execute("""SELECT v.store,v.key,v.revision FROM lineage_versions v
                JOIN lineage_subjects s ON v.id=s.version WHERE v.namespace=? AND s.subject=? ORDER BY v.id LIMIT 4097""",
                               (self.namespace, subject.ref)).fetchall()
            rows = db.execute("""
                WITH RECURSIVE affected(id) AS (
                    SELECT v.id FROM lineage_versions v JOIN lineage_subjects s ON v.id=s.version
                    WHERE v.namespace=? AND s.subject=?
                    UNION
                    SELECT i.artifact FROM lineage_inputs i JOIN affected a ON a.id=i.input
                )
                SELECT c.store,c.key,c.revision,p.store,p.key,p.revision FROM affected a
                JOIN lineage_versions c ON c.id=a.id JOIN lineage_inputs i ON i.artifact=c.id
                JOIN lineage_versions p ON p.id=i.input ORDER BY c.id,p.id LIMIT 4097
            """, (self.namespace, subject.ref)).fetchall()
        if len(roots) > 4096 or len(rows) > 4096:
            raise InputError("lineage exceeds the 4096-entry drill budget")
        dependencies = defaultdict(list)
        for store, key, revision, *parent in rows:
            dependencies[VersionRef(store, key, revision)].append(asdict(VersionRef(*parent)))
        return {"roots": [asdict(VersionRef(*root)) for root in roots],
                "derivations": [{"artifact": asdict(ref), "inputs": parents}
                                for ref, parents in dependencies.items()]}
