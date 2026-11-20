from dataclasses import replace
import unittest

from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE
from setrya.comparison import compare
from setrya.drill import Drill
from setrya.evidence import SCHEMA, Report, bind_inputs
from setrya.review import review
from setrya.review_scenarios import compare_scenarios
from setrya.scenarios import ScenarioEvidence, ScenarioMember
from test_checker import History, view
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

    def test_held_and_final_recurrences_need_the_same_scoped_read_on_both_revisions(self):
        def lane(name, revision, stage, markers):
            h = History()
            h.probe(31, "freshness", ["refund@B"])
            if stage == "freshness":
                for index, marker in enumerate(markers):
                    h.probe(33 + 2 * index, stage, [marker])
            h.add("released", 50, attempt="attempt")
            h.receipt("rejected")
            if stage == "final":
                for index, marker in enumerate(markers):
                    h.probe(70 + 2 * index, stage, [marker])
            h.probe(90, "final", ["delivery@1"], "delivery")
            if stage == "freshness":
                h.probe(92, "final", ["refund@B"])
            return make_report(h, name=name, revision=revision)

        for stage in ("freshness", "final"):
            base = lane("base", "a" * 40, stage, ["refund@B", "refund@B", "refund@A"])
            for samples, expected in ((["refund@B"] * 2, 3), (["refund@B"] * 3, 0),
                                      (["refund@B", "refund@B", None, "refund@B"], 3)):
                with self.subTest(stage=stage, samples=samples):
                    candidate = lane("candidate", "b" * 40, stage, samples)
                    self.assertIs(evaluate(candidate).status, Status.PASS)
                    code, summary = review(base, candidate, base_commit="a" * 40, candidate_commit="b" * 40,
                                           base_exit=1, candidate_exit=0)
                    self.assertEqual(code, expected, summary)
                    reverse_code, reverse = compare(candidate, base)
                    self.assertEqual(reverse_code, 1)
                    self.assertTrue(reverse.startswith("REGRESSION" if expected == 0 else "FAILURE OBSERVED"), reverse)

    def test_pre_receipt_failure_needs_a_corresponding_pre_receipt_read(self):
        def lane(name, early):
            h = History()
            h.resume()
            if early:
                h.probe(55, "final", [early])
            h.receipt("rejected")
            h.probe(70, "final", ["refund@B"])
            h.probe(72, "final", ["delivery@1"], "delivery")
            return make_report(h, name=name)

        base = lane("base", "refund@A")
        for early, expected in ((None, 3), ("refund@B", 0)):
            code, text = compare(base, lane("candidate", early))
            self.assertEqual(code, expected, text)

    def test_pre_receipt_read_with_a_commit_watermark_needs_matching_write_visibility(self):
        def lane(name, marker, commit):
            h = History()
            h.resume()
            h.probe(55, "final", [marker], view=view(commit), origins=["attempt"])
            h.receipt()
            h.probe(70, "final", ["refund@B"], view=view())
            h.probe(72, "final", ["delivery@1"], "delivery", view=view())
            return make_report(h, name=name)

        base = lane("base", "refund@A", 3)
        for commit, expected in ((2, 3), (3, 0)):
            candidate = lane("candidate", "refund@B", commit)
            self.assertEqual(compare(base, candidate)[0], expected)
            self.assertTrue(compare(candidate, base)[1].startswith("REGRESSION" if expected == 0 else "FAILURE OBSERVED"))

    def test_matched_refresh_only_read_sets_the_comparison_tier_even_if_later_reads_have_watermarks(self):
        def lane(name, revision, marker):
            h = History()
            h.rules = replace(h.rules, visibility="observed-after-refresh")
            h.resume()
            h.receipt()
            h.add("refreshed", 65, run_id="run", attempt="attempt", generation="generation",
                  route="support-search", scope="support-agent")
            h.probe(70, "final", [marker])
            h.probe(72, "final", ["delivery@1"], "delivery", view=view())
            h.probe(74, "final", ["refund@B"], view=view())
            return make_report(h, name=name, revision=revision)

        def scenario(report):
            verdict = evaluate(report)
            return ScenarioEvidence("update", "single", verdict, {"single": ScenarioMember(verdict, {0: report}, "config", True)})

        base, candidate = lane("base", "a" * 40, "refund@A"), lane("candidate", "b" * 40, "refund@B")
        self.assertEqual(evaluate(candidate).evidence_tier, "write-confirmed")
        for code, text in (compare(base, candidate),
                           compare_scenarios(scenario(base), scenario(candidate), "a" * 40, "b" * 40, 1, 0)):
            self.assertEqual(code, 0, text)
            self.assertTrue(text.startswith("PASS"), text)
            self.assertIn("after refresh", text.splitlines()[0])
        code, reverse = compare(candidate, base)
        self.assertEqual(code, 1)
        self.assertTrue(reverse.startswith("FAILURE OBSERVED"), reverse)

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
        base = history()
        base.probe(75, "final", ["refund@B"])
        before = make_report(base, name="base", revision="a" * 40)
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
