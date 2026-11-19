from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.adapter import Refresh
from setrya.checker import Status, Verdict
from setrya.cli import main
from setrya.scenario_fixture import Application, _Delivery
from setrya.scenarios import inspect_scenario, preset
from setrya.review_scenarios import compare_scenarios


BASE = "a" * 40
CANDIDATE = "b" * 40


class ScenarioReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.base, cls.candidate = cls.directory / "private-base", cls.directory / "private-candidate"

        # Change the application implementation at the seam under test. The adapter
        # factory, fixture options, search questions and schedules are identical.
        initialize = Application.__init__

        def broken(app, context):
            initialize(app, context)
            app.broken = True

        with redirect_stdout(StringIO()):
            with patch.object(Application, "__init__", broken):
                code = main(["scenario", "revoke", "--schedule", "all", "--output", str(cls.base), "--revision", BASE])
                if code != 1:
                    raise AssertionError(f"broken scenario returned {code}")
            code = main(["scenario", "revoke", "--schedule", "all", "--output", str(cls.candidate),
                         "--revision", CANDIDATE])
            if code != 0:
                raise AssertionError(f"fixed scenario returned {code}")

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def review(self, before, after, base=BASE, candidate=CANDIDATE, exits=(1, 0), output=None):
        summary = output or self.work / "review.md"
        console = StringIO()
        with redirect_stdout(console), redirect_stderr(console):
            code = main(["review", str(before), str(after), "--base-commit", base, "--candidate-commit", candidate,
                         "--base-exit", str(exits[0]), "--candidate-exit", str(exits[1]), "--summary", str(summary)])
        if output is None:
            self.assertIn(console.getvalue().strip(), summary.read_text())
        return code, console.getvalue()

    def test_working_ci_handoff_and_matching_regression_in_reverse(self):
        for schedule in ("all", "reverse"):
            with self.subTest(schedule=schedule):
                before = self.base if schedule == "all" else self.base / schedule
                after = self.candidate if schedule == "all" else self.candidate / schedule
                code, text = self.review(before, after)
                self.assertEqual(code, 0, text)
                self.assertTrue(text.startswith("FIX VERIFIED"), text)
                self.assertIn("final", text)
                self.assertIn("caller support", text)
                self.assertIn("write-confirmed", text)
                prefix = "single/" if schedule == "all" else ""
                self.assertIn(f'setrya replay "$CANDIDATE_REPORT/{prefix}delivery-0.json"', text)
                self.assertIn('setrya replay "$BASE_REPORT"', text)
                self.assertIn("Set BASE_REPORT to the path", text)
                self.assertIn("Set CANDIDATE_REPORT to the path", text)
                self.assertNotIn(str(self.directory), text)
                if schedule == "all":
                    self.assertIn("Schedule reverse", text)
                code, text = self.review(after, before, CANDIDATE, BASE, (0, 1))
                self.assertEqual(code, 1)
                self.assertTrue(text.startswith("REGRESSION"), text)

    def test_changed_options_scope_revisions_exits_and_mixed_types(self):
        other = self.work / "changed-options"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["scenario", "revoke", "--schedule", "reverse", "--mode", "broken",
                                   "--output", str(other), "--revision", BASE]), 1)
        cases = (
            ((other, self.candidate / "reverse"), {}, 3, "adapter, options, or drill"),
            ((self.base / "reverse", self.candidate / "forward"), {}, 3, "schedule differs"),
            ((self.base / "reverse", self.candidate / "reverse"), {"base": "c" * 40}, 3, "expected commit"),
            ((self.base / "reverse", self.candidate / "reverse"), {"exits": (0, 0)}, 2, "test exit disagrees"),
            ((self.base / "reverse", self.candidate / "reverse/delivery-0.json"), {}, 3, "two scenario directories"),
        )
        for (before, after), keywords, expected, reason in cases:
            with self.subTest(reason=reason):
                code, text = self.review(before, after, **keywords)
                self.assertEqual(code, expected)
                self.assertIn(reason, text)

    def test_same_contract_can_pass_across_two_revisions(self):
        other = self.work / "fixed"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["scenario", "revoke", "--schedule", "single", "--output", str(other),
                                   "--revision", BASE]), 0)
        code, text = self.review(other, self.candidate / "single", exits=(0, 0))
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("PASS"), text)

    def test_weak_sibling_downgrades_suite_even_when_the_first_fix_is_write_confirmed(self):
        drill = preset("revoke")
        drill = replace(drill, rules=replace(drill.rules, visibility="observed-after-refresh"))
        before, after = self.work / "before", self.work / "after"
        original_seed, original_receipt = Application.seed, _Delivery.receipt

        def broken(app):
            original_seed(app)
            app.broken = True

        def acknowledged(worker):
            receipt = original_receipt(worker)
            if receipt and worker.message == "job-b":
                return replace(receipt, outcome="committed", generation="projection")
            return receipt

        def refresh(worker, write, probe):
            return Refresh(write.run_id, write.attempt, write.generation, probe.rules.route, probe.rules.scope)

        with patch("setrya.scenarios.preset", return_value=drill), redirect_stdout(StringIO()):
            with patch.object(Application, "seed", broken):
                self.assertEqual(main(["scenario", "revoke", "--schedule", "all", "--output", str(before),
                                       "--revision", BASE]), 1)
            with patch.object(_Delivery, "receipt", acknowledged), patch.object(_Delivery, "refresh", refresh, create=True):
                self.assertEqual(main(["scenario", "revoke", "--schedule", "all", "--output", str(after),
                                       "--revision", CANDIDATE]), 0)
        code, text = self.review(before, after)
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("PASS"), text)
        self.assertIn("observed-after-refresh", text)
        self.assertNotIn("FIX VERIFIED", text)

    def test_missing_delivery_or_suite_member_prevents_verified_fix(self):
        for target in ("reverse/delivery-0.json", "single/scenario.json"):
            with self.subTest(target=target):
                before = self.work / Path(target).stem
                copytree(self.base, before)
                (before / target).unlink()
                code, text = self.review(before, self.candidate)
                self.assertEqual(code, 2)
                self.assertIn("missing valid evidence", text)
                self.assertIn("Base recorded FAIL", text)
                self.assertNotIn("FIX VERIFIED", text)

    def test_summary_cannot_overwrite_nested_evidence_or_follow_a_directory_alias(self):
        before = self.base / "reverse"
        target = before / "delivery-0.json"
        original = target.read_bytes()
        alias = self.work / "alias"
        alias.symlink_to(before, target_is_directory=True)
        for output in (target, self.candidate / "untracked.md", alias / "delivery-0.json"):
            with self.subTest():
                code, text = self.review(before, self.candidate, output=output)
                self.assertEqual(code, 2)
                self.assertIn("outside the evidence directories", text)
        self.assertEqual(original, target.read_bytes())
        self.assertFalse((self.candidate / "untracked.md").exists())

    def test_legacy_artifacts_replay_but_lack_cross_revision_identity(self):
        old = self.work / "legacy"
        copytree(self.base / "single", old)
        manifest = old / "scenario.json"
        data = json.loads(manifest.read_text())
        data["version"] = 1
        del data["revision"]
        manifest.write_text(json.dumps(data))
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["replay", str(old)]), 1)
        code, text = self.review(old, self.candidate / "single")
        self.assertEqual(code, 3)
        self.assertIn("cross-revision scenario identity", text)

    def test_shared_failure_requires_matching_cross_worker_proof(self):
        base = inspect_scenario(self.base / "reverse")
        candidate = inspect_scenario(self.candidate / "reverse")
        # Even when the individual failure also has matching proof, the shared
        # failure is a second, independently observed violation.
        shared = Verdict(Status.FAIL, "A sibling observed the prior grant while a different worker was held.")
        member = replace(base.members["reverse"], shared_failure=shared)
        code, text = compare_scenarios(replace(base, members={"reverse": member}), candidate,
                                       BASE, CANDIDATE, 1, 0)
        self.assertEqual(code, 3)
        self.assertIn("across workers", text)
        self.assertIn(shared.detail, text)
