"""External adopter and real reopened chat acceptance. Run with python -m pip install '.[erasure]'."""

from __future__ import annotations

from dataclasses import replace
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.erasure.demo import ALICE, BOB, CALLERS, ChartApplication, run_case
from setrya.erasure.starter import init_erasure
from setrya.erasure.worker_fixture import WorkerFixture


def _langgraph_installed():
    try:
        return find_spec("langgraph.graph") is not None and find_spec("langgraph.checkpoint.sqlite") is not None
    except ModuleNotFoundError:
        return False


def _run(*args, cwd=None, env=None):
    # The base CI matrix runs from a source checkout without installing Setrya.
    child = dict(os.environ if env is None else env)
    child["PYTHONPATH"] = os.pathsep.join(filter(None, (str(Path(__file__).resolve().parents[1]), child.get("PYTHONPATH"))))
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, timeout=60, cwd=cwd, env=child)


class StarterTests(unittest.TestCase):
    def test_unconnected_starter_cannot_pass_or_overwrite_work(self):
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "shop"
            init_erasure(path)
            result = _run("test_erasure.py", "--output", "run", cwd=path)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("open_fixture()", result.stderr)
            self.assertFalse((path / "run").exists())
            (path / "inventory.toml").write_text("my changes", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                init_erasure(path)
            self.assertEqual((path / "inventory.toml").read_text(), "my changes")

    def test_worker_finishing_without_checkpoint_and_double_dispatch_are_rejected(self):
        with WorkerFixture(lambda *_: None, lambda *_: None) as worker:
            with self.assertRaisesRegex(RuntimeError, "without a live"):
                worker.hold(None)
            with self.assertRaisesRegex(RuntimeError, "already dispatched"):
                worker.hold(None)


@unittest.skipUnless(_langgraph_installed(), "install the erasure extra for the live LangGraph trial")
class ErasureAcceptanceTests(unittest.TestCase):
    def _check_bundle(self, folder, expected, unknown):
        from setrya.erasure.receipt import replay

        receipt = replay(folder)
        self.assertEqual(receipt["verdict"], expected, receipt)
        self.assertEqual(receipt["unknown_copies"], unknown, receipt)
        self.assertGreaterEqual(receipt["time_to_first_verdict_ms"], 0)
        evidence_file = folder / "erasure-evidence.json"
        evidence = json.loads(evidence_file.read_bytes())
        self.assertIs(evidence["worker"]["drained"], True)
        self.assertEqual(evidence["worker"]["held"], evidence["worker"]["pre_release"])
        self.assertTrue(evidence["worker"]["held"])
        raw = evidence_file.read_text() + (folder / "erasure-receipt.json").read_text()
        for sensitive in ("fictional-alice", "fictional-bob", "fictional-record-17", ALICE, BOB, "Bearer ", "Synthetic dock"):
            self.assertNotIn(sensitive, raw)
        for stage, reads in evidence["reads"].items():
            self.assertEqual([row["caller"] for row in reads], evidence["case"]["callers"])
            for row in reads:
                self.assertIs(row["control_present"], True, (stage, row))
                self.assertIs(row["subject_present"], stage == "before" or (stage == "final" and expected == "FAIL"))
        return receipt, evidence

    def test_offline_replay_and_real_reopen_after_stale_snapshot(self):
        with TemporaryDirectory() as scratch:
            for mode, expected, count in (("broken", "FAIL", 0), ("guarded", "PASS", 0), ("opaque", "NOT TESTED", 1)):
                with self.subTest(mode=mode):
                    folder = Path(scratch) / mode
                    run_case(folder, guarded=mode != "broken", opaque=mode == "opaque")
                    receipt, evidence = self._check_bundle(folder, expected, count)
                    self.assertEqual(evidence["case"]["callers"], list(CALLERS))
                    statuses = {copy["store"]: copy["status"] for copy in receipt["copies"]}
                    self.assertEqual(statuses["checkpoints"], "still present" if mode == "broken" else "verified gone")
                    if mode == "opaque":
                        self.assertEqual(statuses["model-observer"], "unknown")
                    result = _run("-m", "setrya", "replay", folder)
                    self.assertEqual(result.returncode, {"PASS": 0, "FAIL": 1, "NOT TESTED": 3}[expected], result.stderr)
                    self.assertIn(expected, result.stdout)

    def test_plain_starter_integrates_an_independent_application_through_three_hooks(self):
        with TemporaryDirectory() as scratch:
            folder = Path(scratch) / "integrator"
            init_erasure(folder)
            shutil.copyfile(Path(__file__).parent / "fixtures" / "erasure_shipping_app.py", folder / "dispatch.py")
            script = folder / "test_erasure.py"
            source = script.read_text().replace(
                "def run(output):", "from dispatch import CASE, open_fixture, snapshot_job, read_chat\n\n\ndef run(output):"
            )
            script.write_text(source)
            for setting, expected, code in (("disabled", "FAIL", 1), ("enabled", "PASS", 0)):
                with self.subTest(setting=setting):
                    result = _run("test_erasure.py", "--output", setting, cwd=folder,
                                  env={**os.environ, "DISPATCH_GUARD": setting})
                    self.assertEqual(result.returncode, code, result.stderr + result.stdout)
                    self.assertIn("0 unknown copies", result.stdout)
                    self.assertIn("ms to first verdict", result.stdout)
                    receipt, evidence = self._check_bundle(folder / setting, expected, 0)
                    self.assertEqual(evidence["case"]["callers"], ["coordinator", "dispatcher"])
                    memory = next(row for row in receipt["copies"] if row["store"] == "memory")
                    self.assertEqual(memory["status"], "verified gone" if code == 0 else "still present")

    def test_wrong_principal_does_not_authorize_deleting_even_the_disposable_subject(self):
        actual_read = ChartApplication.read

        def wrong_session(*args):
            return replace(actual_read(*args), caller=CALLERS[0])

        with TemporaryDirectory() as scratch, patch.object(ChartApplication, "read", wrong_session):
            folder = Path(scratch) / "wrong-session"
            receipt = run_case(folder, guarded=True)
            self.assertEqual(receipt["verdict"], "NOT TESTED")
            evidence = json.loads((folder / "erasure-evidence.json").read_bytes())
            self.assertFalse(any(store["deleted"] for store in evidence["stores"].values()))
