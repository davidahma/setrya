"""Exercise a synthetic application's offboarding job and export measured reads."""

import argparse
import hashlib
from importlib.resources import files
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from setrya.offboarding.observer import open_reader, read_pair


def utc_now():
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Request:
    id: str
    digest: str
    created_at: str
    run_id: str


def load_request(path, revision):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Request contains a duplicate field.")
        return result

    def nonblocking_opener(name, flags):
        return os.open(name, flags | getattr(os, "O_NONBLOCK", 0))

    with open(path, "rb", opener=nonblocking_opener) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Request must be a regular file.")
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Request exceeds the size limit.")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if (
            set(data)
            != {
                "version",
                "request_id",
                "created_at",
                "context",
                "target",
                "revision",
                "run_id",
                "policy_sha256",
                "items",
            }
            or type(data["version"]) is not int
            or data["version"] != 1
        ):
            raise ValueError
        if data["context"] != "staging" or data["target"] != "sqlite-support":
            raise ValueError
        if data["revision"] != revision or not re.fullmatch(
            "[0-9a-f]{32}", data["request_id"]
        ):
            raise ValueError
        if not re.fullmatch("[0-9a-f]{64}", data["policy_sha256"]):
            raise ValueError
        identifier(data["run_id"])
        created = datetime.strptime(data["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        if (
            created.strftime("%Y-%m-%dT%H:%M:%SZ") != data["created_at"]
            or data["created_at"] > utc_now()
        ):
            raise ValueError
        if not isinstance(data["items"], list) or len(data["items"]) > 256:
            raise ValueError
        seen = set()
        for item in data["items"]:
            if set(item) != {"id", "kind"} or item["kind"] not in (
                "provider_statement",
                "retention_schedule",
            ):
                raise ValueError
            item_id = identifier(item["id"])
            if item_id in seen:
                raise ValueError
            seen.add(item_id)
    except (
        argparse.ArgumentTypeError,
        KeyError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise ValueError(
            "Expected a valid staging request for sqlite-support and the tested revision."
        ) from None
    return Request(
        data["request_id"],
        hashlib.sha256(raw).hexdigest(),
        data["created_at"],
        data["run_id"],
    )


def register_request(db, request):
    # The application assigns tenant keys when it accepts the staging request.
    # Raw tenant keys stay inside this database.
    with db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, subject TEXT UNIQUE NOT NULL,
                retained TEXT UNIQUE NOT NULL, CHECK(subject != retained), UNIQUE(id, digest, subject)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, digest TEXT NOT NULL,
                subject TEXT NOT NULL, started_at TEXT NOT NULL, state TEXT NOT NULL,
                FOREIGN KEY (request_id, digest, subject) REFERENCES requests(id, digest, subject)
            )
        """)
        db.execute(
            "INSERT INTO requests VALUES (?, ?, ?, ?)",
            (
                request.id,
                request.digest,
                uuid.uuid4().hex,
                uuid.uuid4().hex,
            ),
        )


def tenants_for(db, request):
    row = db.execute(
        "SELECT subject, retained FROM requests WHERE id = ? AND digest = ?",
        (request.id, request.digest),
    ).fetchone()
    if row is None:
        raise ValueError("The application has no matching request.")
    return row


def seed(db, tenants):
    with db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS tickets (tenant TEXT PRIMARY KEY, ticket TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS cached_answers (tenant TEXT PRIMARY KEY, answer TEXT)"
        )
        for tenant in tenants:
            db.execute(
                "INSERT INTO tickets VALUES (?, ?)", (tenant, f"{tenant} ticket")
            )
            db.execute(
                "INSERT INTO cached_answers VALUES (?, ?)", (tenant, f"{tenant} answer")
            )


def offboard(db, case, subject, request=None):
    started_at = utc_now()
    job_id = uuid.uuid4().hex
    with db:
        if request and (
            subject != tenants_for(db, request)[0] or started_at < request.created_at
        ):
            raise ValueError("The deletion job does not match the registered request.")
        db.execute("DELETE FROM tickets WHERE tenant = ?", (subject,))
        if case != "residual":
            db.execute("DELETE FROM cached_answers WHERE tenant = ?", (subject,))
        if request:
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, 'completed')",
                (job_id, request.id, request.digest, subject, started_at),
            )
    return job_id


def read_job(reader, request, job_id):
    row = reader.execute(
        """
        SELECT j.id, j.digest, j.started_at, j.state
        FROM jobs j JOIN requests r
          ON r.id = j.request_id AND r.digest = j.digest AND r.subject = j.subject
        WHERE j.id = ? AND r.id = ? AND r.digest = ?
    """,
        (job_id, request.id, request.digest),
    ).fetchone()
    if row is None or row[3] != "completed":
        raise ValueError("No committed deletion job for this request.")
    observed_at = utc_now()
    if not request.created_at <= row[2] <= observed_at:
        raise ValueError("Clock changed during the request.")
    job = {
        "id": row[0],
        "request_sha256": row[1],
        "started_at": row[2],
        "observed_at": observed_at,
    }
    return job, row[3]


def observe(path, case, run_id, revision, request=None):
    with closing(sqlite3.connect(path)) as writer:
        tenants = ("acme", "birch")
        if request:
            writer.execute("PRAGMA foreign_keys = ON")
            register_request(writer, request)
            tenants = tenants_for(writer, request)
        seed(writer, tenants)
        with (
            open_reader(path) as source_reader,
            open_reader(path) as cache_reader,
        ):
            probes = [
                (
                    "source",
                    "tickets",
                    source_reader,
                    "SELECT 1 FROM tickets WHERE tenant = ?",
                ),
                (
                    "cache",
                    "cached_answers",
                    cache_reader,
                    "SELECT 1 FROM cached_answers WHERE tenant = ?",
                ),
            ]
            observations = [
                {
                    "id": probe_id,
                    "scope": scope,
                    "checkpoint": "after_offboarding",
                    "before": read_pair(reader, query, tenants),
                }
                for probe_id, scope, reader, query in probes
            ]
            try:
                job_id = offboard(writer, case, tenants[0], request)
                status = "completed"
            except sqlite3.Error:
                if request:
                    raise
                status = "unconfirmed"
            if case == "unavailable":
                cache_reader.close()
            for item, (_, _, reader, query) in zip(observations, probes):
                item["after"] = read_pair(reader, query, tenants)
            if request:
                job, status = read_job(source_reader, request, job_id)

    packet = {
        "version": 1,
        "target": "sqlite-support",
        "revision": revision,
        "run_id": run_id,
        "offboarding": status,
        "probes": observations,
    }
    return {"version": 1, "job": job, "observations": packet} if request else packet


def source_revision():
    source = files(__package__).joinpath("observe.py") if __package__ else Path(__file__)
    return hashlib.sha256(source.read_bytes()).hexdigest()


def identifier(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", value):
        raise argparse.ArgumentTypeError(
            "Use 1 to 128 letters, digits, dots, underscores, or hyphens. "
            "Start with a letter or digit."
        )
    return value


def main(argv=None, *, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument(
        "--case", choices=("clean", "residual", "unavailable"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--request", type=Path, help="staging request issued before this job"
    )
    parser.add_argument(
        "--run-id", type=identifier, help="CI run identifier (default: a UUID)"
    )
    parser.add_argument(
        "--revision",
        type=identifier,
        help="tested build identifier (default: script SHA-256)",
    )
    args = parser.parse_args(argv)
    try:
        request = None
        if args.request:
            if args.run_id or not args.revision:
                parser.error(
                    "With --request, supply the tested --revision and omit --run-id."
                )
            if args.output.exists() and args.output.samefile(args.request):
                parser.error("--output must differ from --request.")
            request = load_request(args.request, args.revision)
        run_id = request.run_id if request else (args.run_id or uuid.uuid4().hex)
        revision = (
            args.revision or source_revision()
        )
        with tempfile.TemporaryDirectory() as directory:
            observations = observe(
                Path(directory).resolve() / "support.db",
                args.case,
                run_id,
                revision,
                request,
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=args.output.parent, delete=False, encoding="utf-8"
        ) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(observations, stream, indent=2)
                stream.write("\n")
                stream.close()
                temporary.replace(args.output)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"Could not produce observations ({type(exc).__name__}).\n")
    return 0


if __name__ == "__main__":
    main()
