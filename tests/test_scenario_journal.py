"""Replay a suite when its producer stops between schedules."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest
from unittest.mock import patch

from setrya.checker import Status
from setrya.scenario_fixture import Application
from setrya.scenarios import SCHEDULES, main, replay


class ScenarioJournalTests(unittest.TestCase):
    def test_terminated_producer_preserves_every_announced_result(self):
        script = dedent("""
            import os
            import sys
            from unittest.mock import patch
            from setrya import scenarios

            run = scenarios.run_scenario

            def stop_at_second_schedule(kind, schedule, *args, **kwargs):
                if schedule == "retry":
                    os._exit(19)
                return run(kind, schedule, *args, **kwargs)

            with patch.object(scenarios, "run_scenario", stop_at_second_schedule):
                sys.exit(scenarios.main(sys.argv[1:]))
        """)
        with TemporaryDirectory() as tmp:
            for mode, first, expected in (("broken", "FAIL", Status.FAIL), ("fixed", "PASS", Status.NOT_TESTED)):
                with self.subTest(mode=mode):
                    directory = Path(tmp, mode)
                    proc = subprocess.run(
                        [sys.executable, "-c", script, "delete", "--mode", mode, "--schedule", "all",
                         "--revision", "a" * 40, "--output", str(directory)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(proc.returncode, 19, proc.stderr)
                    self.assertIn(f"single  {first}", proc.stdout)
                    meta = json.loads((directory / "scenario.json").read_text())
                    self.assertEqual(meta["revision"], "a" * 40)
                    self.assertEqual(set(meta["members"]), set(SCHEDULES))
                    self.assertEqual({k for k, v in meta["members"].items() if v is not None}, {"single"})
                    self.assertIs(replay(directory).status, expected)

    def test_interruption_before_first_attempt_has_an_unfinished_manifest(self):
        with TemporaryDirectory() as tmp, patch("setrya.scenarios.run_scenario", side_effect=KeyboardInterrupt):
            directory = Path(tmp, "suite")
            with self.assertRaises(KeyboardInterrupt):
                main(["delete", "--schedule", "all", "--output", str(directory)])
            self.assertIs(replay(directory).status, Status.NOT_TESTED)

    def test_interrupt_during_an_adapter_call_stops_subsequent_schedules(self):
        with (TemporaryDirectory() as tmp, patch.object(Application, "seed", side_effect=KeyboardInterrupt),
              redirect_stdout(StringIO())):
            directory = Path(tmp, "suite")
            self.assertEqual(main(["delete", "--schedule", "all", "--output", str(directory)]), Status.ERROR.exit_code)
            self.assertIs(replay(directory).status, Status.ERROR)
            self.assertFalse((directory / "retry").exists())
