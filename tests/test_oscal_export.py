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

from setrya.casework import exchange, oscal, profiles
from setrya.casework.handoff import verify_handoff
from setrya.cli import main
from setrya.core.inputs import InvalidReport
from setrya.erasure import run_shared
from setrya.examples import legal_hold, memory_files
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

    def issue(self, *, guarded=True, missing_answers=False, missing_stores=False, missing_scans=False,
              keyless=False, authorize_erase=None):
        original, bundle = self.root / "receipt", self.root / "exchange"
        case = replace(CASE, controls=(CONTROL,), scan_stores=("notes",) if missing_scans else CASE.scan_stores)
        with open_fixture(shared=True, guarded=guarded, record_writes=True) as (subject, app, items):
            if missing_answers or missing_stores or missing_scans:
                original_erase = app.erase
                erased = False

                def erase(*args):
                    nonlocal erased
                    result = original_erase(*args)
                    erased = True
                    return result

                app.erase = erase
                if missing_answers:
                    original_read = app.read
                    app.read = lambda *args: None if erased else original_read(*args)
                if missing_stores:
                    original_inspect = app.inspect
                    app.inspect = lambda *args: None if erased else original_inspect(*args)
                if missing_scans:
                    def scan(*_):
                        if erased:
                            raise RuntimeError("The test scanner became unavailable.")
                        return {"customer-note"}

                    app.scan = scan
            run_shared(subject, case=case, application=app, mode="write-back",
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
        for observation in result["observations"]:
            self.assertEqual(self.props(observation)["technical-verdict"], "PASS")
            self.assertEqual(self.props(observation)["review-status"], "OPEN_ITEMS")
        self.assertEqual(self.props(erasure)["control-reference-source"], "receipt-bound")
        self.assertEqual(self.props(erasure)["passing-checks"], "1")
        self.assertEqual(self.props(preserve)["control-reference-source"], "reviewer-supplied")
        self.assertEqual(self.props(preserve)["check-outcome"], "PASS")
        self.assertEqual(self.props(preserve)["passing-checks"], "2")
        self.assertEqual(self.props(open_item)["open-item-owner"], "platform-team")
        self.assertTrue(open_item["relevant-evidence"][0]["href"].startswith("exchange/erasure-receipt.json#"))
        self.assertNotIn("subject", self.output.read_text(encoding="utf-8"))

    def test_failed_technical_verdict_is_preserved(self):
        document = self.export(self.issue(guarded=False))
        result, = document["results"]
        self.assertEqual(self.props(result)["technical-verdict"], "FAIL")
        self.assertEqual(self.props(result)["review-status"], "RESIDUAL_FOUND")

    def test_public_draft_records_version_and_digest_without_a_control_finding(self):
        draft = SAMPLE.parents[1] / "control-mappings" / "2026-11-26-draft.1.json"
        shutil.copyfile(draft, self.mapping)
        plan = json.loads(self.plan.read_bytes())
        selection, = plan["assessment-plan"]["reviewed-controls"]["control-selections"]
        selection["include-controls"] = [{"control-id": "gdpr-17.1"}]
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result, = self.export(self.issue())["results"]
        props = self.props(result)
        self.assertEqual(props["mapping-version"], "2026-11-26-draft.1")
        self.assertEqual(props["mapping-sha256"], sha256(draft.read_bytes()).hexdigest())
        scope, = result["reviewed-controls"]["control-selections"]
        self.assertEqual(scope["include-controls"], [{"control-id": "gdpr-17.1"}])
        self.assertTrue(any(self.props(row).get("control-reference") == "Article 17(1)"
                            for row in result["observations"]))
        self.assertNotIn("findings", result)
        self.assertNotIn("attestations", result)

    def test_second_draft_maps_additional_controls_with_first_digest_preserved(self):
        folder = SAMPLE.parents[1] / "control-mappings"
        first = folder / "2026-11-26-draft.1.json"
        draft = folder / "2026-11-26-draft.2.json"
        self.assertEqual(sha256(first.read_bytes()).hexdigest(),
                         "39ddf5daf7e9214d324ae4895b63a8093294d2d8d842da57bcc8a50a4fd93dda")
        shutil.copyfile(draft, self.mapping)
        plan = json.loads(self.plan.read_bytes())
        selection, = plan["assessment-plan"]["reviewed-controls"]["control-selections"]
        selection["include-controls"] = [
            {"control-id": "soc2-p4.3"}, {"control-id": "iso27701-a.1.4.9"}, {"control-id": "hipaa-164.310.d.2.i"},
        ]
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result, = self.export(self.issue(), expect_mapping_sha256=sha256(draft.read_bytes()).hexdigest())["results"]
        checked = {props["control-reference"]: props["check-outcome"] for row in result["observations"]
                   if "control-reference" in (props := self.props(row))}
        self.assertEqual(checked, {"P4.3": "PASS", "A.1.4.9": "PASS", "164.310(d)(2)(i)": "PASS"})
        self.assertNotIn("findings", result)

    def test_export_links_verified_maintenance_observations(self):
        original, bundle = self.root / "hold", self.root / "exchange"
        with legal_hold.open_fixture("restored-hold") as (subject, app):
            receipt = run_shared(subject, case=legal_hold.CASE, application=app, mode="deletion", output=original)
        self.assertEqual(receipt["technical_verdict"], "FAIL")
        exchange.pack(original, output=bundle, system="incident-register", version="1", synthetic=True, key=self.key)
        result, = self.export(bundle)["results"]
        preserve = next(row for row in result["observations"] if self.props(row).get("check") == "artifact-preserve")
        self.assertEqual(self.props(result)["technical-verdict"], "FAIL")
        props = self.props(preserve)
        self.assertEqual(props["technical-verdict"], "FAIL")
        self.assertEqual(props["check-outcome"], "FAIL")
        self.assertEqual(props["recorded-maintenance-reads"], "2")
        self.assertEqual(props["passing-checks"], "3")
        self.assertEqual(props["failing-checks"], "1")
        self.assertEqual(props["untested-checks"], "0")
        self.assertEqual(props["passing-maintenance-checks"], "1")
        self.assertEqual(props["failing-maintenance-checks"], "1")
        self.assertTrue(preserve["description"].startswith("FAIL"))
        self.assertIn("exchange/erasure-receipt.json#/maintenance_checks",
                      [item["href"] for item in preserve["relevant-evidence"]])

    def test_missing_maintenance_operation_stays_untested_in_observation(self):
        original, bundle = self.root / "hold", self.root / "exchange"
        with legal_hold.open_fixture("guarded") as (subject, app):
            app.maintain = lambda *_: None
            run_shared(subject, case=legal_hold.CASE, application=app, mode="deletion", output=original)
        exchange.pack(original, output=bundle, system="incident-register", version="1", synthetic=True, key=self.key)
        result, = self.export(bundle)["results"]
        props = next(self.props(row) for row in result["observations"]
                     if self.props(row).get("check") == "artifact-preserve")
        self.assertEqual(props["check-outcome"], "NOT TESTED")
        self.assertEqual(props["passing-checks"], "2")
        self.assertEqual(props["untested-checks"], "2")
        self.assertEqual(props["untested-maintenance-checks"], "2")

    def test_failed_endpoint_and_worker_outcomes_remain_visible(self):
        mapping = json.loads(self.mapping.read_bytes())
        prototype = mapping["supports"][0]
        mapping["supports"] += [{**prototype, "check": check} for check in ("endpoint-read", "write-back")]
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        result, = self.export(self.issue(guarded=False))["results"]
        checks = {props["check"]: props for row in result["observations"] if "check" in (props := self.props(row))}
        for check in ("endpoint-read", "write-back"):
            with self.subTest(check=check):
                self.assertEqual(checks[check]["check-outcome"], "FAIL")
                self.assertGreater(int(checks[check]["failing-checks"]), 0)
                self.assertEqual(checks[check]["technical-verdict"], "FAIL")

    @unittest.skipUnless(shutil.which("git"), "Git stores the native scanner fixture")
    def test_canary_scan_reports_failed_while_other_scans_pass(self):
        original, bundle = self.root / "memory", self.root / "exchange"
        with memory_files.open_fixture(shutil.which("git"), guarded=False) as (subject, app, items):
            run_shared(subject, case=app.case(), application=app, external_items=items, output=original,
                       mode="write-back", journal=app.journal, cancel_hold=app.cancel_and_drain)
        exchange.pack(original, output=bundle, system="memory-review", version="1", synthetic=True, key=self.key)
        mapping = json.loads(self.mapping.read_bytes())
        mapping["supports"][0]["check"] = "store-scan"
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        result, = self.export(bundle)["results"]
        props = next(self.props(row) for row in result["observations"] if self.props(row).get("check") == "store-scan")
        self.assertEqual(props["check-outcome"], "FAIL")
        self.assertGreater(int(props["passing-checks"]), 0)
        self.assertGreater(int(props["failing-checks"]), 0)

    def test_provider_api_outcome_excludes_uninspected_internals(self):
        original, bundle = self.root / "vendor", self.root / "exchange"
        original.mkdir()
        baseline, later = "2026-11-25T12:00:00+00:00", "2026-11-26T12:00:00+00:00"
        profiles.issue(profiles.VENDOR, {
            "target": "openai-vector-files", "observed_at": later, "versions": {"api": "v1"}, "error": None,
            "readback": {"baseline_at": baseline, "scope_sha256": "1" * 64},
            "observations": {"baseline": {"vector-attachment": {"target": True, "retained": True}},
                             "after_delete": {"vector-attachment": {"target": False, "retained": True}}},
        }, original)
        exchange.pack(original, output=bundle, system="provider-demo", version="1", key=self.key)
        mapping = json.loads(self.mapping.read_bytes())
        mapping["supports"][0]["check"] = "provider-readback"
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        result, = self.export(bundle)["results"]
        check, *open_items = result["observations"]
        props = self.props(check)
        self.assertEqual(props["check"], "provider-readback")
        self.assertEqual(props["readback-surface"], "vector-attachment")
        self.assertEqual(props["check-outcome"], "PASS")
        self.assertEqual(props["passing-checks"], "1")
        self.assertEqual(props["untested-checks"], "0")
        self.assertTrue(any(self.props(row)["open-item-surface"] == "provider-internals" for row in open_items))

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
        endpoint, = [self.props(row) for row in result["observations"]
                     if self.props(row).get("check") == "endpoint-read"]
        self.assertEqual(endpoint["check-outcome"], "NOT TESTED")
        self.assertEqual(endpoint["recorded-phases"], "0")
        self.assertGreater(int(endpoint["untested-checks"]), 0)
        self.assertEqual(endpoint["mapped-control"], "demo-retain")
        self.assertNotIn("supports", endpoint)

    def test_all_missing_post_erase_store_and_scan_reads_stay_visible(self):
        bundle = self.issue(missing_stores=True, missing_scans=True)
        mapping = json.loads(self.mapping.read_bytes())
        mapping["supports"].append({**mapping["supports"][1], "check": "store-scan"})
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        result, = self.export(bundle)["results"]
        controls, = result["reviewed-controls"]["control-selections"]
        self.assertEqual(controls["include-controls"], [{"control-id": "demo-erase"}])
        checks = {props["check"]: props for row in result["observations"]
                  if "check" in (props := self.props(row))}
        for name, recorded in (("artifact-preserve", "observed-artifacts"), ("store-scan", "recorded-phases")):
            with self.subTest(check=name):
                props = checks[name]
                self.assertEqual(props["check-outcome"], "NOT TESTED")
                self.assertEqual(props[recorded], "0")
                self.assertEqual(props["untested-checks"], "2")
                self.assertEqual(props["mapped-control"], "demo-retain")
                self.assertNotIn("supports", props)

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
