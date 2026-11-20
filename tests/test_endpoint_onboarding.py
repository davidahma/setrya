"""An adopter connects a generated test to their own HTTP app and hands off the evidence."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import setrya


HERE = Path(__file__).resolve().parent


@unittest.skipUnless(importlib.util.find_spec("pytest"), "install the pytest extra for the starter handoff")
class EndpointOnboardingTests(unittest.TestCase):
    def test_generated_pytest_exercises_private_and_retained_answers(self):
        with TemporaryDirectory() as project, TemporaryDirectory() as reviewer:
            app = Path(project)
            handoff = Path(reviewer)
            env = {**os.environ, "PYTHONPATH": str(Path(setrya.__file__).resolve().parents[1]), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

            def command(*args, cwd=app):
                return subprocess.run([sys.executable, "-m", *args], cwd=cwd, env=env,
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      timeout=30)

            init = command("setrya", "init", "endpoint")
            self.assertEqual(init.returncode, 0, init.stdout)
            test = app / "tests" / "test_setrya_endpoint.py"
            native_app = test.with_name("approval_app.py")
            shutil.copyfile(HERE / "fixtures" / "endpoint_app.py", native_app)
            with test.open("a") as source:
                source.write("\n# Use the application's disposable test fixture.\n"
                             "from approval_app import Application, CASE\n")

            for mode, pytest_exit in [("broken", 1), ("guarded", 0), ("wrong-principal", 1)]:
                with self.subTest(mode=mode):
                    env["APP_TEST_MODE"] = mode
                    reports = app / ("results-" + mode)
                    check = command("pytest", "-q", "-s", str(test), "--basetemp", str(reports))
                    self.assertEqual(check.returncode, pytest_exit, check.stdout)
                    self.assertIn("setrya replay", check.stdout)
                    manifests = list(reports.rglob("answer-cache.json"))
                    self.assertEqual(len(manifests), 1, check.stdout)
                    shutil.copytree(manifests[0].parent, handoff / mode)
                    for artifact in (handoff / mode).glob("*.json"):
                        text = artifact.read_text()
                        for sensitive in ("Which approval level applies?", "The synthetic threshold is seventeen.",
                                          "Ask the test supervisor.", "Bearer "):
                            self.assertNotIn(sensitive, text)

            # The fixture and its HTTP server are unavailable to the reviewer.
            native_app.unlink()
            for mode, exit_code, status in [("broken", 1, "FAIL"), ("guarded", 0, "PASS"),
                                            ("wrong-principal", 3, "NOT TESTED")]:
                for subcommand in ("replay", "diagnose"):
                    review = command("setrya", subcommand, mode, cwd=handoff)
                    self.assertEqual(review.returncode, exit_code, review.stdout)
                    self.assertTrue(review.stdout.startswith(status), review.stdout)
                    if mode == "broken":
                        self.assertIn("job-73", review.stdout)
                    if mode == "guarded":
                        self.assertIn("owner", review.stdout)
                        self.assertIn("reviewer", review.stdout)

            destination = os.environ.get("SETRYA_ENDPOINT_EVIDENCE")
            if destination:
                shutil.copytree(handoff, destination)


if __name__ == "__main__":
    unittest.main()
