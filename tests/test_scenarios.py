"""Exercise the controlled worker, scoped probes and offline schedule verification."""
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.adapter import Refresh, SearchResult
from setrya.checker import Status
from setrya.drill import InputError
from setrya.evidence import Report
from setrya.scenario_fixture import Application, _Delivery
from setrya.scenarios import KINDS, SCHEDULES, main, preset, replay, run_scenario


class ScenarioTests(unittest.TestCase):
    def test_every_worker_schedule_and_mutation_replays_after_teardown(self):
        with TemporaryDirectory() as tmp:
            for kind in KINDS:
                for mode, expected in (("broken", Status.FAIL), ("fixed", Status.PASS)):
                    for schedule in SCHEDULES:
                        with self.subTest(kind=kind, mode=mode, schedule=schedule):
                            out = Path(tmp, kind, mode, schedule)
                            result = run_scenario(kind, schedule, out, drill=preset(kind, mode))
                            self.assertIs(result.status, expected)
                            self.assertEqual(replay(out), result)
                            if schedule == "reverse":
                                reports = [Report.load(out / f"delivery-{i}.json") for i in (0, 1)]
                                released = [next(e["ns"] for e in r.events if e["kind"] == "released") for r in reports]
                                self.assertLess(released[1], released[0])

    def test_weakest_visibility_tier_survives_live_and_offline_suite_aggregation(self):
        configured = preset("delete")
        configured = replace(configured, rules=replace(configured.rules, visibility="observed-after-refresh"))
        receipt = _Delivery.receipt

        def acknowledged(worker):
            result = receipt(worker)
            if not result or worker.abort.is_set() or worker.message == "job-b":
                return result
            return replace(result, outcome="committed", generation="projection")

        def refresh(worker, write, probe):
            return Refresh(write.run_id, write.attempt, write.generation, probe.rules.route, probe.rules.scope)

        with (TemporaryDirectory() as tmp, patch.object(_Delivery, "receipt", acknowledged),
              patch.object(_Delivery, "refresh", refresh, create=True),
              patch("setrya.scenarios.preset", return_value=configured)):
            for schedule in ("single", "all"):
                out = Path(tmp, schedule)
                with redirect_stdout(StringIO()) as console:
                    self.assertEqual(main(["delete", "--schedule", schedule, "--output", str(out)]), 0)
                self.assertIn("write visibility unverified", console.getvalue().splitlines()[-1])
                self.assertEqual(replay(out).evidence_tier, "observed-after-refresh")

    def test_reverse_exposes_a_failure_that_forward_order_misses(self):
        class ReorderBug(Application):
            last_message = None

            def delivery(self, message_id, attempt):
                worker = super().delivery(message_id, attempt)
                release = worker.release

                def apply():
                    self.broken = (self.last_message, message_id) == ("job-b", "job-a")
                    self.last_message = message_id
                    release()

                worker.release = apply
                return worker

        with TemporaryDirectory() as tmp:
            for schedule, expected in (("forward", Status.PASS), ("reverse", Status.FAIL)):
                verdict = run_scenario("delete", schedule, Path(tmp, schedule), factory=ReorderBug)
                self.assertIs(verdict.status, expected)

    def test_revocation_requires_positive_read_on_original_and_still_authorized_callers(self):
        original = _Delivery.search
        def missing_control(worker, probe):
            if probe.rules.id == "retained":
                return SearchResult((), route="search", scope=probe.rules.scope)
            return original(worker, probe)
        with TemporaryDirectory() as tmp, patch.object(_Delivery, "search", missing_control):
            out = Path(tmp, "control")
            self.assertIs(run_scenario("revoke", "single", out).status, Status.NOT_TESTED)
            events = Report.load(out / "delivery-0.json").events
            self.assertFalse(any(e["kind"] == "released" for e in events))
            drill = preset("revoke")
            no_owner = replace(drill, rules=replace(drill.rules, probes=drill.rules.probes[:2]), probes=drill.probes[:2])
            with self.assertRaises(InputError):
                run_scenario("revoke", "single", Path(tmp, "owner"), drill=no_owner)

    def test_retry_fails_closed_if_the_first_attempt_cannot_be_bound_to_the_same_message(self):
        with TemporaryDirectory() as tmp, patch.object(_Delivery, "message_id", return_value="other"):
            self.assertIs(run_scenario("delete", "retry", Path(tmp, "r")).status, Status.NOT_TESTED)

    def test_adapter_error_after_confirmed_residue_preserves_failure(self):
        close = Application.close
        def error(app):
            close(app)
            raise ConnectionError
        with TemporaryDirectory() as tmp, patch.object(Application, "close", error):
            out = Path(tmp, "error")
            verdict = run_scenario("delete", "single", out, drill=preset("delete", "broken"))
            self.assertIs(verdict.status, Status.FAIL)
            self.assertEqual(json.loads((out / "scenario.json").read_text())["error"], "ConnectionError")

    def test_mixed_suite_members_are_rejected_and_confirmed_failure_survives_missing_lane(self):
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            suites = [Path(tmp, name) for name in ("one", "two")]
            for directory in suites:
                self.assertEqual(main(["delete", "--schedule", "all", "--output", str(directory)]), 0)
            (suites[0] / "retry/scenario.json").write_bytes((suites[1] / "retry/scenario.json").read_bytes())
            self.assertIs(replay(suites[0]).status, Status.ERROR)
            broken = Path(tmp, "broken")
            self.assertEqual(main(["delete", "--mode", "broken", "--schedule", "all", "--output", str(broken)]), 1)
            (broken / "single/scenario.json").unlink()
            self.assertIs(replay(broken).status, Status.FAIL)
            (broken / "reverse/delivery-0.json").unlink()
            self.assertIs(replay(broken / "reverse").status, Status.FAIL)
