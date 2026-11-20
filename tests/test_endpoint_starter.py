from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import unittest

from setrya.cli import main


class StarterTests(unittest.TestCase):
    def test_custom_path_is_quoted_and_an_existing_test_is_preserved(self):
        with TemporaryDirectory() as directory:
            target = Path(directory, "test suite", "test_endpoint.py")
            args = ["init", "endpoint", "--output", str(target)]
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args), 0)
            command = shlex.split(output.getvalue().splitlines()[-1].removeprefix("Run  "))
            self.assertEqual(command, ["python", "-m", "pytest", "-q", "-s", "--", str(target)])

            target.write_text("def test_ours(): pass\n")
            with redirect_stderr(StringIO()) as errors:
                self.assertEqual(main(args), 2)
            self.assertIn("already exists", errors.getvalue())
            self.assertEqual(target.read_text(), "def test_ours(): pass\n")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_dangling_destination_link_does_not_redirect_generation(self):
        with TemporaryDirectory() as directory:
            other = Path(directory, "other.py")
            target = Path(directory, "test_endpoint.py")
            target.symlink_to(other)
            with redirect_stderr(StringIO()) as errors:
                self.assertEqual(main(["init", "endpoint", "--output", str(target)]), 2)
            self.assertIn("already exists", errors.getvalue())
            self.assertTrue(target.is_symlink())
            self.assertFalse(other.exists())
