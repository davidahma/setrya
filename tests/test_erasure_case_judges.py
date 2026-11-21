"""Offline checks for the smallest deletion case and the reasons in its evidence."""
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from setrya.erasure.judges import AnswerJudges, Entity, Marker, ModelDecision, ModelJudge, Regex, TextAnswer
from setrya.erasure.subject import Subject
from setrya.erasure.v3 import Artifact, ExternalItem, SharedCase, Stored, observe_baseline, run_shared
from setrya.erasure.v3_receipt import derive, replay
from setrya.offboarding.inputs import InvalidReport


class Workspace:
    def __init__(self, residual=None):
        self.live = True
        self.residual = residual

    def inspect(self, subject, store, key):
        return Stored(key, self.live, self.live, False, "1" if self.live else None)

    def lineage(self, subject):
        return {"roots": [{"store": "notes", "key": "order", "revision": "1"}], "derivations": []}

    def read(self, subject, caller, conversation, question):
        text = "ORCHID-7" if question == "What shipped?" else "Juniper"
        if question == "What shipped?" and not self.live and caller != self.residual:
            text = "Unknown"
        return TextAnswer(caller, conversation, "/ask", question, text)

    def erase(self, subject):
        self.live = False


class CaseJudgesTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.subject = Subject("8" * 64, "test")
        self.case = SharedCase("ticket", "/ask", ("owner",), "What shipped?", "What stayed?",
                               (Artifact("notes", "order", "delete"),), require_lineage=True,
                               judges=AnswerJudges(Marker("ORCHID-7"), Entity("juniper"), capture="excerpt"))

    def test_one_caller_can_erase_one_private_document_without_a_worker(self):
        app = Workspace()
        self.assertTrue(observe_baseline(self.subject, self.case, app))
        self.assertTrue(app.live)
        out = self.root / "one"
        receipt = run_shared(self.subject, case=self.case, application=app, mode="deletion", output=out)
        self.assertEqual((receipt["technical_verdict"], receipt["race"]), ("PASS", "not exercised"))
        self.assertEqual(receipt, replay(out))
        evidence = json.loads((out / "erasure-evidence.json").read_bytes())
        answers = evidence["baseline"]["answers"]
        self.assertEqual([r["judgments"][role]["excerpt"] for r, role in zip(answers, ("target", "control"))],
                         ["ORCHID-7", "Juniper"])
        self.assertNotIn("What shipped?", json.dumps(evidence))
        answers[0]["target_present"] = False
        with self.assertRaises(InvalidReport):
            derive(evidence, "0" * 64)

    def test_doctor_rejects_a_wrong_subject_exhibit_before_touching_the_fixture(self):
        case = replace(self.case, require_lineage=False)
        item = ExternalItem("provider", "call", "engineer", "configuration", "config", subject_ref="4" * 64)
        with self.assertRaisesRegex(InvalidReport, "Wrong subject"):
            observe_baseline(self.subject, case, object(), external_items=(item,))

    def test_replay_binds_exact_marker_decisions_to_the_configured_rule(self):
        for capture in ("excerpt", "digest"):
            out = self.root / capture
            case = replace(self.case, judges=AnswerJudges(Marker("ORCHID-7"), Entity("juniper"), capture=capture))
            run_shared(self.subject, case=case, application=Workspace(), mode="deletion", output=out)
            evidence = json.loads((out / "erasure-evidence.json").read_bytes())
            evidence["case"]["judges"]["target"]["rule_sha256"] = "a" * 64
            with self.assertRaisesRegex(InvalidReport, "Exact marker"):
                derive(evidence, "0" * 64)

    def test_any_declared_callers_residual_fails_and_the_guarded_case_passes(self):
        callers = ("owner", "colleague", "support")
        case = replace(self.case, callers=callers,
                       judges=AnswerJudges(Regex(r"ORCHID-\d+"), Entity("Juniper")))
        for label, residual in (("broken", "support"), ("fixed", None)):
            out = self.root / label
            receipt = run_shared(self.subject, case=case, application=Workspace(residual), mode="deletion", output=out)
            self.assertEqual(receipt["technical_verdict"], "FAIL" if residual else "PASS", receipt)
            self.assertEqual(replay(out), receipt)
            evidence = (out / "erasure-evidence.json").read_bytes()
            self.assertNotIn(b"ORCHID", evidence)
            self.assertNotIn(b"Juniper", evidence)
            self.assertEqual(len(json.loads(evidence)["final"]["answers"]), 6)
            if residual:
                self.assertIn("support recovered the erased finding at final read.", receipt["failures"])
        for invalid in ((), ("one", "one")):
            with self.assertRaises(InvalidReport):
                replace(case, callers=invalid).spec()

    def test_every_workspace_caller_is_checked_even_on_a_preserve_only_case(self):
        class Retained(Workspace):
            def lineage(self, subject):
                return {"roots": [], "derivations": []}

            def inspect(self, subject, store, key):
                return Stored(key, True, False, True, "1")

        callers = tuple(f"reader-{i}" for i in range(53))
        case = replace(self.case, callers=callers, artifacts=(Artifact("notes", "calendar", "preserve"),),
                       require_lineage=False)
        for label, residual in (("guarded", None), ("stale", callers[-1])):
            out = self.root / label
            receipt = run_shared(self.subject, case=case, application=Retained(residual), mode="deletion", output=out)
            self.assertEqual(receipt["technical_verdict"], "FAIL" if residual else "PASS")
            self.assertEqual(receipt, replay(out))
            if residual:
                self.assertIn(f"{residual} recovered the erased finding at final read.", receipt["failures"])

    def test_model_judge_records_decision_and_abstentions_without_network_on_replay(self):
        calls = []

        def judge(body):
            calls.append(body)
            if "ORCHID" in body:
                return ModelDecision(True, "The answer identifies the synthetic shipment.", "ORCHID-7")
            return ModelDecision(None, "Insufficient evidence.")

        case = replace(self.case, judges=AnswerJudges(ModelJudge("test-model/rubric-2", judge), Marker("Juniper")))
        out = self.root / "model"
        receipt = run_shared(self.subject, case=case, application=Workspace(), mode="deletion", output=out)
        self.assertEqual(receipt["technical_verdict"], "NOT TESTED")
        count = len(calls)
        self.assertEqual(replay(out), receipt)
        self.assertEqual(len(calls), count)
        raw = (out / "erasure-evidence.json").read_bytes()
        self.assertNotIn(b"synthetic shipment", raw)
        self.assertIn(b"rationale withheld", raw)
        self.assertIn(b"test-model/rubric-2", raw)
        broken = AnswerJudges(ModelJudge("judge/v1", lambda text: ModelDecision(True, "Saw a match", "absent")),
                              Entity("juniper"))
        with self.assertRaises(InvalidReport):
            broken.inspect("ORCHID-7")
        self.assertFalse(Entity("Ann").evaluate("Hannah").present)
        self.assertTrue(Entity("ANN").evaluate("said Ann.").present)

    def test_a_control_judge_error_cannot_hide_a_separately_witnessed_residual(self):
        app = Workspace(residual="owner")

        def control(body):
            if not app.live:
                raise ConnectionError()
            return Entity("Juniper").evaluate(body)

        case = replace(self.case, judges=AnswerJudges(Marker("ORCHID-7"), ModelJudge("model/v1", control)))
        out = self.root / "judge-error"
        receipt = run_shared(self.subject, case=case, application=app, mode="deletion", output=out)
        self.assertEqual(receipt["technical_verdict"], "FAIL", receipt)
        self.assertTrue(all(err["operation"] == "control_judge" for err in receipt["errors"]))
        self.assertEqual(receipt, replay(out))

    def test_doctor_reads_positive_control_scanners_and_lineage_without_erasing(self):
        case = replace(self.case, scan_stores=("notes",))
        app = Workspace()
        app.scan = lambda *_: set()
        self.assertFalse(observe_baseline(self.subject, case, app))
        app.scan = lambda *_: {"order"}
        self.assertTrue(observe_baseline(self.subject, case, app))
        app.lineage = lambda *_: {"roots": [], "derivations": []}
        self.assertFalse(observe_baseline(self.subject, case, app))
        self.assertTrue(app.live)
