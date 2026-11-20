from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.cli import main
from setrya.examples.access_demo import RECORDINGS


class AccessDemoTests(unittest.TestCase):
    def test_offline_first_run_shows_what_each_user_received(self):
        with TemporaryDirectory() as directory, chdir(directory), redirect_stdout(StringIO()) as output:
            with patch("setrya.cli.preflight", side_effect=AssertionError("unexpected SQLite fixture")):
                self.assertEqual(main(["demo", "access"]), 0)
            self.assertEqual(list(Path.cwd().iterdir()), [])
        intro, cases = output.getvalue().split("Broken cache · FAIL")
        broken, cases = cases.split("Guarded cache · PASS")
        guarded, missing = cases.split("Missing serving evidence · NOT TESTED")
        self.assertIn("Offline replay of synthetic", intro)
        reader_public = "Revoked user (case-reader) · The public refund window is 14 days. · cache miss"
        reader_staff = "Revoked user (case-reader) · The staff refund window is 45 days. · cache hit"
        editor_staff = "Retained colleague (case-editor) · The staff refund window is 45 days. · cache hit"
        for case, outcome, final_answer in ((broken, "committed", reader_staff), (guarded, "rejected", reader_public)):
            paused, final = case.split(f"After worker resumed, late write {outcome}")
            self.assertIn("After revocation, worker paused", paused)
            self.assertIn(reader_public, paused)
            self.assertIn(final_answer, final)
            self.assertIn(editor_staff, paused)
            self.assertIn(editor_staff, final)
        self.assertIn("authenticated caller and route", missing)
        self.assertNotIn("After worker resumed", missing)
        self.assertTrue(missing.rstrip().endswith("setrya init endpoint"))

    def test_damaged_recording_cannot_print_a_successful_demo(self):
        with TemporaryDirectory() as directory:
            recordings = shutil.copytree(RECORDINGS, Path(directory, "recordings"))
            journal = recordings / "guarded" / "run.json"
            journal.write_bytes(journal.read_bytes() + b"\n")
            with patch("setrya.examples.access_demo.RECORDINGS", recordings), redirect_stdout(StringIO()) as out:
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(main(["demo", "access"]), 2)
            self.assertIn("ERROR", errors.getvalue())
            self.assertNotIn("Guarded cache · PASS", out.getvalue())

    def test_existing_search_demo_selection(self):
        for args, selected in (([], None), (["--case", "opaque"], "opaque")):
            with patch("setrya.cli.preflight") as preflight, patch("setrya.cli.demo", return_value=0) as demo:
                self.assertEqual(main(["demo", *args]), 0)
                preflight.assert_called_once_with()
                demo.assert_called_once_with(selected)
