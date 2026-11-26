"""Exercise the generated TypeScript app through the public HTTP drill."""

import json
import re
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from shutil import which
from subprocess import run
from tempfile import TemporaryDirectory


@unittest.skipUnless(find_spec("yaml"), "install setrya[case-yaml]")
class TypeScriptStarterTests(unittest.TestCase):
    def setUp(self):
        node = which("node")
        if not node:
            self.skipTest("install Node.js 24 LTS or later")
        version = run([node, "--version"], check=False, text=True, capture_output=True, timeout=5)
        major = re.match(r"v([0-9]+)\.", version.stdout)
        if version.returncode or not major or int(major[1]) < 24:
            self.skipTest("install Node.js 24 LTS or later")

    def cli(self, cwd, code, *args):
        result = run([sys.executable, "-I", *map(str, args)], check=False, cwd=cwd,
                     text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    def check_case(self, lane, code, verdict):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "my TypeScript fixture"
            init = self.cli(root, 0, "-m", "setrya", "init", "--stack", "typescript", "--output", path)
            self.assertIn("run.py", init)
            self.assertEqual({p.name for p in path.iterdir()}, {"case.yml", "fixture.mts", "run.py"})
            output = root / lane
            trace = self.cli(root, code, path / "run.py", "--mode", lane, "--output", output)
            self.assertIn("READY  Read 3 declared keys and all declared questions for 2 caller(s).", trace)
            replay = self.cli(root, code, "-m", "setrya", "replay", output)
            self.assertIn(f"Technical verdict {verdict}", replay)
            evidence = json.loads((output / "erasure-evidence.json").read_text())

        self.assertTrue(evidence["erased"])
        for stage in ("immediate", "final"):
            with self.subTest(stage=stage):
                stored = {row["key"]: row for row in evidence[stage]["stored"]}
                self.assertFalse(stored["note"]["exists"])
                self.assertEqual(stored["cached-note"]["exists"], bool(code))
                self.assertIs(stored["dispatch"]["control_present"], True)
                self.assertEqual(evidence[stage]["scans"]["memory"], ["cached-note"] if code else [])
                answers = [answer for samples in evidence[stage]["answers"] for answer in samples]
                for caller in ("duty-agent", "duty-reviewer"):
                    reads = [row for row in answers if row["caller"] == caller]
                    self.assertEqual(len(reads), 9)
                    self.assertEqual(sum(row["control_present"] is True for row in reads), 3)
                    self.assertEqual(sum(row["target_present"] is True for row in reads), 6 if code else 0)

    def test_guarded_erasure_replays_pass(self):
        self.check_case("guarded", 0, "PASS")

    def test_cached_residual_replays_fail(self):
        self.check_case("broken", 1, "FAIL")
