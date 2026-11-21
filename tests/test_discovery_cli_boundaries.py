"""An early fixture exit cannot look like a passing discovery case in CI."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from setrya.discovery.cli import main
from setrya.discovery.demo import init_example


class DiscoveryFixtureExitTests(TestCase):
    def test_masks_neither_early_exit_nor_suppressed_test_error(self):
        fixtures = (
            "raise SystemExit(0)",
            "from contextlib import contextmanager\n@contextmanager\ndef open_fixture(binding):\n"
            "    try:\n        yield {}\n    except TypeError:\n        pass",
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = init_example(root / "app")
            fixture = sample / "fixture.py"
            for text in fixtures:
                with self.subTest(fixture=text), redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
                    fixture.write_text(text)
                    code = main(["run", "--finding", str(sample / "finding.json"),
                                 "--mapping", str(sample / "mapping.json"), "--fixture", str(fixture),
                                 "--output", str(root / "case")])
                    self.assertEqual(code, 2)
                    self.assertNotIn("PASS", out.getvalue())
                    self.assertFalse((root / "case").exists())
