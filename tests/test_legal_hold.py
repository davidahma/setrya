"""Exercise SQLite maintenance and independently replay the declared preservation checks."""
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.casework.assessment_comparison import _coverage_changes, _surfaces
from setrya.casework.assessment_details import details, validate_details
from setrya.casework.receipt_summary import receipt_summary
from setrya.cli import main
from setrya.core.inputs import InvalidReport
from setrya.core.receipt_v3 import answer_records, derive
from setrya.erasure import Maintenance, observe_baseline, run_shared
from setrya.examples import legal_hold
from setrya.receipt_format import EVIDENCE
from setrya.receipts import replay


class LegalHoldTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.bundle = self.folder / "receipt"

    def capture(self, case=legal_hold.CASE, *, configure=None):
        with legal_hold.open_fixture() as (subject, app):
            if configure:
                configure(app)
            return run_shared(subject, case=case, application=app, output=self.bundle, mode="deletion")

    def raw(self, path=None):
        return json.loads(((path or self.bundle) / EVIDENCE).read_bytes())

    def test_demo_replays_each_verdict_after_fixture_teardown(self):
        output = self.folder / "demo"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["demo", "legal-hold", "--output", str(output)]), 0)
        for lane, expected in legal_hold.LANES.items():
            with self.subTest(lane=lane), redirect_stdout(StringIO()):
                self.assertEqual(main(["replay", str(output / lane)]), 0 if expected == "PASS" else 1)

        receipts = {lane: replay(output / lane) for lane in legal_hold.LANES}
        guarded = self.raw(output / "guarded")
        before = guarded["baseline"]["stored"][1]["revision"]
        for index, step in enumerate(("consolidate", "summarize")):
            result = receipts["guarded"]["maintenance_checks"][index]
            self.assertEqual(result["step"], step)
            erase, preserve, *_ = result["checks"]
            self.assertEqual((erase["action"], erase["status"], erase["evidence"]),
                             ("delete", "PASS", f"{EVIDENCE}#/maintenance/{index}/stored/0"))
            self.assertEqual((preserve["action"], preserve["status"], preserve["evidence"]),
                             ("preserve", "PASS", f"{EVIDENCE}#/maintenance/{index}/stored/1"))
            self.assertEqual(guarded["maintenance"][index]["stored"][1]["revision"], before)
        self.assertNotEqual(guarded["immediate"]["stored"][2]["revision"],
                            guarded["maintenance"][0]["stored"][2]["revision"])
        self.assertNotEqual(guarded["maintenance"][0]["stored"][3]["revision"],
                            guarded["maintenance"][1]["stored"][3]["revision"])
        target, control = guarded["case"]["questions"]
        answers = answer_records(guarded, "final")
        self.assertEqual([row["target_present"] for row in answers if row["question"] == target], [False] * 3)
        self.assertEqual([row["control_present"] for row in answers if row["question"] == control], [True] * 3)

        recovered = self.raw(output / "restored-hold")
        self.assertEqual(recovered["baseline"]["stored"][1], recovered["final"]["stored"][1])
        kept = [record["checks"][1]["status"] for record in receipts["restored-hold"]["maintenance_checks"]]
        self.assertEqual(kept, ["FAIL", "PASS"])
        self.assertTrue(recovered["final"]["stored"][1]["exists"])
        self.assertFalse(recovered["maintenance"][0]["stored"][1]["exists"])
        self.assertIn("maintenance step consolidate", receipts["restored-hold"]["failures"][0])
        self.assertIn("records/held-packet lost the retained finding after erasure",
                      receipt_summary(output / "lost-hold", audience="reviewer"))
        leaked = self.raw(output / "resurfaced-target")
        self.assertFalse(leaked["final"]["stored"][0]["exists"])
        self.assertTrue(leaked["final"]["stored"][1]["exists"])
        self.assertEqual([row["target_present"] for row in answer_records(leaked, "final")
                          if row["question"] == target], [True] * 3)
        self.assertEqual(receipts["resurfaced-target"]["maintenance_checks"][1]["checks"][3]["status"], "FAIL")

    def test_missing_hook_blocks_erase_and_doctor(self):
        issues = []
        with legal_hold.open_fixture() as (subject, app):
            app.maintain = None
            self.assertFalse(observe_baseline(subject, legal_hold.CASE, app, on_gap=lambda *row: issues.append(row)))
            receipt = run_shared(subject, case=legal_hold.CASE, application=app, output=self.bundle, mode="deletion")
            self.assertTrue(app.inspect(subject, "records", "customer-note").exists)
        self.assertEqual(receipt["technical_verdict"], "NOT TESTED")
        self.assertEqual(issues[0][0], "maintain")
        self.assertIsNone(self.raw()["erased"])
        self.assertEqual(self.raw()["maintenance"], [None, None])

    def test_mismatched_step_acknowledgment_stops_the_schedule(self):
        calls = []

        def override(app):
            def maintain(_, step):
                calls.append(step)
                return Maintenance("unexpected")
            app.maintain = maintain

        self.assertEqual(self.capture(configure=override)["technical_verdict"], "NOT TESTED")
        self.assertEqual(calls, ["consolidate"])
        self.assertEqual(self.raw()["maintenance"], [None, None])
        self.assertEqual(replay(self.bundle)["technical_verdict"], "NOT TESTED")

    def test_missing_preserve_read_at_one_step_prevents_pass(self):
        def override(app):
            inspect = app.inspect
            maintain = app.maintain
            stage = None

            def set_step(subject, step):
                nonlocal stage
                stage = step
                return maintain(subject, step)

            def read(subject, store, key):
                if stage == "consolidate" and key == "held-packet":
                    return None
                return inspect(subject, store, key)

            app.maintain, app.inspect = set_step, read

        receipt = self.capture(configure=override)
        self.assertEqual(receipt["technical_verdict"], "NOT TESTED")
        self.assertEqual(receipt["maintenance_checks"][0]["checks"][1]["status"], "NOT TESTED")
        self.assertIsNone(receipt["maintenance_checks"][0]["checks"][1]["evidence"])

    def test_unchanged_fact_does_not_mask_a_changed_held_revision(self):
        def override(app):
            maintain = app.maintain

            def modify(subject, step):
                completed = maintain(subject, step)
                if step == "consolidate":
                    with app.db:
                        app.db.execute("UPDATE records SET body = body || ' amended' WHERE key = ?", ("held-packet",))
                return completed

            app.maintain = modify

        receipt = self.capture(configure=override)
        self.assertEqual(receipt["technical_verdict"], "FAIL")
        self.assertTrue(self.raw()["maintenance"][0]["stored"][1]["control_present"])
        self.assertIn("consolidate · FAIL · records/held-packet · preserve requirement",
                      receipt_summary(self.bundle, audience="reviewer"))

    def test_maintenance_scope_is_projected_only_when_observed(self):
        receipt = self.capture()
        projected = details(receipt, self.bundle)
        validate_details(projected)
        self.assertIn(["maintenance", legal_hold.CASE.route, legal_hold.CASE.conversation,
                       "1 consolidate", "records", "held-packet", "preserve"],
                      projected["observed"])
        rendered = receipt_summary(self.bundle, audience="reviewer")
        self.assertIn("erased · erasure-evidence.json#/maintenance/0/stored/0", rendered)
        self.assertIn("same revision retained · erasure-evidence.json#/maintenance/1/stored/1", rendered)
        self.assertNotIn("records/held-packet", receipt_summary(self.bundle, audience="customer"))
        self.bundle = self.folder / "plain"
        plain = self.capture(replace(legal_hold.CASE, maintenance_steps=()))
        self.assertNotIn("maintenance_checks", plain)
        self.assertNotIn("maintenance_steps", plain["case"])
        self.assertNotIn("maintenance", self.raw())
        self.assertEqual(plain, replay(self.bundle))

    def test_one_route_cannot_cover_another_routes_maintenance(self):
        first = {"details": details(self.capture(), self.bundle)}
        alternate = replace(legal_hold.CASE, route="/archive/ask", conversation="archive")

        def configure(app):
            read = app.read

            def read_archive(subject, caller, conversation, question):
                answer = read(subject, caller, legal_hold.CASE.conversation, question)
                return replace(answer, conversation=conversation, route=alternate.route)

            app.read = read_archive

        self.bundle = self.folder / "archive-full"
        archive = {"details": details(self.capture(alternate, configure=configure), self.bundle)}
        self.bundle = self.folder / "archive-no-maintenance"
        plain_case = replace(alternate, maintenance_steps=())
        plain = {"details": details(self.capture(plain_case, configure=configure), self.bundle)}
        changes = list(_coverage_changes(_surfaces([first, archive]), _surfaces([first, plain])))
        self.assertEqual(len(changes), 8)
        self.assertTrue(all(not increased and "on /archive/ask in archive" in label
                            for increased, label in changes))

    def test_replay_requires_the_declared_step_order_and_exact_held_key(self):
        self.capture()
        for damage in ("omitted", "swapped", "wrong-key", "missing-prefix"):
            with self.subTest(damage=damage):
                captured = self.raw()
                if damage == "omitted":
                    captured.pop("maintenance")
                elif damage == "swapped":
                    captured["maintenance"].reverse()
                elif damage == "wrong-key":
                    captured["maintenance"][0]["stored"][1]["key"] = "replacement"
                else:
                    captured["maintenance"][0] = None
                with self.assertRaises(InvalidReport):
                    derive(captured, "0" * 64)
