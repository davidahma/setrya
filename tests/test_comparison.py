from dataclasses import replace
import unittest

from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE
from setrya.comparison import compare
from setrya.drill import Drill
from setrya.evidence import SCHEMA, Report, bind_inputs
from test_checker import History


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
