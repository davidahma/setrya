"""Scenario v2 identifies the test independently from the checkout under test."""

from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from setrya.cli import main as cli
from setrya.checker import Status
from setrya.drill import InputError
from setrya.scenario_fixture import Application
from setrya.scenarios import inspect_scenario, main, replay, run_scenario


class ScenarioVersionTests(unittest.TestCase):
    def test_release_080_replays_with_its_historical_identity(self):
        with TemporaryDirectory() as tmp:
            fixture = Path(__file__).parent / "fixtures" / "scenario-v080.zip"
            with ZipFile(fixture) as bundle:
                bundle.extractall(tmp)
            evidence = inspect_scenario(tmp)
            self.assertEqual(evidence.verdict.status, Status.PASS)
            self.assertIsNone(evidence.members["duplicate"].comparison_id)

    def test_commit_bound_inside_deliveries_and_suite(self):
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            root = Path(tmp, "suite")
            self.assertEqual(main(["update", "--schedule", "all", "--output", str(root),
                                   "--revision", "a" * 40]), 0)
            for path in (root, root / "retry"):
                manifest = path / "scenario.json"
                meta = json.loads(manifest.read_bytes())
                meta["revision"] = "b" * 40
                manifest.write_text(json.dumps(meta))
                self.assertEqual(replay(path).status, Status.ERROR)

    def test_cleanup_error_preserves_an_interrupted_suite(self):
        with (TemporaryDirectory() as tmp, redirect_stdout(StringIO()),
              patch.object(Application, "seed", side_effect=KeyboardInterrupt),
              patch.object(Application, "close", side_effect=ConnectionError)):
            root = Path(tmp, "suite")
            self.assertEqual(main(["delete", "--schedule", "all", "--output", str(root)]), 2)
            self.assertFalse((root / "retry").exists())
            self.assertEqual(json.loads((root / "single/scenario.json").read_bytes())["error"], "KeyboardInterrupt")

    def test_injected_wrappers_replay_but_cannot_verify_an_adapter_change(self):
        class Broken(Application):
            def __init__(self, context):
                super().__init__(context)
                self.broken = True

        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            base, candidate = (Path(tmp, n) for n in ("base", "candidate"))
            for path, factory, sha, status in ((base, Broken, "a" * 40, Status.FAIL),
                                               (candidate, Application, "b" * 40, Status.PASS)):
                self.assertEqual(run_scenario("delete", "duplicate", path, factory=partial(factory),
                                              revision=sha).status, status)
                self.assertIsNone(inspect_scenario(path).members["duplicate"].comparison_id)
            for a, b in ((base, candidate), (base / "delivery-0.json", candidate / "delivery-0.json")):
                self.assertEqual(cli(["review", str(a), str(b), "--base-commit", "a" * 40,
                                      "--candidate-commit", "b" * 40, "--base-exit", "1",
                                      "--candidate-exit", "0"]), 3)

    def test_unavailable_or_noncallable_adapter_leaves_destination_reusable(self):
        with TemporaryDirectory() as tmp, redirect_stderr(StringIO()):
            target = Path(tmp, "evidence")
            for schedule in ("single", "all"):
                for name in ("setrya_missing_test_module:Application", "setrya.scenarios:FORMAT"):
                    with self.subTest(schedule=schedule, factory=name):
                        self.assertEqual(main(["delete", "--schedule", schedule, "--adapter", name,
                                               "--output", str(target)]), 2)
                        self.assertFalse(target.exists())

    def test_invalid_revision_does_not_consume_output_path(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp, "single")
            with self.assertRaises(InputError):
                run_scenario("update", "single", target, revision="short")
            self.assertFalse(target.exists())
