"""Verify the draft v1 exchange against actual replayed v3 cases."""

import json
import unittest
from contextlib import redirect_stdout
from hashlib import sha256
from importlib.resources import files
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from setrya.casework import exchange
from setrya.casework.profiles import VENDOR, issue, snapshot
from setrya.casework.receipt_summary import receipt_summary
from setrya.cli import main
from setrya.core.inputs import InvalidReport


class ReceiptExchangeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.originals = self.root / "workspace"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["demo", "--output", str(self.originals)]), 0)
        self.key = self.root / "separately-provisioned.key"

        def sign(raw, output, *, key):
            output.write_bytes(sha256(raw).digest())

        def verify(raw, signature, *, key, **policy):
            if signature != sha256(raw).digest():
                raise InvalidReport("Signature differs from the exact signed bytes.")

        self.enterContext(patch.object(exchange.signing, "sign_bytes", side_effect=sign))
        self.enterContext(patch.object(exchange.signing, "verify_bytes", side_effect=verify))

    def pack(self, lane="guarded"):
        directory = self.root / lane
        manifest = exchange.pack(self.originals / lane, output=directory, system="support-app", version="1.2",
                                 key=self.key, synthetic=True)
        return directory, manifest

    def test_signed_verdict_and_open_items_are_replayed(self):
        for lane, verdict, status, code in (("broken", "FAIL", "RESIDUAL_FOUND", 1),
                                             ("guarded", "PASS", "OPEN_ITEMS", 0)):
            with self.subTest(lane=lane):
                bundle, manifest = self.pack(lane)
                self.assertEqual(exchange.verify(bundle, key=self.key), manifest)
                self.assertEqual(manifest["technical_verdict"], verdict)
                self.assertEqual(manifest["review_status"], status)
                self.assertIsNone(manifest["ticket"])
                self.assertEqual(manifest["open_items"][0]["owner"], "platform-team")
                self.assertEqual(manifest["open_items"][0]["evidence"], "erasure-receipt.json#/open_items/0")
                self.assertNotIn("subject", manifest)
                args = ["receipt", "verify-exchange", str(bundle), "--key", str(self.key)]
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(args), code)
                    self.assertEqual(main([*args, "--case-review"]), 3 if code == 0 else code)
                self.assertIn(f"Verified exchange · {verdict} · {status}", output.getvalue())
                try:
                    import jsonschema
                except ImportError:
                    continue
                schema = files(exchange.__package__).joinpath("receipt-exchange-v1.schema.json").read_text()
                jsonschema.Draft202012Validator(json.loads(schema), format_checker=jsonschema.FormatChecker()).validate(
                    manifest
                )

    def test_authenticated_inconsistent_claim_is_rejected(self):
        bundle, manifest = self.pack("broken")
        manifest["technical_verdict"] = "PASS"
        raw = json.dumps(manifest).encode()
        (bundle / exchange.MANIFEST).write_bytes(raw)
        (bundle / exchange.SIGNATURE).write_bytes(sha256(raw).digest())
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[VERDICT_MISMATCH\].*differs from the evidence"):
            exchange.verify(bundle, key=self.key)

    def test_vendor_exchange_replays_the_original_surface_findings(self):
        source = self.root / "vendor-source"
        source.mkdir()
        stages = {
            phase: {surface: {"target": phase == "baseline" or surface == "history", "retained": True}
                    for surface in ("store", "index", "history")}
            for phase in ("baseline", "after_delete", "after_retry")
        }
        report = issue(VENDOR, {
            "target": "mem0", "observed_at": "2026-11-25T10:00:00+00:00",
            "versions": {"mem0ai": "1", "qdrant-client": "1"}, "observations": stages, "error": None,
            "execution": {"operator": "fixture-ci", "run_id": "0b2f88b1-47ad-4723-a8fe-0eab52bc995e",
                          "setrya_version": "1.12.0"},
        }, source)
        bundle = self.root / "vendor-exchange"
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["receipt", "pack", str(source), "--output", str(bundle),
                                   "--system", "mem0", "--system-version", "1", "--key", str(self.key)]), 0)
            self.assertEqual(main(["receipt", "verify-exchange", str(bundle), "--key", str(self.key)]), 1)
        self.assertIn("FAIL · history", output.getvalue())
        self.assertIn("recorded operator fixture-ci", output.getvalue())
        self.assertIn("Authentication applies to the exchange issuer.", output.getvalue())
        manifest = exchange.verify(bundle, key=self.key)
        self.assertEqual(manifest["evidence_kind"], "vendor-attestation")
        self.assertEqual(manifest["scope"], "vendor")
        self.assertIsNone(manifest["ticket"])
        self.assertEqual(manifest["vendor"]["target"], "mem0")
        self.assertEqual(manifest["vendor"]["execution"], report["execution"])
        for filename in (exchange.RECEIPT, exchange.OBSERVED_EVIDENCE):
            self.assertEqual(snapshot(bundle / filename), snapshot(source / filename))
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[APP_COMMIT_MISMATCH\]"):
            exchange.verify(bundle, key=self.key, expected_app_commit="a" * 40)
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[TICKET_ID_MISMATCH\]"):
            exchange.verify(bundle, key=self.key, expect_ticket="TICKET-1")
        manifest["vendor"]["checks"][2]["status"] = "PASS"
        raw = json.dumps(manifest).encode()
        (bundle / exchange.MANIFEST).write_bytes(raw)
        (bundle / exchange.SIGNATURE).write_bytes(sha256(raw).digest())
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[CLAIMS_MISMATCH\]"):
            exchange.verify(bundle, key=self.key)

    def test_existing_api_readback_report_can_be_packed_without_provider_access(self):
        source = self.root / "readback-source"
        source.mkdir()
        issue(VENDOR, {
            "target": "mem0-hosted-history", "observed_at": "2026-11-25T10:00:00+00:00",
            "versions": {"api": "v1"}, "error": None,
            "observations": {
                "baseline": {"memory-history-content": {"target": True, "retained": True}},
                "after_delete": {"memory-history-content": {"target": False, "retained": True}},
            },
            "readback": {"baseline_at": "2026-11-25T09:00:00+00:00", "scope_sha256": "a" * 64},
        }, source)
        bundle = self.root / "readback-exchange"
        manifest = exchange.pack(source, output=bundle, system="mem0", version="v1", key=self.key)
        self.assertEqual(manifest["evidence_kind"], "provider-readback")
        self.assertEqual(manifest["vendor"]["kind"], "api-readback")
        self.assertEqual(manifest["vendor"]["scope_sha256"], "a" * 64)
        self.assertEqual(manifest["observation"]["first_checked_at"], "2026-11-25T09:00:00+00:00")
        self.assertNotIn("execution", manifest["vendor"])
        self.assertIn({"surface": "provider-internals", "status": "NOT TESTED"}, manifest["vendor"]["checks"])
        self.assertEqual(manifest["technical_verdict"], "PASS")
        self.assertEqual(manifest["review_status"], "OPEN_ITEMS")
        self.assertEqual(exchange.verify(bundle, key=self.key), manifest)
        self.assertIn("Provider API readback", receipt_summary(bundle, audience="reviewer"))
        self.assertIn("declared API surface", receipt_summary(bundle, audience="customer"))
        try:
            import jsonschema
        except ImportError:
            jsonschema = None
        if jsonschema:
            schema = files(exchange.__package__).joinpath("receipt-exchange-v1.schema.json").read_text()
            jsonschema.Draft202012Validator(json.loads(schema), format_checker=jsonschema.FormatChecker()).validate(manifest)
        legacy = {**manifest, "evidence_kind": "vendor-attestation",
                  "observation": {"last_checked_at": manifest["observation"]["last_checked_at"]},
                  "vendor": {name: value for name, value in manifest["vendor"].items() if name != "scope_sha256"}}
        raw = json.dumps(legacy).encode()
        (bundle / exchange.MANIFEST).write_bytes(raw)
        (bundle / exchange.SIGNATURE).write_bytes(sha256(raw).digest())
        self.assertEqual(exchange.verify(bundle, key=self.key), legacy)

    def test_file_bytes_and_signed_manifest_are_both_bound(self):
        bundle, _ = self.pack()
        evidence = bundle / exchange.DRILL_EVIDENCE
        evidence.write_bytes(evidence.read_bytes() + b"\n")
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[EVIDENCE_DIGEST_MISMATCH\].*file digests differ"):
            exchange.verify(bundle, key=self.key)
        manifest = bundle / exchange.MANIFEST
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[WRONG_SIGNER\].*could not be authenticated"):
            exchange.verify(bundle, key=self.key)

    def test_bounds_and_trust_are_explicit(self):
        bundle, _ = self.pack()
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[WRONG_SIGNER\].*signer policy"):
            exchange.verify(bundle)
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[TICKET_ID_MISMATCH\].*independently supplied ticket"):
            exchange.verify(bundle, key=self.key, expect_ticket="TICKET-1")
        extra = bundle / "approved-mapping.toml"
        extra.touch()
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[UNEXPECTED_FILES\].*Expected exactly"):
            exchange.verify(bundle, key=self.key)
        extra.unlink()
        manifest = bundle / exchange.MANIFEST
        renamed = manifest.rename(self.root / "outside.json")
        manifest.symlink_to(renamed)
        with self.assertRaisesRegex(exchange.ExchangeError, r"\[UNEXPECTED_FILES\].*regular files"):
            exchange.verify(bundle, key=self.key)

    def test_pack_is_atomic_and_keeps_original_receipt(self):
        target = self.root / "target"
        source = self.originals / "guarded" / exchange.RECEIPT
        before = snapshot(source)
        kwargs = {"output": target, "system": "support-app", "version": "1", "key": self.key}
        with self.assertRaisesRegex(InvalidReport, "synthetic data"):
            exchange.pack(source.parent, **kwargs)
        with (
            patch.object(exchange.signing, "sign_bytes", side_effect=InvalidReport("Signing unavailable.")),
            self.assertRaisesRegex(InvalidReport, "Signing unavailable"),
        ):
            exchange.pack(source.parent, **kwargs, synthetic=True)
        self.assertFalse(target.exists())
        self.assertEqual(snapshot(source), before)
