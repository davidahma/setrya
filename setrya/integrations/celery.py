"""A one-use checkpoint for a live Celery worker in a local, disposable test."""

from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
from time import monotonic, sleep

from setrya.adapter import Checkpoint
from setrya.drill import InputError, label


class CeleryCheckpoint:
    """Coordinate one bound task with its test through a private shared SQLite file.

    Create a new file for each task and run. Pass a known local path to the worker
    from the test setup. Do not expose this hook to production task callers.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()

    @classmethod
    def create(cls, path: str | Path, *, run_id: str, task_id: str):
        """Reserve a new control file before dispatching the selected Celery task."""
        gate = cls(path)
        label(run_id, "run ID")
        label(task_id, "Celery task ID")
        descriptor = os.open(gate.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            with closing(gate._connect()) as db, db:
                db.execute("""CREATE TABLE gate (
                    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    state TEXT NOT NULL, document TEXT, revision TEXT, name TEXT, deadline REAL
                )""")
                db.execute("INSERT INTO gate(run_id,task_id,state) VALUES (?,?,'pending')", (run_id, task_id))
        except BaseException:
            gate.path.unlink(missing_ok=True)
            raise
        return gate

    def _connect(self):
        # Opening a worker hook at the wrong path must not initialize a new journal.
        return sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=1)

    def hold(self, task, *, run_id: str, document: str, revision: str, name: str, timeout: float = 30) -> None:
        """Call after the bound task captured its inputs and immediately before its write.

        The worker continues only after the driver releases this exact checkpoint.
        Eager, direct, retried or redelivered calls cannot stand in for the scheduled task.
        """
        request = task.request
        if (request.is_eager or request.called_directly or request.retries != 0
                or (request.delivery_info or {}).get("redelivered")):
            raise InputError("the checkpoint requires the first delivery of a live Celery task")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise InputError("the checkpoint needs a finite positive timeout")
        attempt = label(request.id, "Celery task ID")
        for field, value in (("run ID", run_id), ("document", document), ("revision", revision), ("checkpoint", name)):
            label(value, field)
        with closing(self._connect()) as db:
            deadline = monotonic() + timeout
            with db:
                changed = db.execute("""UPDATE gate SET state='held', document=?, revision=?, name=?, deadline=?
                    WHERE run_id=? AND task_id=? AND state='pending'""",
                                     (document, revision, name, deadline, run_id, attempt)).rowcount
                if changed != 1:
                    raise InputError("the Celery task does not own an unused checkpoint for this run")
            while True:
                if db.execute("SELECT state FROM gate").fetchone()[0] == "released":
                    return
                remaining = deadline - monotonic()
                if remaining <= 0:
                    # A release accepted just before the deadline can race this poll.
                    with db:
                        expired = db.execute("UPDATE gate SET state='expired' WHERE state='held'").rowcount
                    if expired:
                        raise TimeoutError("Celery checkpoint expired before release")
                    continue
                sleep(min(0.01, remaining))

    def checkpoint(self) -> Checkpoint | None:
        """Poll for the worker's acknowledged capture while the hold is still live."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT run_id, task_id, document, revision, name, deadline FROM gate WHERE state='held'").fetchone()
        return Checkpoint(*row[:5]) if row and monotonic() < row[5] else None

    def release(self) -> None:
        """Release one held worker. An early, expired or repeated release is an error."""
        with closing(self._connect()) as db, db:
            # Serialize expiration with the release decision. Checking time while
            # holding the write lock avoids publishing a release observed after expiry.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE gate SET state='released' WHERE state='held' AND deadline > ?", (monotonic(),)).rowcount != 1:
                raise InputError("no live Celery checkpoint is available for release")
