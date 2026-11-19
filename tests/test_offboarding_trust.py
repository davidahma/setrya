import copy
import io
import json
import os
from contextlib import closing, redirect_stderr
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from setrya.offboarding.cli import main
from setrya.offboarding.gate import evaluate
from setrya.offboarding.observer import open_reader, read_pair

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "tests/fixtures/offboarding-receipt-axiom-v0.3.2.json"


class TrustTests(unittest.TestCase):
    def test_writer_restoring_both_tenants_cannot_create_a_passing_read_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "support.db")
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("CREATE TABLE tickets (tenant TEXT)")
                tenants = [("acme",), ("birch",)]
                query = "SELECT 1 FROM tickets WHERE tenant = ?"
                with writer:
                    writer.executemany("INSERT INTO tickets VALUES (?)", tenants)
                with open_reader(path) as reader:
                    before = read_pair(reader, query, ("acme", "birch"))
                    with writer:
                        writer.execute("DELETE FROM tickets")

                    def restore_on_control(sql):
                        if "'birch'" in sql:
                            with writer:
                                writer.executemany("INSERT INTO tickets VALUES (?)", tenants)

                    reader.set_trace_callback(restore_on_control)
                    after = read_pair(reader, query, ("acme", "birch"))
                    reader.set_trace_callback(None)
                    restored = read_pair(reader, query, ("acme", "birch"))
        self.assertEqual(before, {"subject": "present", "control": "present"})
        self.assertEqual(after, {"subject": "absent", "control": "absent"})
        self.assertEqual(restored, before)
        probe = {"id": "source", "scope": "tickets", "checkpoint": "after_offboarding"}
        policy = {"version": 1, "target": "fixture", "probes": [probe]}
        packet = {**policy, "run_id": "test", "revision": "fixture", "offboarding": "completed",
                  "probes": [{**probe, "before": before, "after": after}]}
        self.assertEqual(evaluate(policy, packet)["verdict"], "INCONCLUSIVE")

    def test_read_only_observer_after_writer_disconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp, "support.db")
            writer = sqlite3.connect(db)
            writer.execute("create table tickets (tenant text)")
            writer.execute("insert into tickets values ('birch')")
            writer.commit()
            writer.close()
            with open_reader(db) as reader:
                self.assertEqual(read_pair(reader, "select 1 from tickets where tenant=?", ("acme", "birch")),
                                 {"subject": "absent", "control": "present"})
                for sql in ("delete from tickets", "pragma query_only=off", "attach ':memory:' as writable"):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                        reader.execute(sql)
                with self.assertRaises(ValueError):
                    read_pair(reader, "select 1 from tickets where tenant=?", ("acme", "acme"))

    def test_signs_exact_bytes_and_refuses_existing_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp, "receipt.sigstore.json")

            def sign(command, *, check):
                self.assertTrue(check)
                self.assertEqual(command[:4], ["cosign", "sign-blob", "--yes", "--bundle"])
                self.assertEqual(Path(command[-1]).read_bytes(), RECEIPT.read_bytes())
                Path(command[4]).write_text("synthetic bundle")

            args = ["sign-receipt", "--receipt", str(RECEIPT), "--bundle", str(bundle)]
            with patch("subprocess.run", side_effect=sign) as signer, redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 2)
            signer.assert_called_once()
            self.assertEqual(bundle.read_text(), "synthetic bundle")

    def test_verification_requires_local_trust_and_exact_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle, root = Path(tmp, "bundle.json"), Path(tmp, "root.json")
            bundle.write_bytes(b"synthetic bundle")
            root.write_bytes(b"independent trust policy")
            args = ["verify-signature", "--receipt", str(RECEIPT), "--bundle", str(bundle),
                    "--trusted-root", str(root), "--issuer", "https://issuer.example",
                    "--identity", "reviewer@example.com"]
            def verify(command, *, check):
                self.assertTrue(check)
                self.assertEqual(command[:3], ["cosign", "verify-blob", "--bundle"])
                self.assertEqual(Path(command[3]).read_bytes(), bundle.read_bytes())
                self.assertEqual(command[4], "--trusted-root")
                self.assertEqual(Path(command[5]).read_bytes(), root.read_bytes())
                self.assertEqual(command[6:-1], ["--certificate-identity", "reviewer@example.com",
                                                 "--certificate-oidc-issuer", "https://issuer.example"])
                self.assertEqual(Path(command[-1]).read_bytes(), RECEIPT.read_bytes())

            with patch("subprocess.run", side_effect=verify) as runner:
                self.assertEqual(main(args), 0)
                runner.assert_called_once()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes require POSIX")
    def test_nonregular_sigstore_inputs_stop_before_invoking_cosign(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo, normal = Path(tmp, "pipe"), Path(tmp, "regular")
            os.mkfifo(fifo)
            normal.write_text("{}")
            with patch("subprocess.run") as runner, redirect_stderr(io.StringIO()):
                for bundle, root in ((fifo, normal), (normal, fifo)):
                    self.assertEqual(main(["verify-signature", "--receipt", str(RECEIPT),
                                           "--bundle", str(bundle), "--trusted-root", str(root),
                                           "--issuer", "https://issuer.example", "--identity", "reviewer@example.com"]), 2)
                runner.assert_not_called()

    def test_oversized_sigstore_inputs_stop_before_invoking_cosign(self):
        with tempfile.TemporaryDirectory() as tmp:
            large, normal = Path(tmp, "large"), Path(tmp, "regular")
            large.write_bytes(b"0123456789")
            normal.write_text("{}")
            with (patch("setrya.offboarding.signature.MAX_SIGSTORE_BYTES", 8),
                  patch("subprocess.run") as runner, redirect_stderr(io.StringIO())):
                for bundle, root in ((large, normal), (normal, large)):
                    self.assertEqual(main(["verify-signature", "--receipt", str(RECEIPT), "--bundle", str(bundle),
                                           "--trusted-root", str(root), "--issuer", "https://issuer.example",
                                           "--identity", "reviewer@example.com"]), 2)
                runner.assert_not_called()

    def test_schema_is_required_before_signing_or_verifying(self):
        original = json.loads(RECEIPT.read_bytes())
        minimal = {key: original[key] for key in ("version", "request_sha256", "evidence_sha256",
                                                   "technical_verdict", "review_status")}
        invalid = [minimal]
        for path, replacement in (
            (("version",), True),
            (("request_sha256",), "not-a-digest"),
            (("observed_at",), "2026-02-30T09:00:00Z"),
            (("items", 0, "document_sha256"), "a" * 63),
            (("items", 0, "status"), "SCHEDULED"),
            (("items", 0, "outcome"), "reported_retained"),
            (("items", 1, "expires_on"), "2026-02-30"),
            (("probes", 0, "reasons"), [False]),
            (("probes", 0, "extra"), "unexpected"),
            (("probes",), []),
        ):
            value = copy.deepcopy(original)
            target = value
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            invalid.append(value)
        missing_nested = copy.deepcopy(original)
        del missing_nested["items"][0]["issuer"]
        invalid.append(missing_nested)

        with tempfile.TemporaryDirectory() as tmp, patch("subprocess.run") as runner:
            path, bundle = Path(tmp, "receipt.json"), Path(tmp, "bundle.json")
            flags = ["--receipt", str(path), "--bundle", str(bundle)]
            verify = ["verify-signature", *flags, "--trusted-root", str(Path(tmp, "root.json")),
                      "--identity", "user@example.com", "--issuer", "https://example.com"]
            for case, receipt in enumerate(invalid):
                path.write_text(json.dumps(receipt))
                for args in (["sign-receipt", *flags], verify):
                    with self.subTest(case=case, command=args[0]), redirect_stderr(io.StringIO()) as stderr:
                        self.assertEqual(main(args), 2)
                        self.assertIn("v1 schema", stderr.getvalue())
            runner.assert_not_called()
            self.assertFalse(bundle.exists())

    def test_archived_receipt_has_frozen_schema_fields(self):
        schema = json.loads((ROOT / "setrya/offboarding/receipt-v1.schema.json").read_bytes())
        legacy = json.loads(RECEIPT.read_bytes())
        self.assertEqual(set(schema["required"]), legacy.keys())
        for value, definition in [(legacy["probes"][0], "probe"), (legacy["items"][0], "statement"),
                                  (legacy["items"][1], "schedule")]:
            self.assertEqual(set(schema["$defs"][definition]["required"]), value.keys())
