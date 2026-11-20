"""Progress from real HTTP sessions to the worker on both generated starters."""

from contextlib import contextmanager
from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import setrya
from setrya.drill import InputError
from setrya.endpoint import EndpointCase, check_endpoint_reads
from setrya.endpoint_reads import MANIFEST, replay
from setrya.status import Status


CASE = EndpointCase("answer", "reader", "owner", "question", "staff", "guide", "secret answer", "public")
FIXTURES = Path(__file__).with_name("fixtures")


class ReadChecks(unittest.TestCase):
    def test_each_call_checks_the_serving_observations_and_protects_unexpected_values(self):
        expected = dict(http_status=200, caller=CASE.revoked, route=CASE.route,
                        query_sha256=sha256(CASE.question.encode()).hexdigest(), answer=CASE.private_answer)
        for change, reason in (
            ({"http_status": 403}, "HTTP 403"), ({"caller": CASE.retained}, "served as owner"),
            ({"route": "unrelated-secret"}, "missing route answer"),
            ({"query_sha256": "b" * 64}, "missing question"), ({"answer": "unrelated-secret"}, "missing expected private answer"),
            ({"caller": None}, "missing authenticated caller"),
        ):
            with self.subTest(change=change), TemporaryDirectory() as tmp:
                calls = []

                class Sessions:
                    def answer(self, caller, question):
                        calls.append((caller, question))
                        return {**expected, "caller": caller, **(change if caller == CASE.revoked else {})}

                @contextmanager
                def fixture(_):
                    yield Sessions()

                out = Path(tmp, "reads")
                actual = check_endpoint_reads(CASE, fixture, out)
                self.assertEqual(calls, [(CASE.revoked, CASE.question), (CASE.retained, CASE.question)])
                self.assertEqual(actual, replay(out))
                verdict, text = actual
                self.assertIs(verdict.status, Status.NOT_TESTED)
                self.assertIn(reason, text)
                self.assertIn("owner · confirmed HTTP 200, authenticated caller, route answer, question, expected private answer", text)
                self.assertNotIn("unrelated-secret", (out / MANIFEST).read_text())
                self.assertNotIn(CASE.private_answer, (out / MANIFEST).read_text())
                path = out / MANIFEST
                data = json.loads(path.read_text())
                data["status"] = "PASS"
                path.write_text(json.dumps(data))
                with self.assertRaises(InputError):
                    replay(out)


# The fixture owns already prepared synthetic sessions. The object yielded to
# Setrya exposes only an authenticated HTTP read until the test connects seed.
READS_FIXTURE = """
from contextlib import contextmanager
import os
from approval_app import Application as HTTPApp, CASE

@contextmanager
def sessions(run_id):
    with HTTPApp(run_id) as app:
        app.seed()

        class Sessions:
            def answer(self, caller, question):
                if os.environ.get("TEST_WRONG_SESSION") and caller == CASE.revoked:
                    caller = CASE.retained
                return app.answer(caller, question)

        fixture = Sessions()
        if os.environ.get("TEST_WARM_ENTRIES"):
            fixture.seed = app.seed
        yield fixture
"""


@unittest.skipUnless(find_spec("pytest"), "requires pytest to run the generated consumer test")
class ReadOnboardingTests(unittest.TestCase):
    def test_both_generated_starters_work_with_only_authenticated_reads(self):
        for framework, name in (("generic", "Application"), ("celery-langchain", "open_test_app")):
            with self.subTest(framework=framework), TemporaryDirectory() as directory:
                app = Path(directory)
                env = dict(os.environ, PYTHONPATH=str(Path(setrya.__file__).resolve().parents[1]),
                           PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", APP_TEST_MODE="guarded")

                def run(*args, code, **settings):
                    result = subprocess.run([sys.executable, "-m", *args], cwd=app, env={**env, **settings},
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    return result.stdout

                init = run("setrya", "init", "endpoint", "--framework", framework, code=0)
                self.assertIn("--setrya-reads-only", init)
                test = app / "tests/test_setrya_endpoint.py"
                shutil.copyfile(FIXTURES / "endpoint_app.py", test.with_name("approval_app.py"))
                with test.open("a") as source:
                    source.write(READS_FIXTURE + f"\n{name} = sessions\n")

                for mode, settings in (("ready", {}), ("wrong", {"TEST_WRONG_SESSION": "1"})):
                    dest = app / mode
                    output = run("pytest", "-q", "-s", "-p", "setrya.pytest_plugin", "--setrya-reads-only",
                                 "--basetemp", str(dest), str(test), code=1, **settings)
                    self.assertIn("setrya replay", output)
                    manifest, = dest.rglob(MANIFEST)
                    self.assertFalse(list(dest.rglob("answer-cache.json")))
                    for command in ("replay", "diagnose"):
                        review = run("setrya", command, str(manifest.parent), code=3)
                        self.assertTrue(review.startswith("NOT TESTED"), review)
                        self.assertIn("owner · confirmed HTTP 200, authenticated caller", review)
                        self.assertIn("Both endpoint reads are confirmed" if mode == "ready" else "served as owner", review)

                full = run("pytest", "-q", "-s", str(test), code=1)
                self.assertIn("Connect seed()", full)
                warm = run("pytest", "-q", "-s", str(test), code=1, TEST_WARM_ENTRIES="1")
                self.assertIn("Connect start_old()", warm)
                self.assertEqual(warm.count("question and expected private answer confirmed"), 2)
