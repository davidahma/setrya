"""Recompute the answer decision, including incomplete and inconsistent endpoint traces."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.adapter import Checkpoint, Publication, Receipt
from setrya.checker import Status
from setrya.drill import InputError
from setrya.examples.answer_cache.contract import CHECKPOINT, PRIVATE, PRIVATE_TEXT, PUBLIC, PUBLIC_TEXT, QUESTION
from setrya.examples.answer_cache.drill import run
from setrya.examples.answer_cache.evidence import MANIFEST, replay


class SyntheticAnswers:
    """Use the same fields as the real HTTP client, with one controlled fault at a time."""
    fault = None

    def __init__(self, run_id, mode, directory):
        self.run_id, self.mode = run_id, mode
        self.revoked, self.released = False, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.fault == "close":
            raise ConnectionError("cleanup")

    def seed(self):
        pass

    def start_old(self):
        return "task-1"

    def checkpoint(self):
        if self.fault == "no-checkpoint" or (self.fault == "escaped" and self.revoked):
            return None
        return Checkpoint(self.run_id, "task-1", "policy", "granted", CHECKPOINT)

    def captured(self):
        if self.fault == "no-capture":
            return None
        return {"run_id": self.run_id, "attempt": "task-1", "caller": "revoked", "question": QUESTION,
                "revision": "granted", "answer": PUBLIC_TEXT if self.fault == "public-only" else PRIVATE_TEXT + " " + PUBLIC_TEXT,
                "sources": [PRIVATE, PUBLIC]}

    def revoke(self):
        self.revoked = True
        return Publication(self.run_id, "policy", "revoked")

    def release(self):
        self.released = True
        self.revoked_reads = 0

    def receipt(self):
        if self.fault == "missing-write":
            return None
        if self.mode == "broken" or self.fault == "unseen":
            return Receipt(self.run_id, "task-1", "policy", "granted", "committed", self.run_id, 7)
        return Receipt(self.run_id, "task-1", "policy", "granted", "rejected")

    def answer(self, caller, question):
        late = self.released and self.mode == "broken" and caller == "revoked"
        private = not self.revoked or caller == "retained" or late
        hit = private and not (self.fault == "retained-miss" and caller == "retained" and self.released)
        row = {
            "answer": (PRIVATE_TEXT + " " if private else "") + PUBLIC_TEXT,
            "sources": [PRIVATE, PUBLIC] if private else [PUBLIC], "cache_hit": hit,
            "cache_writer": ("task-1" if late else "seed") if hit else None,
            "generation": self.run_id, "commit": 6 if self.fault == "unseen" else 7,
            "route": "answer", "caller": caller, "http_status": 200,
        }
        if self.released and caller == "revoked":
            self.revoked_reads += 1
            if self.fault == "second-leak" and self.revoked_reads == 2:
                row.update(answer=PRIVATE_TEXT + " " + PUBLIC_TEXT, sources=[PRIVATE, PUBLIC], cache_hit=True,
                           cache_writer="task-1")
            if self.fault == "wrong-user":
                row["caller"] = "retained"
            if self.fault == "opaque":
                row["answer"] = "another response"
            if self.fault == "bad-http":
                row["http_status"] = 403
            if self.fault == "body-leak":
                row["answer"] = PRIVATE_TEXT + " " + PUBLIC_TEXT
        return row


class AnswerCacheDrillTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)

    def exercise(self, mode, fault=None):
        app = type("Fixture", (SyntheticAnswers,), {"fault": fault})
        directory = self.root / (fault or mode)
        live = run(directory, mode, application=app, wait_seconds=0.01)
        self.assertEqual(replay(directory), live)
        return directory, live

    def test_late_write_is_visible_on_the_actual_answer_rows(self):
        _, (verdict, summary) = self.exercise("broken")
        self.assertIs(verdict.status, Status.FAIL)
        self.assertIn("old attempt task-1", verdict.attribution)
        self.assertIn("final · revoked · HTTP 200 · cache hit · private-and-public", summary)
        self.assertIn("final · retained · HTTP 200 · cache hit · private-and-public", summary)
        fixed, (verdict, summary) = self.exercise("guarded")
        self.assertIs(verdict.status, Status.PASS)
        self.assertIn("final · revoked · HTTP 200 · cache miss · public-only", summary)
        self.assertNotIn(PRIVATE_TEXT, (fixed / MANIFEST).read_text())

    def test_missing_observations_do_not_pass(self):
        for fault in ("no-capture", "public-only", "no-checkpoint", "escaped", "missing-write", "unseen", "wrong-user", "opaque", "bad-http"):
            with self.subTest(fault=fault):
                _, (verdict, _) = self.exercise("guarded", fault)
                self.assertIs(verdict.status, Status.NOT_TESTED)
        _, (verdict, _) = self.exercise("guarded", "retained-miss")
        self.assertIs(verdict.status, Status.FAIL)

    def test_body_leak_wins_over_filtered_sources_or_later_cleanup_error(self):
        _, (verdict, _) = self.exercise("guarded", "body-leak")
        self.assertIs(verdict.status, Status.FAIL)
        _, (verdict, _) = self.exercise("guarded", "second-leak")
        self.assertIs(verdict.status, Status.FAIL)
        self.assertEqual(verdict.probe, "public-answer")
        _, (verdict, _) = self.exercise("broken", "close")
        self.assertIs(verdict.status, Status.FAIL)

    def test_replay_checks_both_directions_of_the_binding(self):
        directory, _ = self.exercise("guarded")
        path = directory / MANIFEST
        data = json.loads(path.read_bytes())
        for key, value in (("cache_writer", "different"), ("status", 201), ("body", "public-only")):
            with self.subTest(key=key):
                row = data["observations"][-1] | {key: value}
                path.write_text(json.dumps(data | {"observations": [*data["observations"][:-1], row]}))
                with self.assertRaises(InputError):
                    replay(directory)
        path.write_text(json.dumps(data | {"held_after_reads": None}))
        self.assertIs(replay(directory)[0].status, Status.NOT_TESTED)
        path.write_text(json.dumps(data | {"report_sha256": "0" * 64}))
        with self.assertRaises(InputError):
            replay(directory)
