"""Regressions specific to the synthetic producer and its delayed worker."""

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from setrya.examples.offboarding import fixture


class OffboardingExampleTests(unittest.TestCase):
    def test_delayed_job_changes_the_actual_support_read(self):
        for mode, after, baseline in (
            ("broken", "present", "present"),
            ("fixed", "absent", "present"),
            ("inconclusive", "absent", "unavailable"),
        ):
            with self.subTest(mode=mode):
                packet = fixture.run_case(mode)
                probes = {p["id"]: p for p in packet["probes"]}
                self.assertEqual(len(probes), 6)
                self.assertEqual(probes["support-after_worker"]["before"]["subject"], baseline)
                self.assertEqual(probes["support-after_offboarding"]["after"]["subject"], "absent")
                self.assertEqual(probes["source-after_worker"]["after"]["subject"], "absent")
                self.assertEqual(probes["support-after_worker"]["after"]["subject"], after)
                self.assertEqual(probes["summary-after_worker"]["after"]["subject"], after)
                for probe in probes.values():
                    self.assertEqual(probe["after"]["control"], "present")
                    self.assertEqual(probe["before"]["control"], "present")
                    if probe["checkpoint"] == "after_worker":
                        self.assertEqual(probe["worker"], {
                            "captured_before_offboarding": True, "finished_after_offboarding": True,
                        })

    def test_registered_request_binds_job_and_tenant_in_the_delete_transaction(self):
        with TemporaryDirectory() as work:
            path = Path(work).resolve() / "app.sqlite"
            fixture._seed(path)
            fixture._register_request(path, {"request_id": "request-a"}, "original-digest")
            with self.assertRaises(ValueError):
                fixture._offboard(path, "birch", "request-a")
            with fixture._database(path, read_only=True) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM ticket").fetchone(), (2,))
                self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone(), (0,))

            with fixture._database(path) as db:
                db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON ticket "
                           "BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
            with self.assertRaises(sqlite3.Error):
                fixture._offboard(path, "acme", "request-a")
            with fixture._database(path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone(), (0,))
                db.execute("DROP TRIGGER reject_delete")

            job_id = fixture._offboard(path, "acme", "request-a")
            with self.assertRaises(sqlite3.IntegrityError), fixture._database(path) as db:
                db.execute("UPDATE requests SET digest = 'different-digest'")
            self.assertEqual(fixture._read_deletion_job(path, job_id)["request_sha256"], "original-digest")
