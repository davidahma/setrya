"""A synthetic support service, its summary worker, and independent observations.

Adapted from Axiom v0.3.2. The worker scenario now emits CI gate packets and
can bind its deletion job to a separately issued staging request.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
from pathlib import Path
from socketserver import TCPServer
from tempfile import TemporaryDirectory
from threading import Event, Thread
from uuid import uuid4

from setrya.offboarding.inputs import read_json
from setrya.offboarding.receipt import validate_request


@dataclass(frozen=True)
class Ticket:
    workspace: str
    source_id: str
    reference: str


TICKETS = (
    Ticket("acme", "ticket-a", "CIRRUS-483"),
    Ticket("birch", "ticket-b", "MEADOW-729"),
)
WAIT_SECONDS = 10


@contextmanager
def _database(path: Path, *, read_only: bool = False):
    mode = "ro" if read_only else "rwc"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            yield db


def _seed(path: Path) -> None:
    with _database(path) as db:
        db.executescript(
            """
            CREATE TABLE workspace (
                id TEXT PRIMARY KEY, generation INTEGER NOT NULL, active INTEGER NOT NULL
            );
            CREATE TABLE ticket (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspace(id),
                reference TEXT NOT NULL
            );
            CREATE TABLE summary (
                source_id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspace(id),
                answer TEXT NOT NULL
            );
            """
        )
        for ticket in TICKETS:
            db.execute("INSERT INTO workspace VALUES (?, 1, 1)", (ticket.workspace,))
            db.execute(
                "INSERT INTO ticket VALUES (?, ?, ?)",
                (ticket.source_id, ticket.workspace, ticket.reference),
            )
    for ticket in TICKETS:
        pending = _read_job(path, ticket.source_id)
        _finish_job(path, pending, guard=True)


@dataclass(frozen=True)
class SummaryJob:
    source_id: str
    workspace: str
    generation: int
    answer: str


def _read_job(path: Path, source_id: str) -> SummaryJob:
    with _database(path, read_only=True) as db:
        row = db.execute(
            """SELECT t.workspace, w.generation, t.reference
               FROM ticket t JOIN workspace w ON w.id = t.workspace
               WHERE t.id = ? AND w.active = 1""",
            (source_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("The fixture worker could not capture its source")
    workspace, generation, reference = row
    return SummaryJob(
        source_id, workspace, generation, f"Use {reference} for this escalation."
    )


def _finish_job(path: Path, job: SummaryJob, *, guard: bool) -> str:
    with _database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if guard:
            current = db.execute(
                "SELECT 1 FROM workspace WHERE id = ? AND active = 1 AND generation = ?",
                (job.workspace, job.generation),
            ).fetchone()
            if current is None:
                return "discarded"
        db.execute(
            """INSERT INTO summary(source_id, workspace, answer) VALUES (?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET answer = excluded.answer""",
            (job.source_id, job.workspace, job.answer),
        )
    return "written"


def _held_job(path: Path, captured: Event, resume: Event, *, guard: bool) -> str:
    job = _read_job(path, TICKETS[0].source_id)
    captured.set()
    if not resume.wait(WAIT_SECONDS):
        raise TimeoutError("The fixture worker never received its resume signal")
    return _finish_job(path, job, guard=guard)


def _register_request(path, request, digest):
    # The dispatcher maps this synthetic request to Acme before running the job.
    with _database(path) as db:
        db.executescript("""
            CREATE TABLE requests (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, workspace TEXT NOT NULL,
                UNIQUE(id, digest, workspace)
            );
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                digest TEXT NOT NULL, workspace TEXT NOT NULL, started_at TEXT NOT NULL,
                FOREIGN KEY(request_id, digest, workspace) REFERENCES requests(id, digest, workspace)
            );
        """)
        db.execute("INSERT INTO requests VALUES (?, ?, ?)", (request["request_id"], digest, "acme"))


def _utc_now():
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _offboard(path, workspace, request_id=None):
    job_id = uuid4().hex
    with _database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if request_id is not None:
            # Commit the exact registered request and tenant with the deletion.
            registered = db.execute(
                "INSERT INTO jobs SELECT ?, id, digest, workspace, ? FROM requests "
                "WHERE id = ? AND workspace = ?",
                (job_id, _utc_now(), request_id, workspace),
            )
            if registered.rowcount != 1:
                raise ValueError("The deletion job does not match its registered workspace.")
        db.execute(
            "UPDATE workspace SET active = 0, generation = generation + 1 WHERE id = ?",
            (workspace,),
        )
        db.execute("DELETE FROM ticket WHERE workspace = ?", (workspace,))
        db.execute("DELETE FROM summary WHERE workspace = ?", (workspace,))
    return job_id


def _read_deletion_job(path, job_id):
    with _database(path, read_only=True) as reader:
        digest, started = reader.execute(
            "SELECT digest, started_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return {"id": job_id, "started_at": started, "observed_at": _utc_now(), "request_sha256": digest}


@contextmanager
def _support_service(path: Path, credential: str):
    """Serve an internal support role with read scope for both synthetic workspaces."""

    routes = {
        f"/support/summary/{ticket.workspace}": ticket.workspace for ticket in TICKETS
    }

    class SupportHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 200
            body = {}
            workspace = routes.get(self.path)
            if self.headers.get("Authorization") != f"Bearer {credential}":
                status = 403
            elif workspace is None:
                status = 404
            else:
                with _database(path, read_only=True) as db:
                    row = db.execute(
                        "SELECT answer FROM summary WHERE workspace = ?", (workspace,)
                    ).fetchone()
                body = {"answer": row[0] if row else None}
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    # The handler only needs the bound port. HTTPServer would resolve the
    # loopback address through the host's name service on every startup.
    with TCPServer(("127.0.0.1", 0), SupportHandler) as server:
        thread = Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join()


def _support_observation(port: int, ticket: Ticket, credential: str | None) -> str:
    # The query contains only the workspace. The expected marker stays in the observer.
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    with closing(HTTPConnection("127.0.0.1", port, timeout=WAIT_SECONDS)) as client:
        client.request("GET", f"/support/summary/{ticket.workspace}", headers=headers)
        response = client.getresponse()
        if response.status != 200:
            return "unavailable"
        answer = json.loads(response.read())["answer"]
    if answer is None:
        return "not_returned"
    if isinstance(answer, str) and ticket.reference in answer:
        return "returned"
    raise RuntimeError("The support endpoint returned an unexpected fixture answer")


SCOPES = {"source": "tickets", "summary": "stored-summaries", "support": "support-agent.summary"}
CHECKPOINTS = ("after_offboarding", "after_worker")


def policy():
    return {
        "version": 1,
        "target": "sqlite-support",
        "probes": [
            {"id": f"{surface}-{checkpoint}", "scope": scope, "checkpoint": checkpoint}
            for checkpoint in CHECKPOINTS for surface, scope in SCOPES.items()
        ],
    }


def revision():
    return hashlib.sha256(files(__package__).joinpath("fixture.py").read_bytes()).hexdigest()


def _observe(
    path: Path, port: int, credential: str, phase: str, *, lose_baseline: bool = False
):
    observations = []
    for ticket in TICKETS:
        with _database(path, read_only=True) as db:
            source = db.execute(
                "SELECT 1 FROM ticket WHERE id = ? AND workspace = ?",
                (ticket.source_id, ticket.workspace),
            ).fetchone()
            summary = db.execute(
                "SELECT 1 FROM summary WHERE source_id = ? AND workspace = ?",
                (ticket.source_id, ticket.workspace),
            ).fetchone()
        auth = None if lose_baseline and ticket.workspace == "acme" else credential
        support = _support_observation(port, ticket, auth)
        for surface, state in (
            ("source", "present" if source else "absent"),
            ("summary", "present" if summary else "absent"),
            ("support", support),
        ):
            observations.append(
                {
                    "phase": phase,
                    "subject": ticket.workspace,
                    "surface": surface,
                    "state": state,
                    "observed_at": datetime.now(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                }
            )
    return observations


def _measure(mode, *, request_dir=None):
    """Collect timestamped reads and the actual worker result from one disposable run."""
    if mode not in {"broken", "fixed", "inconclusive"}:
        raise ValueError("Unknown offboarding fixture case.")
    run_id = mode
    request_id = None
    with TemporaryDirectory(prefix="setrya-offboarding-") as directory:
        path = Path(directory).resolve() / "support.sqlite3"
        _seed(path)
        if request_dir is not None:
            request, digest = read_json(request_dir / "request.json", "request")
            validate_request(request)
            selected, policy_hash = read_json(request_dir / "policy.json", "policy")
            if (request["context"] != "staging" or request["target"] != "sqlite-support"
                    or request["revision"] != revision() or request["created_at"] > _utc_now()
                    or selected != policy() or request["policy_sha256"] != policy_hash):
                raise ValueError("The request does not match the synthetic staging application.")
            _register_request(path, request, digest)
            run_id, request_id = request["run_id"], request["request_id"]

        credential = secrets.token_urlsafe(24)
        captured, resume = Event(), Event()
        with _support_service(path, credential) as port:
            observations = _observe(path, port, credential, "baseline", lose_baseline=mode == "inconclusive")
            with ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(_held_job, path, captured, resume, guard=mode != "broken")
                try:
                    if not captured.wait(WAIT_SECONDS):
                        raise TimeoutError("The fixture worker never captured its source")
                    job_id = _offboard(path, "acme", request_id)
                    observations.extend(_observe(path, port, credential, "offboarded"))
                finally:
                    resume.set()
                terminal = worker.result(timeout=WAIT_SECONDS)
        # Reopen the caller's route after the same worker finishes its write attempt.
        with _support_service(path, credential) as port:
            observations.extend(_observe(path, port, credential, "worker_finished"))
        job = _read_deletion_job(path, job_id) if request_id is not None else None

    case = {
        "case": mode, "origin": "controlled-fixture", "observations": observations,
        "worker": {"captured_before_delete": captured.is_set(), "terminal": terminal},
    }
    return case, run_id, job


def reference_case(mode):
    """Run a fresh worker and collect the historical, timestamped report format."""
    case, _, _ = _measure(mode)
    return case


def run_case(mode, *, request_dir=None):
    """Export the same observed reads in the staging gate or issued request format."""
    case, run_id, job = _measure(mode, request_dir=request_dir)
    reads = {}
    for row in case["observations"]:
        pair = reads.setdefault((row["phase"], row["surface"]), {})
        role = "subject" if row["subject"] == TICKETS[0].workspace else "control"
        pair[role] = {"returned": "present", "not_returned": "absent"}.get(row["state"], row["state"])
    probes = []
    for checkpoint, phase in zip(CHECKPOINTS, ("offboarded", "worker_finished"), strict=True):
        for surface, scope in SCOPES.items():
            probe = {
                "id": f"{surface}-{checkpoint}", "scope": scope, "checkpoint": checkpoint,
                "before": reads["baseline", surface], "after": reads[phase, surface],
            }
            if checkpoint == "after_worker":
                probe["worker"] = {
                    "captured_before_offboarding": case["worker"]["captured_before_delete"],
                    "finished_after_offboarding": case["worker"]["terminal"] in ("written", "discarded"),
                }
            probes.append(probe)
    packet = {
        "version": 1, "target": "sqlite-support", "revision": revision(), "run_id": run_id,
        "offboarding": "completed", "probes": probes,
    }
    if job is None:
        return packet
    return {"version": 1, "job": job, "observations": packet}
