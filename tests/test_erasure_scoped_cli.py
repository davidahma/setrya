"""The unified CLI preserves native help, v2 mode and verified exit codes."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.erasure.cli import main
from setrya.erasure.starter import init_erasure


class ScopedCLI(unittest.TestCase):
    def test_tenant_help_names_runnable_public_commands(self):
        for action, legacy in (("run", "gate"), ("replay", "verify-receipt")):
            with self.subTest(action=action), redirect_stdout(StringIO()) as out:
                with self.assertRaises(SystemExit) as done:
                    main([action, "--scope", "tenant", "--help"])
                self.assertEqual(done.exception.code, 0)
                usage = out.getvalue().splitlines()[0]
                self.assertIn(f"setrya erasure {action} --scope tenant", usage)
                self.assertNotIn(legacy, usage)

    def test_unconnected_legacy_starter_requires_late_write(self):
        with TemporaryDirectory() as tmp:
            starter = init_erasure(Path(tmp) / "starter") / "test_erasure.py"
            args = ["run", "--starter", str(starter), "--output", f"{tmp}/output"]
            self.assertEqual(main([*args, "--mode", "deletion"]), 2)
            self.assertEqual(main([*args, "--mode", "late-write"]), 3)

    def test_no_trusted_pass_on_starter_exit_or_mode_mismatch(self):
        with TemporaryDirectory() as tmp:
            starter = Path(tmp) / "starter.py"
            starter.write_text("print('unverified starter output')\n")
            args = ["run", "--starter", str(starter), "--output", f"{tmp}/output"]
            for mode, verdict in (("late-write", "PASS"), ("deletion", "FAIL")):
                receipt = {"version": 3, "mode": mode, "technical_verdict": verdict}
                with self.subTest(mode=mode), patch("setrya.erasure.cli.load_directory", return_value=receipt):
                    with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                        self.assertEqual(main(args), 2)
                        self.assertEqual(out.getvalue(), "")
                        self.assertIn("Starter exit or recorded mode", err.getvalue())
