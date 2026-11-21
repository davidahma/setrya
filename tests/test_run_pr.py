"""Exercise checkout provenance and review artifacts with two local Git commits.

A native SDK command drives a local adapter whose worker behavior differs by
checkout. The installed CI example separately exercises the pytest fixture.
"""
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from setrya.run_pr import main


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "setrya/examples/support-search/python.toml"
ADAPTER = """
from dataclasses import replace
import os
from pathlib import Path
from setrya.examples.support_search.adapter import python_fixture

def factory(context):
    mode = "broken" if Path("lane").read_text() == "base" else "fixed"
    if os.environ.get("TEST_OPAQUE") == "1":
        mode = "opaque"
    return python_fixture(replace(context, drill=replace(context.drill, options={"mode": mode, "final_reads": "after-receipt"})))
"""

DRIVER = """
from dataclasses import replace
import json, os, subprocess, sys, time
from pathlib import Path
sys.path[:0] = [str(Path.cwd()), sys.argv[1]]
# The stdlib CI matrix runs directly from source. Give the real fixture's
# separate Python worker access to that same SDK and fixture package.
os.environ["PYTHONPATH"] = sys.argv[1]
from setrya.drill import Drill
from setrya.runner import run

lane = Path("lane").read_text()
source = Path(os.environ["SETRYA_REVIEW_DRILL"])
assert source.read_bytes() == Path(sys.argv[2]).read_bytes()
revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert revision == os.environ["SETRYA_REVIEW_REVISION"]
label = sys.argv[3]
if label == "missing" and lane == "base": sys.exit(5)
if label == "slow" and lane == "base": time.sleep(30)
drill = Drill.load(source)
if label == "other-factory": drill = replace(drill, factory="setrya.examples.support_search.adapter:python_fixture")
if label == "other-options": drill = replace(drill, options={"mode": "other"})
if label == "opaque": os.environ["TEST_OPAQUE"] = "1"
result = run(drill, revision=revision)
target = Path(os.environ["SETRYA_REVIEW_REPORT"])
result.report.save(target)
if label == "slow-teardown" and lane == "base": time.sleep(30)
if label == "dirty" and lane == "candidate": Path("lane").write_text("changed")
report = json.loads(target.read_bytes())
if label == "wrong" and lane == "candidate": report["rules"]["document"] = "other"
if label == "stale" and lane == "candidate": report["inputs"]["revision"] = "a" * 40
if label == "wrong-identity": report["inputs"]["adapter"] = "f" * 64
target.write_text(json.dumps(report))
print("synthetic native output only")
sys.exit(result.verdict.status.exit_code)
"""


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class RunPrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = tempfile.TemporaryDirectory()
        cls.addClassCleanup(root.cleanup)
        cls.root = Path(root.name).resolve()
        cls.driver = cls.root / "trusted.py"
        cls.driver.write_text(DRIVER)
        cls.repo = cls.root / "subject"
        cls.repo.mkdir()
        git(cls.repo, "init", "-q")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "user.email", "fixture@example.invalid")
        git(cls.repo, "config", "commit.gpgsign", "false")
        cls.drill = cls.root / "selected.toml"
        cls.drill.write_text(EXAMPLE.read_text().replace("setrya.examples.support_search.adapter:python_fixture",
                                                        "localadapter:factory"))
        (cls.repo / "localadapter.py").write_text(ADAPTER)
        git(cls.repo, "add", "localadapter.py")
        for name in ("base", "candidate"):
            (cls.repo / "lane").write_text(name)
            git(cls.repo, "add", "lane")
            git(cls.repo, "commit", "-qm", name)
            git(cls.repo, "tag", name)
        (cls.repo / "lane").write_text("dirty user work")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name) / "evidence"

    def check(self, kind="normal", base="base", candidate="candidate", timeout=15, repo=None, exit_contract="pytest"):
        stream = StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            code = main(["--repo", str(repo or self.repo), "--base", base, "--candidate", candidate,
                         "--exit-contract", exit_contract, "--drill", str(self.drill), "--timeout", str(timeout), "--output", str(self.out),
                         "--", sys.executable, str(self.driver), str(ROOT), str(self.drill), kind])
        return code

    def test_handoff_binds_real_commits_exits_and_replays_after_clones_are_gone(self):
        nested = self.repo / "tests"
        nested.mkdir(exist_ok=True)
        self.assertEqual(self.check(repo=nested), 0)
        summary = (self.out / "review.md").read_text()
        self.assertIn("FIX VERIFIED", summary)
        self.assertNotIn(str(self.root), summary)
        self.assertNotIn("synthetic native output", summary)
        self.assertEqual({p.name for p in self.out.iterdir()}, {"run-pr.json", "base.json", "candidate.json", "review.md"})
        metadata = json.loads((self.out / "run-pr.json").read_bytes())
        self.assertEqual(metadata["drill_sha256"], sha256(self.drill.read_bytes()).hexdigest())
        for name, code in (("base", 1), ("candidate", 0)):
            self.assertEqual(metadata[name], {"commit": git(self.repo, "rev-parse", name), "test_exit": code,
                                             "report_sha256": sha256((self.out / f"{name}.json").read_bytes()).hexdigest()})
        self.assertEqual((self.repo / "lane").read_text(), "dirty user work")
        self.assertEqual(self.check(), 2)
        self.assertEqual((self.out / "review.md").read_text(), summary)

    def test_invalid_or_equal_refs_do_not_consume_an_output_path(self):
        for name in ("unresolved", "base"):
            with self.subTest(ref=name):
                self.assertEqual(self.check(candidate=name), 2)
                self.assertFalse(self.out.exists())

    def test_mismatched_commit_drill_or_changed_checkout_cannot_confirm_a_fix(self):
        for mode, code, reason in (("stale", 3, "expected commit"), ("wrong", 2, "selected drill"),
                                   ("dirty", 2, "tracked files"), ("wrong-identity", 2, "live SDK run"),
                                   ("other-factory", 2, "did not produce"), ("other-options", 2, "did not produce")):
            with self.subTest(mode=mode):
                self.assertEqual(self.check(mode), code)
                summary = (self.out / "review.md").read_text()
                self.assertNotIn("FIX VERIFIED", summary)
                self.assertIn(reason, summary)
                shutil.rmtree(self.out)

    def test_timeout_or_missing_base_still_runs_candidate(self):
        for mode, native_exit in (("slow", None), ("missing", 5)):
            with self.subTest(mode=mode):
                self.assertEqual(self.check(mode, timeout=1), 2)
                metadata = json.loads((self.out / "run-pr.json").read_bytes())
                self.assertEqual(metadata["base"]["test_exit"], native_exit)
                self.assertIsNone(metadata["base"]["report_sha256"])
                self.assertTrue((self.out / "candidate.json").is_file())
                self.assertIn("ERROR", (self.out / "review.md").read_text())
                shutil.rmtree(self.out)

    def test_search_timeout_after_recording_keeps_the_base_failure(self):
        self.assertEqual(self.check("slow-teardown", timeout=3), 2)
        metadata = json.loads((self.out / "run-pr.json").read_bytes())
        base = (self.out / "base.json").read_bytes()
        self.assertEqual(metadata["base"]["report_sha256"], sha256(base).hexdigest())
        self.assertIsNone(metadata["base"]["test_exit"])
        self.assertEqual(metadata["candidate"]["test_exit"], 0)
        summary = (self.out / "review.md").read_text()
        self.assertIn("command timed out after recording", summary)
        self.assertNotIn("FIX VERIFIED", summary)

    def test_setrya_native_inconclusive_exits_survive_offline_review(self):
        from setrya.cli import main as cli
        import shlex

        for contract, code in (("pytest", 2), ("setrya", 3)):
            with self.subTest(contract=contract):
                self.assertEqual(self.check("opaque", exit_contract=contract), code)
                metadata = json.loads((self.out / "run-pr.json").read_bytes())
                self.assertEqual(metadata["exit_contract"], contract)
                self.assertEqual([metadata[lane]["test_exit"] for lane in ("base", "candidate")], [3, 3])
                line = next(line for line in (self.out / "review.md").read_text().splitlines()
                            if line.startswith("Review again  "))
                args = shlex.split(line.removeprefix("Review again  "))[1:]
                args[1:3] = [str(self.out / args[1]), str(self.out / args[2])]
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(cli(args), code)
                shutil.rmtree(self.out)
