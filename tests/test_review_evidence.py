from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.checker import Status, evaluate
from setrya.comparison import compare
from setrya.drill import InputError
from setrya.evidence import Report
from test_checker import History, view
from test_runtime_evidence import make_report


class EvidenceBoundaryTests(unittest.TestCase):
    def test_commit_receipt_must_arrive_within_the_settle_window(self):
        for delay, passage, expected in (
            (0, "refund@B", Status.PASS), (1, "refund@B", Status.NOT_TESTED),
            (0, "different", Status.FAIL), (1, "different", Status.NOT_TESTED),
            (1, "refund@A", Status.FAIL),
        ):
            with self.subTest(delay=delay, passage=passage):
                h = History()
                h.resume()
                h.probe(80, "final", [passage], view=view())
                h.probe(82, "final", ["delivery@1"], "delivery", view=view())
                h.add("receipt", 50 + h.rules.settle_ms + delay, **h.identity,
                      outcome="committed", generation="generation", commit=3)
                report = make_report(h)
                with TemporaryDirectory() as directory:
                    path = Path(directory) / "run.json"
                    report.save(path)
                    for candidate in (report, Report.load(path)):
                        verdict = evaluate(candidate)
                        self.assertIs(verdict.status, expected)
                        if expected is Status.NOT_TESTED:
                            self.assertIn("receipt arrived after", verdict.detail)
                            self.assertEqual(verdict.event, len(h.events))

    def test_older_reports_enforce_measurable_checkpoint_deadlines(self):
        h = History()
        h.rules = replace(h.rules, checkpoint_ms=1)
        h.resume()
        h.receipt("rejected")
        h.probe(80, "final", ["refund@A"])
        h.probe(82, "final", ["delivery@1"], "delivery")
        held = next(event for event in h.events if event["kind"] == "held")
        held["ns"] = 7_000_000  # Start was at 6 ms.
        base = make_report(h, name="base", schema=3)
        self.assertIs(evaluate(base).status, Status.FAIL)

        held["ns"] += 1
        h.events[-2]["passages"] = ["refund@B"]
        for schema in (2, 3):
            with self.subTest(schema=schema):
                candidate = make_report(h, name="candidate", schema=schema)
                verdict = evaluate(candidate)
                self.assertIs(verdict.status, Status.NOT_TESTED)
                self.assertIn("checkpoint acknowledgment arrived after", verdict.detail)
                if schema == 3:
                    code, headline = compare(base, candidate)
                    self.assertEqual(code, 3)
                    self.assertTrue(headline.startswith("INCONCLUSIVE"))

    def test_schema_one_cannot_claim_modern_route_and_scope_fields(self):
        h = History()
        for event in h.events:
            if event["kind"] == "probe":
                del event["route"], event["scope"]
            elif event["kind"] == "published":
                del event["publication"]
        legacy = make_report(h, schema=1)
        self.assertIs(evaluate(legacy).status, Status.NOT_TESTED)
        for key in ("route", "scope"):
            with self.subTest(field=key):
                with self.assertRaises(InputError):
                    evaluate(replace(legacy, events=tuple(
                        {**event, key: "identifier"} if event["kind"] == "probe" else event
                        for event in legacy.events
                    )))

    def test_programmatic_headers_reject_invalid_evidence_before_save_or_evaluation(self):
        h = History()
        h.resume()
        h.receipt("rejected")
        h.probe(80, "final", ["refund@B"])
        h.probe(82, "final", ["delivery@1"], "delivery")
        report = make_report(h)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            report.save(path)
            saved = path.read_bytes()
            for invalid in (replace(report, run_id="private run\n"),
                            replace(report, rules=replace(report.rules, new_revision=report.rules.old_revision)),
                            replace(report, rules=None), replace(report, events=iter(report.events)),
                            replace(report, schema=2)):
                with self.subTest(report=invalid):
                    with self.assertRaises(InputError):
                        evaluate(invalid)
                    with self.assertRaises(InputError):
                        invalid.save(path)
                    self.assertEqual(path.read_bytes(), saved)
