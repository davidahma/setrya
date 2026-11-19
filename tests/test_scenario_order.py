"""A shared publication establishes one transition for every delivery observing it."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.adapter import SearchResult
from setrya.checker import Status, evaluate
from setrya.evidence import Report
from setrya.scenario_fixture import Application
from setrya.scenario_order import shared_transition_failure
from setrya.scenarios import preset, replay, run_scenario


class SecondDelivery(Application):
    stale_scope = "support"

    def publish(self):
        publication = super().publish()
        self.published = True
        return publication

    def delivery(self, message_id, attempt):
        worker = super().delivery(message_id, attempt)
        read = worker.search
        returned_stale = False

        def search(probe):
            nonlocal returned_stale
            if (attempt.endswith(".1") and getattr(self, "published", False)
                    and probe.rules.id == "subject" and not returned_stale):
                returned_stale = True
                return SearchResult(("subject@A",), route=probe.rules.route, scope=self.stale_scope)
            return read(probe)

        worker.search = search
        return worker


def journals(directory):
    return {lane: Report.load(directory / f"delivery-{lane}.json") for lane in (0, 1)}


class ScenarioOrderTests(unittest.TestCase):
    def test_new_then_old_on_another_delivery_fails_live_and_on_replay(self):
        with TemporaryDirectory() as tmp:
            for kind, schedule in (("update", "duplicate"), ("delete", "reverse"), ("revoke", "forward")):
                with self.subTest(kind=kind, schedule=schedule):
                    directory = Path(tmp, schedule)
                    result = run_scenario(kind, schedule, directory, factory=SecondDelivery)
                    reports = journals(directory)
                    self.assertTrue(all(evaluate(report).status is Status.PASS for report in reports.values()))
                    self.assertEqual(result, shared_transition_failure(reports))
                    self.assertIs(result.status, Status.FAIL)
                    self.assertIn("Delivery 1 event", result.detail)
                    self.assertIn("after delivery 0 event", result.detail)
                    self.assertEqual(result.probe, "subject")
                    self.assertEqual(result, replay(directory))

    def test_unrelated_or_overlapping_reads_do_not_establish_a_cross_delivery_failure(self):
        class Preview(SecondDelivery):
            stale_scope = "preview"

        with TemporaryDirectory() as tmp:
            directory = Path(tmp, "duplicate")
            self.assertIs(run_scenario("update", "duplicate", directory, factory=Preview).status, Status.PASS)
            reports = journals(directory)
            self.assertIsNone(shared_transition_failure(reports))
            transition = next(e for e in reports[0].events if e.get("stage") == "freshness")
            stale = next(e for e in reports[1].events if e.get("stage") == "freshness")
            for start in (transition["ns"] - 1, transition["ns"]):
                with self.subTest(start=start):
                    events = tuple(e | {"scope": "support", "started_ns": start} if e is stale else e
                                   for e in reports[1].events)
                    overlapping = replace(reports[1], events=events)
                    overlapping.validate()
                    self.assertIsNone(shared_transition_failure({0: reports[0], 1: overlapping}))

    def test_individual_final_failure_keeps_its_original_attribution(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp, "reverse")
            result = run_scenario("delete", "reverse", directory, drill=preset("delete", "broken"))
            self.assertIs(result.status, Status.FAIL)
            self.assertIsNone(shared_transition_failure(journals(directory)))
