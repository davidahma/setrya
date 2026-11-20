"""Two real fixture commits, one trusted harness, and an offline reviewer."""
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.cli import main
from setrya.endpoint_evidence import JOURNAL, MANIFEST, contract_for, observation


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/endpoint_app.py"
HARNESS = ROOT / "scripts/check-pr-endpoint.py"


def git(folder, *args):
    return subprocess.check_output(["git", "-C", str(folder), *args], text=True).strip()


class EndpointPrTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "app"
        self.app = self.repo / "tests/fixtures/endpoint_app.py"
        self.app.parent.mkdir(parents=True)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        self.out = self.root / "review"
        self.source = FIXTURE.read_text()

    def commit(self, source):
        self.app.write_text(source)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--allow-empty", "-qm", "fixture change")
        return git(self.repo, "rev-parse", "HEAD")

    def cli(self, *args):
        capture = StringIO()
        with redirect_stdout(capture), redirect_stderr(capture):
            code = main(list(args))
        return code, capture.getvalue()

    def compare(self, before, after, harness=HARNESS, *, timeout=30):
        self.base, self.candidate = self.commit(before), self.commit(after)
        with patch.dict(os.environ, {"PYTHONPATH": str(ROOT)}):
            code, _ = self.cli("run-pr", "--endpoint", "--repo", str(self.repo), "--base", self.base,
                               "--candidate", self.candidate, "--output", str(self.out), "--exit-contract", "setrya", "--timeout", str(timeout),
                               "--", sys.executable, str(harness))
        return code, (self.out / "review.md").read_text()

    def test_http_fixture_starts_without_reverse_dns(self):
        import runpy

        fixture = runpy.run_path(str(FIXTURE))
        with patch.dict(os.environ, APP_TEST_MODE="guarded"), patch("socket.getfqdn", side_effect=AssertionError):
            with fixture["Application"]("local-check") as app:
                app.seed()
                for caller in ("owner", "reviewer"):
                    self.assertEqual(app.answer(caller, fixture["CASE"].question)["caller"], caller)

    def test_verified_fix_shows_both_callers_and_replays_without_checkout(self):
        broken = self.source.replace('if self.granted or self.mode == "broken":', "if True:")
        code, text = self.compare(broken, self.source)
        self.assertEqual(code, 0, text)
        self.assertIn("FIX VERIFIED", text)
        self.assertIn("FIX VERIFIED", text.splitlines()[3])
        self.assertIn("Replay base  setrya replay base", text.splitlines()[4])
        self.assertIn("Replay candidate  setrya replay candidate", text.splitlines()[5])
        self.assertIn("Weakest evidence · write-confirmed", text)
        self.assertIn("revoked reviewer · final event", text)
        self.assertIn("retained owner · final event", text)
        self.assertIn("limits@2", text)
        self.assertNotIn("Which approval level applies?", text)
        self.assertNotIn(str(self.root), text)
        shutil.rmtree(self.repo)
        for name, expected in (("base", 1), ("candidate", 0)):
            self.assertEqual(self.cli("replay", str(self.out / name))[0], expected)
        metadata = json.loads((self.out / "run-pr.json").read_bytes())
        for name in ("base", "candidate"):
            manifest = json.loads((self.out / name / MANIFEST).read_bytes())
            journal = json.loads((self.out / name / JOURNAL).read_bytes())
            self.assertEqual(manifest["revision"], metadata[name]["commit"])
            self.assertEqual(journal["inputs"]["revision"], metadata[name]["commit"])
        args = ["review", str(self.out / "base"), str(self.out / "candidate"), "--base-commit", self.base,
                "--candidate-commit", self.candidate, "--base-exit", "1", "--candidate-exit", "0"]
        self.assertIn("FIX VERIFIED", self.cli(*args)[1])
        args[4] = "f" * 40
        self.assertTrue(self.cli(*args)[1].startswith("NOT TESTED"))
        args[4] = self.base
        args[-1] = "5"
        self.assertTrue(self.cli(*args)[1].startswith("ERROR"))

    def test_both_pass_and_changed_case_and_wrong_principal(self):
        for candidate, expected, headline in (
                (self.source, 0, "PASS  Both endpoint revisions passed"),
                (self.source.replace("Which approval level applies?", "New question?"), 3, "NOT TESTED  The two endpoint cases"),
                (self.source.replace('app.mode == "wrong-principal"', "True"), 3, "NOT TESTED  Check answer()"),
                (self.source.replace('if self.granted or self.mode == "broken":', "if True:"), 1, "FAIL  At event")):
            with self.subTest(headline=headline):
                code, text = self.compare(self.source, candidate)
                self.assertEqual(code, expected, text)
                self.assertIn(headline, text)
                self.assertNotIn("FIX VERIFIED", text)
                shutil.rmtree(self.out)

    def test_partial_base_evidence_cannot_verify_a_late_worker_fix(self):
        broken = self.source.replace('if self.granted or self.mode == "broken":', "if True:")
        hook = "if app.mode == \"wrong-principal\" and app.finished.is_set():"
        for fault in ('answer["query_sha256"] = "f" * 64', 'answer["caller"] = "somebody-else"',
                      'answer["commit"] = 0', 'answer["cache_hit"] = False', None):
            with self.subTest(fault=fault):
                if fault:
                    before = broken.replace(hook, 'if principal == "owner" and app.finished.is_set():\n            ' + fault
                                            + "\n        " + hook)
                else:
                    before = broken.replace('(body, sources, "job-73")', '(body, sources, "unknown-writer")')
                code, text = self.compare(before, self.source)
                self.assertEqual(code, 0, text)
                self.assertIn("PASS  Both candidate callers passed", text)
                self.assertIn("Weakest evidence · response observed", text)
                self.assertNotIn("FIX VERIFIED", text)
                shutil.rmtree(self.out)

    def test_base_needs_both_retained_reads_before_release_to_verify_a_fix(self):
        broken = self.source.replace('if self.granted or self.mode == "broken":', "if True:")
        self.assertEqual(self.compare(broken, self.source)[0], 0)
        base = self.out / "base"
        trace = json.loads((base / JOURNAL).read_bytes())
        manifest = json.loads((base / MANIFEST).read_bytes())
        for stage in ("baseline", "freshness"):
            with self.subTest(stage=stage):
                events = [dict(e) for e in trace["events"]
                          if (e.get("stage"), e.get("probe_id")) != (stage, "retained-answer")]
                rows = [r for r in manifest["observations"] if (r["stage"], r["probe"]) != (stage, "retained-answer")]
                for seq, event in enumerate(events, 1):
                    event["seq"] = seq
                (base / JOURNAL).write_text(json.dumps({**trace, "events": events}))
                digest = sha256((base / JOURNAL).read_bytes()).hexdigest()
                (base / MANIFEST).write_text(json.dumps({**manifest, "observations": rows, "report_sha256": digest}))
                code, summary = self.cli("review", str(base), str(self.out / "candidate"),
                                         "--base-commit", self.base, "--candidate-commit", self.candidate,
                                         "--base-exit", "1", "--candidate-exit", "0")
                self.assertEqual(code, 0, summary)
                self.assertIn("PASS  Both candidate callers passed", summary)
                self.assertIn("Weakest evidence · response observed", summary)
                self.assertNotIn("FIX VERIFIED", summary)

    def test_replayed_candidate_needs_retained_cache_hit_while_worker_is_held(self):
        self.assertEqual(self.compare(self.source, self.source)[0], 0)
        candidate = self.out / "candidate"
        trace = json.loads((candidate / JOURNAL).read_bytes())
        manifest = json.loads((candidate / MANIFEST).read_bytes())
        row = next(r for r in manifest["observations"] if (r["stage"], r["probe"]) == ("freshness", "retained-answer"))
        row.update(cache_hit=False, cache_writer=None)
        result = observation(row, contract_for(manifest))
        event = next(e for e in trace["events"] if (e.get("stage"), e.get("probe_id")) == (row["stage"], row["probe"]))
        event.update(passages=result.passages, origins=result.origins)
        (candidate / JOURNAL).write_text(json.dumps(trace))
        manifest["report_sha256"] = sha256((candidate / JOURNAL).read_bytes()).hexdigest()
        (candidate / MANIFEST).write_text(json.dumps(manifest))
        code, summary = self.cli("replay", str(candidate))
        self.assertEqual(code, 3, summary)
        self.assertIn("freshness read for owner", summary)
        self.assertIn("cache-hit", summary)

    def test_late_retained_read_cannot_verify_fix(self):
        broken = self.source.replace('if self.granted or self.mode == "broken":', "if True:")
        self.assertEqual(self.compare(broken, self.source)[0], 0)
        journal = self.out / "base" / JOURNAL
        trace = json.loads(journal.read_bytes())
        final = trace["events"][-1]
        self.assertEqual(final["probe_id"], "retained-answer")
        final["ns"] += (trace["rules"]["settle_ms"] + 1) * 1_000_000
        journal.write_text(json.dumps(trace))
        path = self.out / "base" / MANIFEST
        manifest = json.loads(path.read_bytes())
        manifest["report_sha256"] = sha256(journal.read_bytes()).hexdigest()
        path.write_text(json.dumps(manifest))
        code, text = self.cli("review", str(self.out / "base"), str(self.out / "candidate"),
                              "--base-commit", self.base, "--candidate-commit", self.candidate,
                              "--base-exit", "1", "--candidate-exit", "0")
        self.assertEqual(code, 0, text)
        self.assertIn("Weakest evidence · response observed", text)
        self.assertNotIn("FIX VERIFIED", text)

    def test_two_selected_tests_are_ambiguous(self):
        driver = self.root / "twice.py"
        driver.write_text(HARNESS.read_text().replace("raise SystemExit(main())", "main()\n    main()"))
        code, text = self.compare(self.source, self.source, harness=driver)
        self.assertEqual(code, 2)
        self.assertIn("exactly one Setrya recording", text)
        self.assertFalse((self.out / "candidate").exists())

    def test_timed_out_teardown_preserves_only_a_completed_endpoint_run(self):
        driver = self.root / "timeout.py"
        # Base hangs before recording anything. Candidate finishes the SDK
        # check, then hangs during an unrelated part of the native command.
        driver.write_text(HARNESS.read_text().replace("raise SystemExit(main())", """
    import time
    if Path('tests/fixtures/endpoint_app.py').read_text().endswith('# candidate\\n'):
        main()
    time.sleep(60)
"""))
        code, summary = self.compare(self.source, self.source + "# candidate\n", driver, timeout=2)
        self.assertEqual(code, 2, summary)
        self.assertIn("ERROR", summary)
        self.assertIn("command timed out after recording", summary)
        self.assertIn("Weakest evidence · incomplete", summary)
        self.assertIn("Replay candidate", summary)
        self.assertNotIn("Replay base", summary)
        self.assertNotIn("Review again", summary)
        self.assertFalse((self.out / "base").exists())
        self.assertEqual(self.cli("replay", str(self.out / "candidate"))[0], 0)
        metadata = json.loads((self.out / "run-pr.json").read_bytes())
        self.assertIsNone(metadata["candidate"]["test_exit"])
        self.assertTrue(metadata["candidate"]["manifest_sha256"])
