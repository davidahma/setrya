"""The unified CLI preserves native help, v2 mode and verified exit codes."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from textwrap import dedent
from unittest.mock import patch

from setrya.erasure.cli import main
from setrya.erasure.starter import init_erasure


class ScopedCLI(unittest.TestCase):
    def test_live_run_is_bound_to_the_evidence_before_fixture_teardown(self):
        with TemporaryDirectory() as tmp:
            starter = Path(tmp) / "starter.py"
            starter.write_text(dedent("""
                from pathlib import Path
                import shutil
                import sys
                from setrya.erasure.v3 import run_shared
                from setrya.examples.workspace import SIMPLE, open_fixture

                output = Path(sys.argv[sys.argv.index("--output") + 1])
                with open_fixture() as (subject, app, items):
                    run_shared(subject, case=SIMPLE, application=app, external_items=items,
                               output=output, mode="deletion", run_id="application-run-id")
                if output.name == "substituted":
                    shutil.rmtree(output)
                    shutil.copytree(output.parent / "original", output)
            """))
            args = ["run", "--starter", str(starter), "--output"]
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as err:
                self.assertEqual(main([*args, f"{tmp}/original"]), 0, err.getvalue())
                self.assertEqual(main([*args, f"{tmp}/substituted"]), 2)
            self.assertIn("does not match evidence issued by this live run", err.getvalue())

    def test_tenant_help_names_runnable_public_commands(self):
        for action, legacy in (("run", "gate"), ("replay", "verify-receipt")):
            with self.subTest(action=action), redirect_stdout(StringIO()) as out:
                with self.assertRaises(SystemExit) as done:
                    main([action, "--scope", "tenant", "--help"])
                self.assertEqual(done.exception.code, 0)
                usage = out.getvalue().splitlines()[0]
                self.assertIn(f"setrya erasure {action} --scope tenant", usage)
                self.assertNotIn(legacy, usage)

    def test_unconnected_v3_starter_is_not_tested_in_either_mode(self):
        with TemporaryDirectory() as tmp:
            starter = init_erasure(Path(tmp) / "starter") / "test_erasure.py"
            args = ["run", "--starter", str(starter), "--output", f"{tmp}/output"]
            self.assertEqual(main([*args, "--mode", "deletion"]), 3)
            self.assertEqual(main([*args, "--mode", "late-write"]), 3)

    def test_no_trusted_pass_on_starter_exit_or_mode_mismatch(self):
        with TemporaryDirectory() as tmp:
            starter = Path(tmp) / "starter.py"
            starter.write_text(
                "import sys\nfrom pathlib import Path\n"
                "output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
                "output.mkdir()\n(output / 'erasure-receipt.json').write_text('{}')\n")
            for mode, verdict in (("late-write", "PASS"), ("deletion", "FAIL")):
                args = ["run", "--starter", str(starter), "--output", f"{tmp}/{mode}"]
                receipt = {"version": 3, "scope": "person", "mode": mode, "technical_verdict": verdict}
                with self.subTest(mode=mode), patch("setrya.erasure.cli.load_directory", return_value=receipt):
                    with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                        self.assertEqual(main(args), 2)
                        self.assertEqual(out.getvalue(), "")
                        self.assertIn("Starter exit or recorded mode", err.getvalue())
