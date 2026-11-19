import copy
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from setrya.offboarding.cli import main
from setrya.offboarding.observer import open_reader, read_pair

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "tests/fixtures/offboarding-receipt-axiom-v0.3.2.json"


class TrustTests(unittest.TestCase):
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
            args = ["verify-signature", "--receipt", str(RECEIPT), "--bundle", str(bundle),
                    "--trusted-root", str(root), "--issuer", "https://issuer.example",
                    "--identity", "reviewer@example.com"]
            with patch("subprocess.run") as runner:
                self.assertEqual(main(args), 0)
                command = runner.call_args.args[0]
                self.assertEqual(command[:-1], ["cosign", "verify-blob", "--bundle", str(bundle.resolve()),
                                                 "--trusted-root", str(root.resolve()),
                                                 "--certificate-identity", "reviewer@example.com",
                                                 "--certificate-oidc-issuer", "https://issuer.example"])

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
