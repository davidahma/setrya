import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from setrya.examples.offboarding.sqlite import observe

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = "setrya.examples.offboarding.sqlite.observe"
POLICY = ROOT / "setrya/examples/offboarding/sqlite/policy.json"


class BoundProducerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.manifest = self.work / "request.json"
        self.output = self.work / "packet.json"
        self.data = {
            "version": 1,
            "request_id": "a" * 32,
            "created_at": "2020-01-01T00:00:00Z",
            "context": "staging",
            "target": "sqlite-support",
            "revision": "build-42",
            "run_id": "ci-attempt-1",
            "policy_sha256": hashlib.sha256(POLICY.read_bytes()).hexdigest(),
            "items": [],
        }
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data) + "\n")

    def produce(self, case, *extra):
        return subprocess.run(
            [
                sys.executable,
                "-m", PRODUCER,
                "--case",
                case,
                "--request",
                self.manifest,
                "--output",
                self.output,
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_bound_clean_residual_and_unavailable_reads(self):
        digest = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        for case, after in (
            ("clean", {"subject": "absent", "control": "present"}),
            ("residual", {"subject": "present", "control": "present"}),
            ("unavailable", {"subject": "unavailable", "control": "unavailable"}),
        ):
            with self.subTest(case=case):
                result = self.produce(case, "--revision", "build-42")
                self.assertEqual(result.returncode, 0, result.stderr)
                envelope = json.loads(self.output.read_bytes())
                self.assertEqual(set(envelope), {"version", "job", "observations"})
                job = envelope["job"]
                self.assertEqual(
                    set(job), {"id", "started_at", "observed_at", "request_sha256"}
                )
                self.assertEqual(job["request_sha256"], digest)
                self.assertGreaterEqual(job["started_at"], self.data["created_at"])
                self.assertGreaterEqual(job["observed_at"], job["started_at"])
                packet = envelope["observations"]
                self.assertEqual(packet["offboarding"], "completed")
                self.assertEqual(packet["run_id"], self.data["run_id"])
                probes = {probe["id"]: probe for probe in packet["probes"]}
                self.assertEqual(
                    probes["source"]["after"],
                    {"subject": "absent", "control": "present"},
                )
                self.assertEqual(probes["cache"]["after"], after)

    def test_wrong_context_build_target_timestamp_or_run_override_rejected(self):
        for change, args in (
            ({"context": "customer"}, ["--revision", "build-42"]),
            ({"target": "opendesk"}, ["--revision", "build-42"]),
            ({"created_at": "2999-01-01T00:00:00Z"}, ["--revision", "build-42"]),
            ({}, ["--revision", "other-build"]),
            ({}, []),
            ({}, ["--revision", "build-42", "--run-id", "override"]),
        ):
            with self.subTest(change=change, args=args):
                self.manifest.write_text(json.dumps({**self.data, **change}))
                result = self.produce("clean", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_nonregular_manifest_is_rejected_without_waiting_for_a_writer(self):
        self.manifest.unlink()
        os.mkfifo(self.manifest)
        result = self.produce("clean", "--revision", "build-42")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(str(self.manifest), result.stderr)
        self.assertFalse(self.output.exists())

    def test_malformed_and_oversize_manifests_do_not_expose_the_contents(self):
        valid = self.manifest.read_bytes()
        for payload in (
            b'{"target": "private test data",' + valid[1:],
            valid + b" " * (1024 * 1024),
            b"private test data\xff",
        ):
            with self.subTest(length=len(payload)):
                self.manifest.write_bytes(payload)
                result = self.produce("clean", "--revision", "build-42")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("private test data", result.stderr + result.stdout)
                self.assertFalse(self.output.exists())

    def test_mismatched_tenant_or_receipt_cannot_satisfy_neighboring_request(self):
        path = self.work / "app.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            requests = []
            for token in ("a", "b"):
                self.data["request_id"] = token * 32
                self.write_manifest()
                request = observe.load_request(self.manifest, "build-42")
                observe.register_request(db, request)
                tenants = observe.tenants_for(db, request)
                observe.seed(db, tenants)
                requests.append((request, tenants))
            (first, first_pair), (second, second_pair) = requests
            self.assertEqual(len(set(first_pair + second_pair)), 4)
            with self.assertRaises(ValueError):
                observe.offboard(db, "clean", second_pair[0], first)
            self.assertEqual(
                db.execute("SELECT count(*) FROM tickets").fetchone(), (4,)
            )
            job = observe.offboard(db, "clean", first_pair[0], first)
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            ) as reader:
                with self.assertRaises(ValueError):
                    observe.read_job(reader, second, job)
                record, status = observe.read_job(reader, first, job)
                self.assertEqual(status, "completed")
                self.assertEqual(record["request_sha256"], first.digest)
                remaining = {
                    row[0] for row in reader.execute("SELECT tenant FROM tickets")
                }
                self.assertEqual(remaining, {first_pair[1], *second_pair})

    def test_failure_to_record_completion_rolls_back_deletion(self):
        path = self.work / "app.db"
        request = observe.load_request(self.manifest, "build-42")
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            observe.register_request(db, request)
            tenants = observe.tenants_for(db, request)
            observe.seed(db, tenants)
            db.execute("""CREATE TRIGGER interrupt_completion BEFORE INSERT ON jobs
                          BEGIN SELECT RAISE(ABORT, 'interrupted'); END""")
            with self.assertRaises(sqlite3.Error):
                observe.offboard(db, "clean", tenants[0], request)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
            self.assertEqual(
                reader.execute("SELECT count(*) FROM jobs").fetchone(), (0,)
            )
            for table in ("tickets", "cached_answers"):
                rows = {row[0] for row in reader.execute(f"SELECT tenant FROM {table}")}
                self.assertEqual(rows, set(tenants))
