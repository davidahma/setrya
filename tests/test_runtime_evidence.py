from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE
from setrya.comparison import compare
from setrya.drill import Drill
from setrya.evidence import SCHEMA, Report, bind_inputs
from setrya.review import review
from test_checker import History, view


def make_report(history, *, name="run", revision=None, schema=SCHEMA):
    events = deepcopy(history.events)
    for event in events:
        if "run_id" in event:
            event["run_id"] = name
        if event.get("publication"):
            event["publication"]["run_id"] = name
    inputs = bind_inputs(Drill.load(EXAMPLE / "drill.toml"), revision=revision) if schema >= 3 else None
    return Report(name, history.rules, tuple(events), schema, inputs)


def completed_history(outcome="committed"):
    history = History()
    history.resume()
    history.receipt(outcome)
    history.probe(70, "final", ["refund@B"], view=view())
    history.probe(72, "final", ["delivery@1"], "delivery", view=view())
    return history


class RuntimeEvidenceTests(unittest.TestCase):
    def test_rejection_must_precede_the_read_even_when_timestamps_tie(self):
        history = History()
        history.resume()
        history.probe(60, "final", ["refund@B"], start=60)
        history.receipt("rejected")
        history.probe(61, "final", ["delivery@1"], "delivery")
        self.assertIs(evaluate(make_report(history)).status, Status.NOT_TESTED)
        history.probe(62, "final", ["refund@B"])
        self.assertIs(evaluate(make_report(history)).status, Status.PASS)

    def test_latest_unidentified_rank_supersedes_confirmation_on_replay(self):
        history = completed_history("rejected")
        history.probe(80, "final", ["refund@B", None])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for schema in range(2, SCHEMA + 1):
                with self.subTest(schema=schema):
                    report = make_report(history, schema=schema)
                    report.save(path)
                    verdict = evaluate(Report.load(path))
                    self.assertIs(verdict.status, Status.NOT_TESTED)
                    self.assertEqual(verdict.event, len(report.events))
                    self.assertEqual(verdict.probe, "refund-window")
                    self.assertIn("identity", verdict.detail)

    def test_latest_read_needs_write_visibility_and_can_be_reconfirmed(self):
        for watermark in (None, view(commit=2), view(generation="other"), view(scope="other")):
            with self.subTest(watermark=watermark):
                history = completed_history()
                history.probe(80, "final", ["delivery@1"], "delivery", view=watermark)
                ambiguous = make_report(history, name="after", revision="b" * 40)
                verdict = evaluate(ambiguous)
                self.assertIs(verdict.status, Status.NOT_TESTED)
                self.assertEqual(verdict.event, len(history.events))
                self.assertEqual(verdict.probe, "delivery")
                base = make_report(completed_history(), name="before", revision="a" * 40)
                code, _ = review(base, ambiguous, base_commit="a" * 40, candidate_commit="b" * 40,
                                 base_exit=0, candidate_exit=0)
                self.assertEqual(code, 3)
                history.probe(85, "final", ["delivery@1"], "delivery", view=view(4))
                confirmed = evaluate(make_report(history))
                self.assertIs(confirmed.status, Status.PASS)
                self.assertIn(len(history.events), confirmed.confirmed)

    def test_foreign_caller_or_late_ambiguous_sample_does_not_replace_a_valid_read(self):
        history = completed_history()
        decisive = history.events[-2]["seq"]
        history.probe(80, "final", [None])
        history.events[-1]["scope"] = "preview"
        history.probe(51 + history.rules.settle_ms, "final", [None])
        verdict = evaluate(make_report(history))
        self.assertIs(verdict.status, Status.PASS)
        self.assertEqual(verdict.event, decisive)

    def test_later_unknown_and_confirmed_samples_preserve_established_failure(self):
        history = completed_history()
        history.probe(75, "final", [], view=view(4))
        failure = len(history.events)
        history.probe(80, "final", [None])
        history.probe(85, "final", ["refund@B"], view=view(5))
        verdict = evaluate(make_report(history))
        self.assertIs(verdict.status, Status.FAIL)
        self.assertEqual(verdict.event, failure)

    def test_regression_requires_a_matching_observation_on_the_base(self):
        def lane(name, revision, early):
            history = History()
            primary, control = history.rules.probes
            history.rules = replace(history.rules, probes=(primary, replace(control, forbid=("retired",))))
            history.probe(40, "freshness", ["refund@B"])
            if early is not None:
                history.probe(45, "freshness", early, "delivery")
            history.add("released", 50, attempt="attempt")
            history.receipt("rejected")
            history.probe(70, "final", ["refund@B"])
            history.probe(72, "final", ["delivery@1"], "delivery")
            return make_report(history, name=name, revision=revision)

        candidate = lane("candidate", "b" * 40, ["delivery@1", "retired"])
        for sample, expected in ((None, "FAILURE OBSERVED"), (["delivery@1"], "REGRESSION")):
            with self.subTest(sample=sample):
                base = lane("base", "a" * 40, sample)
                self.assertIs(evaluate(base).status, Status.PASS)
                self.assertIs(evaluate(candidate).status, Status.FAIL)
                reviewed = review(base, candidate, base_commit="a" * 40, candidate_commit="b" * 40,
                                  base_exit=0, candidate_exit=1)
                for code, summary in (reviewed, compare(base, candidate)):
                    self.assertEqual(code, 1)
                    self.assertTrue(summary.startswith(expected), summary)
                    self.assertIn("affected phase" if sample is None else "matching phase", summary)
