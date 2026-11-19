"""Exercise the controlled worker, scoped probes and offline schedule verification."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from setrya.adapter import Refresh, SearchResult
from setrya.checker import Status
from setrya.drill import Drill, InputError
from setrya.evidence import Report
from setrya.scenario_fixture import Application, _Delivery
from setrya.scenarios import KINDS, SCHEDULES, main, preset, replay, run_scenario


class ScenarioTests(unittest.TestCase):
    def test_a_second_terminal_worker_cannot_qualify_as_still_held_at_the_first_release(self):
        class EarlyCompletion(Application):
            def publish(self):
                result = super().publish()
                self.workers[1].retryable_failure()
                return result

        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "forward"
            verdict = run_scenario("update", "forward", output, factory=EarlyCompletion)
            self.assertIs(verdict.status, Status.NOT_TESTED)
            self.assertIs(replay(output).status, Status.NOT_TESTED)
            manifest = json.loads((output / "scenario.json").read_bytes())
            self.assertEqual(manifest["release_starts"], [])
            for i in (0, 1):
                events = Report.load(output / f"delivery-{i}.json").events
                self.assertNotIn("released", [event["kind"] for event in events])

    def test_scenario_adapter_import_failure_is_redacted(self):
        with TemporaryDirectory() as tmp, redirect_stderr(StringIO()) as errors:
            with patch("setrya.scenarios.import_module", side_effect=RuntimeError("private fixture setting")):
                code = main(["update", "--output", str(Path(tmp) / "scenario")])
            self.assertEqual(code, 2)
            self.assertIn("ERROR", errors.getvalue())
            self.assertNotIn("private fixture setting", errors.getvalue())

    def test_mode_overrides_only_the_bundled_worker_and_preserves_drill_defaults(self):
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
            root = Path(tmp)
            base = ["update", "--drill", "app.toml", "--schedule", "single"]
            for configured, flags, expected in (
                ("broken", [], 1),
                ("broken", ["--mode", "fixed"], 0),
                ("fixed", ["--mode", "broken"], 1),
                ("mistyped", [], 2),
            ):
                with self.subTest(configured=configured, flags=flags):
                    drill = replace(preset("update"), options={"mode": configured})
                    output = root / (configured + "-".join(flags))
                    with patch.object(Drill, "load", return_value=drill):
                        self.assertEqual(main([*base, *flags, "--output", str(output)]), expected)
                    self.assertEqual(replay(output).status.exit_code, expected)
            with patch.object(Drill, "load", return_value=preset("update")):
                output = root / "custom"
                self.assertEqual(main([*base, "--adapter", "custom:Worker", "--mode", "broken",
                                       "--output", str(output)]), 2)
                self.assertIn("--mode applies only to the bundled SQLite worker", errors.getvalue())
                self.assertFalse(output.exists())

    def test_fixture_reports_the_target_it_captured_and_the_mutation_it_performed(self):
        revisions = {"update": "updated", "delete": "deleted", "revoke": "revoked"}
        for kind in KINDS:
            for mode in ("fixed", "broken"):
                for field, value in (("document", "other-doc"), ("old_revision", "Z"),
                                     ("checkpoint", "other-hook"), ("new_revision", "unexpected"),
                                     ("new_revision", "deleted" if kind == "revoke" else "revoked")):
                    with self.subTest(kind=kind, mode=mode, field=field, value=value), TemporaryDirectory() as tmp:
                        drill = preset(kind, mode)
                        drill = replace(drill, rules=replace(drill.rules, **{field: value}))
                        output = Path(tmp, "run")
                        live = run_scenario(kind, "single", output, drill=drill)
                        self.assertIs(live.status, Status.NOT_TESTED)
                        self.assertEqual(replay(output), live)
                        events = Report.load(output / "delivery-0.json").events
                        held = next(e for e in events if e["kind"] == "held")
                        self.assertEqual((held["document"], held["revision"], held["name"]),
                                         ("subject", "A", "before-write"))
                        if field == "new_revision":
                            published = next(e["publication"] for e in events if e["kind"] == "published")
                            self.assertEqual((published["document"], published["revision"]), ("subject", revisions[kind]))
                        self.assertFalse(any(e["kind"] == "released" for e in events))

    def test_retry_accepts_an_injected_application_without_a_configured_factory(self):
        drill = replace(preset("delete"), factory=None, options={})
        with TemporaryDirectory() as tmp:
            output = Path(tmp, "retry")
            self.assertIs(run_scenario("delete", "retry", output, drill=drill, factory=Application).status,
                          Status.PASS)
            self.assertIs(replay(output).status, Status.PASS)
            retry = json.loads((output / "scenario.json").read_bytes())["retry"]
            self.assertEqual(retry["receipt"]["outcome"], "rejected")

    def test_every_revoked_baseline_marker_requires_positive_authorization_evidence(self):
        drill = preset("revoke")
        primary, retained, owner = drill.probes
        for baseline in (("subject@A",), ("subject@A", "retained@A")):
            with self.subTest(baseline=baseline), TemporaryDirectory() as tmp:
                subject = replace(primary, rules=replace(primary.rules, before=baseline,
                                                         forbid=("subject@A", "retained@A")))
                unrelated = replace(owner, question="control", rules=replace(
                    owner.rules, before=("retained@A",), require=("retained@A",)))
                probes = (subject, retained, unrelated)
                weak = replace(drill, probes=probes, rules=replace(drill.rules, probes=tuple(p.rules for p in probes)))
                out = Path(tmp, "run")
                with self.assertRaisesRegex(InputError, "each revoked baseline marker"):
                    run_scenario("revoke", "single", out, drill=weak)
                self.assertFalse(out.exists())

    def test_v090_revocation_with_unrelated_positive_control_stays_readable(self):
        # The old release ran a tenant-wide deletion and kept only an unrelated
        # marker for the owner. It returned PASS on the guarded worker.
        with TemporaryDirectory() as tmp:
            with ZipFile(Path(__file__).with_name("fixtures") / "scenario-v090-unrelated-control.zip") as bundle:
                bundle.extractall(tmp)
            result = replay(Path(tmp, "fixed"))
            self.assertIs(result.status, Status.NOT_TESTED)
            self.assertIn("still-authorized control", result.detail)
            self.assertIs(replay(Path(tmp, "broken")).status, Status.FAIL)

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
                                manifest = json.loads((out / "scenario.json").read_bytes())
                                self.assertEqual(manifest["deliveries"], [
                                    sha256((out / f"delivery-{i}.json").read_bytes()).hexdigest() for i in (0, 1)
                                ])
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
