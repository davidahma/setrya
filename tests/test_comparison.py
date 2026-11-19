from dataclasses import replace
import unittest

from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE
from setrya.comparison import compare
from setrya.drill import Drill
from setrya.evidence import SCHEMA, Report, bind_inputs
from setrya.review import review
from test_checker import History
from test_runtime_evidence import make_report


class ComparisonTests(unittest.TestCase):
    def test_passing_final_probe_alone_cannot_verify_an_early_failure(self):
        drill = Drill.load(EXAMPLE / "drill.toml")
        primary, control = drill.probes
        control = replace(control, rules=replace(control.rules, forbid=("delivery@old",)))
        rules = replace(drill.rules, probes=(primary.rules, control.rules))
        inputs = bind_inputs(replace(drill, rules=rules, probes=(primary, control)))

        def report(run_id, early_passages):
            history = History()
            history.probe(40, "freshness", ["refund@B"])
            if early_passages:
                history.probe(45, "freshness", early_passages, "delivery")
            history.add("released", 50, attempt="attempt")
            history.receipt("rejected")
            history.probe(70, "final", ["refund@B"])
            history.probe(72, "final", ["delivery@1"], "delivery")
            for event in history.events:
                if "run_id" in event:
                    event["run_id"] = run_id
                if event.get("publication"):
                    event["publication"]["run_id"] = run_id
            return Report(run_id, rules, tuple(history.events), SCHEMA, inputs)

        before = report("before", ["delivery@old"])
        after = report("after", None)
        self.assertIs(evaluate(before).status, Status.FAIL)
        self.assertIs(evaluate(after).status, Status.PASS)
        code, text = compare(before, after)
        self.assertEqual(code, 3)
        self.assertIn("affected phase", text)
        self.assertIn("Probe delivery", text)
        code, text = compare(before, report("after", ["delivery@1"]))
        self.assertEqual(code, 0)
        self.assertIn("After PASS · event", text)
        self.assertIn("freshness · delivery@1", text)

    def test_recurrence_while_held_requires_a_second_read_on_the_passing_revision(self):
        def lane(name, followup, revision):
            h = History()
            h.probe(35, "freshness", ["refund@B"])
            if followup:
                h.probe(40, "freshness", [followup])
            h.add("released", 50, attempt="attempt")
            h.receipt("rejected")
            h.probe(70, "final", ["refund@B"])
            h.probe(75, "final", ["delivery@1"], "delivery")
            return make_report(h, name=name, revision=revision)

        base = lane("base", "refund@A", "a" * 40)
        for followup, expected in ((None, 3), ("refund@B", 0)):
            with self.subTest(followup=followup):
                candidate = lane("candidate", followup, "b" * 40)
                self.assertEqual(evaluate(candidate).status, Status.PASS)
                self.assertEqual(compare(base, candidate)[0], expected)
                code, text = review(base, candidate, base_commit="a" * 40, candidate_commit="b" * 40,
                                    base_exit=1, candidate_exit=0)
                self.assertEqual(code, expected, text)
                _, reverse = compare(candidate, base)
                if followup:
                    self.assertIn("After PASS · event 8", text)
                    self.assertTrue(reverse.startswith("REGRESSION"))
                else:
                    self.assertTrue(text.startswith("INCONCLUSIVE"))
                    self.assertTrue(reverse.startswith("FAILURE OBSERVED"))

    def test_absence_requires_a_control_observed_after_the_deletion(self):
        def lane(name, control_after_deletion):
            history = History()
            primary, control = history.rules.probes
            history.rules = replace(history.rules, probes=(replace(primary, require=(), absence=True),
                                                           replace(control, forbid=("delivery@old",))))
            history.probe(20, "freshness", ["refund@A"])
            history.probe(22, "freshness", ["delivery@1"], "delivery")
            history.probe(30, "freshness", [])
            history.probe(32, "freshness", control_after_deletion, "delivery")
            witness = len(history.events)
            history.add("released", 50, attempt="attempt")
            history.receipt("rejected")
            history.probe(70, "final", [])
            history.probe(72, "final", ["delivery@1"], "delivery")
            return make_report(history, name=name), witness

        before, _ = lane("before", ["delivery@old"])
        after, witness = lane("after", ["delivery@1"])
        code, summary = compare(before, after)
        self.assertEqual(code, 0)
        self.assertIn(f"After PASS · event {witness} · delivery · freshness", summary)
        code, summary = compare(after, before)
        self.assertEqual(code, 1)
        self.assertIn(f"Before PASS · event {witness} · delivery · freshness", summary)

    def test_regression_claim_requires_write_visibility_on_the_matching_base_probe(self):
        from test_store_signals import VisibilityTests

        history = VisibilityTests().history
        before = make_report(history(), name="base", revision="a" * 40)
        candidate = history()
        candidate.probe(75, "final", ["refund@A"])
        after = make_report(candidate, name="candidate", revision="b" * 40)
        for code, summary in (compare(before, after),
                              review(before, after, base_commit="a" * 40, candidate_commit="b" * 40,
                                     base_exit=0, candidate_exit=1)):
            self.assertEqual(code, 1)
            headline = summary.splitlines()[0]
            self.assertTrue(headline.startswith("FAILURE OBSERVED"), headline)
            self.assertIn("after refresh", headline)
            self.assertIn("unverified", headline)

    def test_fingerprints_bind_each_exact_question_without_recording_text(self):
        drill = Drill.load(EXAMPLE / "drill.toml")
        question = drill.probes[0].question
        original = bind_inputs(drill)
        changed = bind_inputs(replace(drill, probes=(replace(drill.probes[0], question=question + " "), *drill.probes[1:])))
        self.assertNotEqual(original["questions"][drill.probes[0].rules.id],
                            changed["questions"][drill.probes[0].rules.id])
        self.assertNotIn(question, str(original))


if __name__ == "__main__":
    unittest.main()
