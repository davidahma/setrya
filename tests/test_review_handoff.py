from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.cli import EXAMPLE, main
from setrya.drill import Drill
from setrya.evidence import SCHEMA, Report, bind_inputs
from test_checker import History


class ReviewHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.base = self.directory / "base.json"
        self.candidate = self.directory / "private-name.json"
        self.summary = self.directory / "review.md"
        self.base_commit = "a" * 40
        self.candidate_commit = "b" * 40
        self.options = {"--base-commit": self.base_commit, "--candidate-commit": self.candidate_commit,
                        "--base-exit": "0", "--candidate-exit": "0"}

    def save_passing(self, path, commit):
        history = History()
        history.resume()
        history.receipt("rejected")
        history.probe(80, "final", ["refund@B"])
        history.probe(82, "final", ["delivery@1"], "delivery")
        inputs = bind_inputs(Drill.load(EXAMPLE / "drill.toml"), revision=commit)
        Report("run", history.rules, tuple(history.events), SCHEMA, inputs).save(path)

    def review(self):
        argv = ["review", str(self.base), str(self.candidate), "--summary", str(self.summary)]
        for option, value in self.options.items():
            argv.extend([option, value])
        captured = StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            code = main(argv)
        self.assertEqual(code, 2)
        return captured.getvalue()

    def test_missing_or_invalid_report_replaces_stale_green_and_keeps_other_evidence(self):
        for missing, bad in (("Base", False), ("Candidate", False), ("Candidate", True)):
            with self.subTest(missing=missing, malformed=bad):
                self.base.unlink(missing_ok=True)
                self.candidate.unlink(missing_ok=True)
                if missing == "Base":
                    self.save_passing(self.candidate, self.candidate_commit)
                    other, invalid_file = "Candidate", self.base
                else:
                    self.save_passing(self.base, self.base_commit)
                    other, invalid_file = "Base", self.candidate
                if bad:
                    invalid_file.write_text("{private report payload")
                self.options[f"--{missing.lower()}-exit"] = "4"
                self.summary.write_text("FIX VERIFIED  previous CI run")
                captured = self.review()
                self.options[f"--{missing.lower()}-exit"] = "0"
                text = self.summary.read_text()
                self.assertTrue(captured.startswith("ERROR"))
                self.assertNotIn("FIX VERIFIED", text)
                self.assertIn(f"{missing} report is " + ("unreadable or invalid" if bad else "missing"), text)
                self.assertIn(f"{other} recorded PASS", text)
                self.assertIn(f"{missing} commit {self.options[f'--{missing.lower()}-commit']} · test exit 4", text)
                self.assertIn(f"Replay {other.lower()} ", text)
                self.assertNotIn(f"Replay {missing.lower()} ", text)
                for private_value in (self.candidate.name, str(self.directory), "private report payload"):
                    self.assertNotIn(private_value, captured + text)

    def test_two_missing_reports_and_invalid_metadata_leave_an_error_handoff(self):
        for option, value, expected in (("--candidate-commit", "private-input", "Candidate commit (invalid)"),
                                        ("--candidate-exit", "private-input", "test exit (invalid)")):
            with self.subTest(option=option):
                original = self.options[option]
                self.options[option] = value
                self.review()
                text = self.summary.read_text()
                self.assertIn(expected, text)
                self.assertIn("Base report is missing", text)
                self.assertIn("Candidate report is missing", text)
                self.assertNotIn("Replay ", text)
                self.assertNotIn(value, text)
                self.options[option] = original

    def test_existing_reports_with_invalid_review_metadata_replace_stale_summary(self):
        self.save_passing(self.base, self.base_commit)
        self.save_passing(self.candidate, self.candidate_commit)
        for option, value, diagnostic in (("--candidate-commit", self.base_commit, "two different Git commits"),
                                          ("--base-commit", "private-input", "full lowercase Git commit"),
                                          ("--candidate-exit", None, "numeric test exit")):
            with self.subTest(option=option):
                original = self.options.pop(option)
                if value is not None:
                    self.options[option] = value
                self.summary.write_text("FIX VERIFIED")
                self.review()
                text = self.summary.read_text()
                self.assertIn(diagnostic, text)
                self.assertIn("Candidate recorded PASS", text)
                self.assertNotIn("FIX VERIFIED", text)
                self.assertNotIn("private-input", text)
                self.options[option] = original

    def test_summary_alias_cannot_overwrite_an_input(self):
        self.save_passing(self.base, self.base_commit)
        expected = self.base.read_bytes()
        self.summary.symlink_to(self.base)
        self.assertIn("distinct file", self.review())
        self.assertEqual(self.base.read_bytes(), expected)
