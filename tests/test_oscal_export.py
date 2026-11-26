"""Exercise the OSCAL export from replayed, authenticated receipt exchanges."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.casework import exchange, oscal
from setrya.casework.handoff import verify_handoff
from setrya.cli import main
from setrya.core.inputs import InvalidReport
from setrya.erasure import run_shared
from setrya.examples.workspace import CASE, open_fixture


SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "examples" / "governance-handoff"
CONTROL = {"framework": "Support governance", "reference": "ERASURE-07"}
PUBLIC_KEY = b"-----BEGIN PUBLIC KEY-----\nZm9v\n-----END PUBLIC KEY-----\n"


class OscalExportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.plan = self.root / "plan.json"
        self.mapping = self.root / "mapping.json"
        shutil.copyfile(SAMPLE / "oscal-assessment-plan.json", self.plan)
        shutil.copyfile(SAMPLE / "oscal-control-mapping.json", self.mapping)
        self.key = self.root / "trusted.pub"
        self.key.write_bytes(PUBLIC_KEY)
        self.output = self.root / "result.json"

        def sign(raw, output, *, key):
            output.write_bytes(sha256(raw).digest())

        def verify(raw, signature, *, key, **policy):
            if key is not None:
                self.assertNotEqual(key, self.key)
                require_trust = key.read_bytes() == PUBLIC_KEY
            else:
                require_trust = policy["identity"] == "https://example.invalid/workflows/issuer"
            if not require_trust or signature != sha256(raw).digest():
                raise InvalidReport("Signature or issuer differs.")

        self.enterContext(patch.object(exchange.signing, "sign_bytes", side_effect=sign))
        self.enterContext(patch.object(exchange.signing, "verify_bytes", side_effect=verify))

    def issue(self, *, guarded=True, missing_answers=False, keyless=False, authorize_erase=None):
        original, bundle = self.root / "receipt", self.root / "exchange"
        with open_fixture(shared=True, guarded=guarded, record_writes=True) as (subject, app, items):
            if missing_answers:
                original_read, original_erase = app.read, app.erase
                erased = False

                def erase(*args):
                    nonlocal erased
                    result = original_erase(*args)
                    erased = True
                    return result

                def read(*args):
                    return None if erased else original_read(*args)

                app.erase, app.read = erase, read
            run_shared(subject, case=replace(CASE, controls=(CONTROL,)), application=app, mode="write-back",
                       output=original, external_items=items, journal=app.journal, cancel_hold=app.cancel_and_drain,
                       authorize_erase=authorize_erase)
        exchange.pack(original, output=bundle, system="fictional-support", version="1",
                      synthetic=True, key=None if keyless else self.key)
        return bundle

    def export(self, bundle, **policy):
        review = {"key": self.key, **policy}
        oscal.export_results(bundle, assessment_plan=self.plan, mapping=self.mapping, output=self.output, **review)
        return json.loads(self.output.read_bytes())["assessment-results"]

    @staticmethod
    def props(element):
        return {item["name"]: item["value"] for item in element["props"]}

    def test_cli_exports_verified_signer_evidence_and_open_item(self):
        bundle = self.issue()
        args = ["receipt", "export-oscal", str(bundle), "--key", str(self.key), "--assessment-plan", str(self.plan),
                "--mapping", str(self.mapping), "--ticket", "GOV-204", "--output", str(self.output)]
        with redirect_stdout(StringIO()):
            self.assertEqual(main(args), 0)
        report = json.loads(self.output.read_bytes())["assessment-results"]
        self.assertEqual(report["import-ap"], {"href": "plan.json"})
        self.assertEqual(report["metadata"]["oscal-version"], "1.2.0")
        result, = report["results"]
        props = self.props(result)
        self.assertEqual(props["exchange-signer-sha256"], sha256(b"foo").hexdigest())
        self.assertEqual(props["assessment-plan-sha256"], sha256(self.plan.read_bytes()).hexdigest())
        self.assertEqual(props["mapping-sha256"], sha256(self.mapping.read_bytes()).hexdigest())
        self.assertEqual(props["receipt-sha256"], sha256((bundle / exchange.RECEIPT).read_bytes()).hexdigest())
        self.assertEqual(props["technical-verdict"], "PASS")
        self.assertEqual(props["review-status"], "OPEN_ITEMS")
        self.assertEqual(props["governance-ticket"], "GOV-204")
        self.assertNotIn("findings", result)
        self.assertNotIn("attestations", result)
        selections, = result["reviewed-controls"]["control-selections"]
        self.assertEqual(selections["include-controls"], [{"control-id": "demo-erase"}, {"control-id": "demo-retain"}])
        erasure, preserve, open_item = result["observations"]
        self.assertEqual(self.props(erasure)["control-reference-source"], "receipt-bound")
        self.assertEqual(self.props(preserve)["control-reference-source"], "reviewer-supplied")
        self.assertEqual(self.props(open_item)["open-item-owner"], "platform-team")
        self.assertTrue(open_item["relevant-evidence"][0]["href"].startswith("exchange/erasure-receipt.json#"))
        self.assertNotIn("subject", self.output.read_text(encoding="utf-8"))

    def test_failed_technical_verdict_is_preserved(self):
        document = self.export(self.issue(guarded=False))
        result, = document["results"]
        self.assertEqual(self.props(result)["technical-verdict"], "FAIL")
        self.assertEqual(self.props(result)["review-status"], "RESIDUAL_FOUND")

    def test_grouped_missing_endpoint_answers_do_not_create_support(self):
        bundle = self.issue(missing_answers=True)
        mapping = json.loads(self.mapping.read_bytes())
        mapping["supports"][1]["check"] = "endpoint-read"
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        document = self.export(bundle)
        result, = document["results"]
        self.assertEqual(self.props(result)["technical-verdict"], "NOT TESTED")
        controls, = result["reviewed-controls"]["control-selections"]
        self.assertEqual(controls["include-controls"], [{"control-id": "demo-erase"}])

    def test_final_read_after_skipped_erase_is_not_post_erasure_support(self):
        report = self.export(self.issue(authorize_erase=lambda *_: False))
        result, = report["results"]
        self.assertEqual(self.props(result)["technical-verdict"], "NOT TESTED")
        controls, = result["reviewed-controls"]["control-selections"]
        self.assertEqual(controls["include-controls"], [{"control-id": "demo-erase"}])
        checks = [item["value"] for observation in result["observations"] for item in observation["props"]
                  if item["name"] == "check"]
        self.assertEqual(checks, ["synthetic-erasure"])

    def test_mismatched_signer_digest_and_completed_ticket_create_no_output(self):
        bundle = self.issue()
        wrong_key = self.root / "wrong.pub"
        wrong_key.write_bytes(PUBLIC_KEY.replace(b"Zm9v", b"YmFy"))
        for policy, detail in (
            ({"key": wrong_key}, "WRONG_SIGNER"),
            ({"expect_plan_sha256": "a" * 64}, "Assessment Plan differs"),
            ({"expect_mapping_sha256": "a" * 64}, "Control mapping differs"),
            ({"expect_ticket": "REQ-9"}, "TICKET_ID_MISMATCH"),
        ):
            with self.subTest(policy=policy), self.assertRaisesRegex(InvalidReport, detail):
                self.export(bundle, **policy)
            self.assertFalse(self.output.exists())

    def test_assessment_plan_exclusion_and_mapping_validation(self):
        bundle = self.issue()
        plan = json.loads(self.plan.read_bytes())
        select, = plan["assessment-plan"]["reviewed-controls"]["control-selections"]
        select["exclude-controls"] = select["include-controls"]
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(InvalidReport, "No mapping supports"):
            self.export(bundle)
        self.assertFalse(self.output.exists())
        self.mapping.write_text('{"format":"setrya.control-mapping.v1","version":"1","supports":[]}', encoding="utf-8")
        with self.assertRaisesRegex(InvalidReport, "one to 128 supporting mapping"):
            self.export(bundle)
        self.assertFalse(self.output.exists())

    def test_assessment_plan_needs_complete_metadata(self):
        sample = self.plan.read_bytes()
        bundle = self.issue()
        for name, value in (("title", ""), ("version", None), ("last-modified", "2026-11-26T12:00:00")):
            plan = json.loads(sample)
            metadata = plan["assessment-plan"]["metadata"]
            if value is None:
                metadata.pop(name)
            else:
                metadata[name] = value
            self.plan.write_text(json.dumps(plan), encoding="utf-8")
            with self.subTest(name=name), self.assertRaisesRegex(InvalidReport, "Assessment Plan"):
                self.export(bundle)
            self.assertFalse(self.output.exists())

    def test_control_id_must_be_an_oscal_token(self):
        bundle = self.issue()
        mapping_bytes, plan_bytes = self.mapping.read_bytes(), self.plan.read_bytes()
        for bad_id in (b"1-delete", b"demo:delete"):
            for path, original in ((self.mapping, mapping_bytes), (self.plan, plan_bytes)):
                path.write_bytes(original.replace(b"demo-erase", bad_id))
                with self.subTest(path=path.name, control_id=bad_id), self.assertRaisesRegex(InvalidReport, "control ID"):
                    self.export(bundle)
                self.assertFalse(self.output.exists())
                path.write_bytes(original)

    def test_mapping_applicability_must_be_an_oscal_string(self):
        bundle = self.issue()
        mapping = json.loads(self.mapping.read_bytes())
        mapping["supports"][0]["applicability"] += " "
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        with self.assertRaisesRegex(InvalidReport, "surrounding whitespace"):
            self.export(bundle)
        self.assertFalse(self.output.exists())

    def test_keyless_issuer_and_identity_are_recorded(self):
        bundle = self.issue(keyless=True)
        trust = self.root / "root.json"
        trust.write_text("{}", encoding="utf-8")
        result, = self.export(bundle, key=None, identity="https://example.invalid/workflows/issuer",
                              issuer="https://example.invalid/oidc", trusted_root=trust)["results"]
        self.assertEqual(self.props(result)["exchange-signer-identity"], "https://example.invalid/workflows/issuer")
        self.assertEqual(self.props(result)["exchange-signer-issuer"], "https://example.invalid/oidc")

    def test_cli_preserves_existing_output(self):
        bundle = self.issue()
        self.output.write_text("keep this", encoding="utf-8")
        args = ["receipt", "export-oscal", str(bundle), "--key", str(self.key), "--assessment-plan", str(self.plan),
                "--mapping", str(self.mapping), "--output", str(self.output)]
        with redirect_stderr(StringIO()):
            self.assertEqual(main(args), 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep this")

    def test_export_does_not_add_files_inside_signed_exchange(self):
        bundle = self.issue()
        self.output = bundle / "result.json"
        with self.assertRaisesRegex(InvalidReport, "outside the signed exchange"):
            self.export(bundle)
        self.assertFalse(self.output.exists())
        self.assertEqual(verify_handoff(bundle, key=self.key).manifest["technical_verdict"], "PASS")
