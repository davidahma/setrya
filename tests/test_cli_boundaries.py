from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.cli import EXAMPLE, _write_summary, console_main, main
from setrya.drill import InputError
from setrya.evidence import Report
from test_trust import EvidenceFixture


class CliBoundaryTests(unittest.TestCase):
    def test_only_live_console_checks_add_the_working_checkout_to_imports(self):
        for command in ("replay", "diagnose", "review", "compare", "check"):
            with self.subTest(command=command), patch.object(sys, "argv", ["setrya", command]):
                with patch.object(sys, "path", ["installed-packages"]):
                    def inspect_path():
                        self.assertEqual(str(Path.cwd()) in sys.path, command == "check")
                        return 0

                    with patch("setrya.cli.main", side_effect=inspect_path):
                        self.assertEqual(console_main(), 0)
                    self.assertEqual(sys.path, ["installed-packages"])

    def test_live_custom_adapter_can_change_directory_without_redirecting_outputs(self):
        with TemporaryDirectory() as temp, chdir(temp), redirect_stdout(StringIO()):
            app = Path(temp, "app")
            app.mkdir()
            drill = app / "config.toml"
            content = (EXAMPLE / "drill.toml").read_text().replace(
                "setrya.examples.support_search.adapter:http_fixture", "test_trust:fixture"
            )
            drill.write_text(content)
            with patch.object(EvidenceFixture, "close", side_effect=lambda: os.chdir(app)), patch(
                "setrya.cli.preflight", side_effect=AssertionError("custom adapter does not use FTS5")
            ):
                self.assertEqual(main(["check", str(drill), "--report", "config.toml", "--summary", "check.md"]), 0)
            self.assertEqual(drill.read_text(), content)
            Report.load(Path(temp, "config.toml"))
            self.assertIn("PASS", Path(temp, "check.md").read_text())
            self.assertFalse((app / "check.md").exists())

    def test_pgvector_qualification_has_no_fts5_precondition(self):
        with patch("setrya.cli.preflight", side_effect=AssertionError), patch(
            "setrya.qualification.qualify", return_value=0
        ) as qualify:
            self.assertEqual(main(["qualify", "--case", "pgvector"]), 0)
            qualify.assert_called_once_with("pgvector", False)

    def test_check_and_compare_replace_a_stale_summary_when_input_is_missing(self):
        with TemporaryDirectory() as temp, chdir(temp), redirect_stderr(StringIO()):
            summary = Path("result.md")
            for command in (["check", "missing.toml"], ["compare", "missing.json", "other.json"]):
                with self.subTest(command=command[0]):
                    summary.write_text("FIX VERIFIED")
                    self.assertEqual(main([*command, "--summary", str(summary)]), 2)
                    self.assertIn("ERROR", summary.read_text())
                    self.assertNotIn("FIX VERIFIED", summary.read_text())

    def test_reviewers_reject_a_directory_containing_two_verdicts(self):
        from setrya.scenarios import inspect_scenario

        with TemporaryDirectory() as directory:
            for name in ("answer-cache.json", "scenario.json"):
                Path(directory, name).write_text("{}")
            for command in ("replay", "diagnose"):
                with self.subTest(command=command), redirect_stderr(StringIO()) as errors:
                    self.assertEqual(main([command, directory]), 2)
                    self.assertIn("ambiguous evidence directory", errors.getvalue())
            with self.assertRaisesRegex(InputError, "ambiguous evidence directory"):
                inspect_scenario(directory)

    def test_summary_destination_must_be_a_regular_file(self):
        with TemporaryDirectory() as directory:
            summary = Path(directory, "summary.md")
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaisesRegex(InputError, "regular file"):
                    _write_summary(summary, "release review", "result")
            self.assertFalse(summary.exists())
