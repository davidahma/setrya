"""Regression tests for false passes and lost evidence at the public entry points."""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.adapter import Checkpoint, Publication, ReadView, Receipt, SearchResult
from setrya.checker import Status
from setrya.cli import EXAMPLE, demo
from setrya.drill import Drill, InputError
from setrya.runner import run

ROOT = Path(__file__).resolve().parents[1]
FACTORY = "test_trust:fixture"
PRIVATE = "private query and endpoint https://internal.example.test"


class EvidenceFixture:
    def __init__(self, context):
        self.run_id = context.run_id
        self.mode = context.drill.options.get("mode", "fixed")
        self.published = False
        self.released = False
        self.freshness_queries = 0

    def seed(self):
        pass

    def start_old(self):
        return "attempt"

    def checkpoint(self):
        if self.mode == "missing-checkpoint":
            return None
        return Checkpoint(self.run_id, "attempt", "refund", "A", "after_source_read")

    def publish(self):
        self.published = True
        if self.mode != "missing-publication":
            return Publication(self.run_id, "refund", "B")

    def search(self, probe):
        route = probe.rules.route
        scope = probe.rules.scope
        if probe.rules.id == "delivery":
            passages = ["delivery@1"]
        elif not self.published:
            passages = ["refund@A", "refund@B"] if self.mode == "ambiguous-baseline" else ["refund@A"]
        elif not self.released:
            self.freshness_queries += 1
            if self.mode == "preview":
                scope = "preview"
            if self.mode == "unconfirmed-caller":
                scope = None
            stale = self.mode in {"release-error", "malformed-after-fail"} and self.freshness_queries > 1
            passages = ["refund@A" if stale else "refund@B"]
        else:
            passages = ["refund@B", "other@1", "refund@A"] if self.mode == "ranked" else ["refund@B"]
        if self.mode == "malformed-after-fail" and self.freshness_queries > 1 and probe.rules.id == "delivery":
            passages = [PRIVATE]
        read_view = ReadView("generation", 3, route, scope) if scope is not None and self.mode != "preview" else None
        return SearchResult(tuple(passages[:probe.rules.top_k]), view=read_view, route=route, scope=scope)

    def release(self):
        if self.mode in {"release-error", "early-error"}:
            raise ConnectionError(PRIVATE)
        self.released = True

    def receipt(self):
        if self.mode == "ranked":
            return Receipt(self.run_id, "attempt", "refund", "A", "committed", "generation", 3)
        return Receipt(self.run_id, "attempt", "refund", "A", "rejected")

    def close(self):
        if self.mode == "close-error":
            raise ConnectionError(PRIVATE)


def fixture(context):
    return EvidenceFixture(context)


class TrustTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory(prefix="setrya-trust-")
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)
        self.report = self.directory / "run.json"
        self.summary = self.directory / "summary.md"
        self.drill = self.directory / "drill.toml"

    def config(self, mode="fixed", **replacements):
        content = (EXAMPLE / "drill.toml").read_text(encoding="utf-8")
        changes = {
            'factory = "examples.support_search.adapter:http_fixture"': f'factory = "{FACTORY}"',
            'mode = "fixed"': f'mode = "{mode}"',
            'freshness_ms = 1200': 'freshness_ms = 300',
            'max_gap_ms = 225': 'max_gap_ms = 120',
            **replacements,
        }
        for previous, new in changes.items():
            content = content.replace(previous, new)
        self.drill.write_text(content, encoding="utf-8")
        return self.drill

    def cli(self, *args, entry=("-m", "setrya"), pythonpath=None):
        paths = (ROOT, ROOT / "tests") if pythonpath is None else pythonpath
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, paths))}
        return subprocess.run([sys.executable, *entry, *map(str, args)], capture_output=True,
                              text=True, cwd=self.directory, env=env, timeout=15)

    def check(self):
        return self.cli("check", self.drill.name, "--report", self.report.name, "--summary", self.summary.name)

    def assert_exit(self, completed, code, detail):
        self.assertEqual(completed.returncode, code, completed.stdout + completed.stderr)
        self.assertIn(detail, completed.stdout + completed.stderr)

    def bundle(self):
        return json.loads(self.report.read_text())

    def replay(self, data=None):
        if data is not None:
            self.report.write_text(json.dumps(data))
        return self.cli("replay", self.report.name)

    def test_wrong_and_unconfirmed_callers_cannot_trigger_release(self):
        for mode in ("preview", "unconfirmed-caller"):
            with self.subTest(mode=mode):
                self.config(mode)
                self.assert_exit(self.check(), 3, "declared route and caller before release")
                self.assertNotIn("released", {event["kind"] for event in self.bundle()["events"]})
                self.assert_exit(self.replay(), 3, "declared route and caller before release")

    def test_replay_cannot_use_a_valid_read_after_release_as_freshness_proof(self):
        self.config()
        self.assert_exit(self.check(), 0, "PASS")
        bundle = self.bundle()
        for event in bundle["events"]:
            if event.get("stage") == "freshness" and event["probe_id"] == "refund-window":
                event.update(scope="preview", view={**event["view"], "scope": "preview"})
        self.assert_exit(self.replay(bundle), 3, "declared route and caller before release")

    def test_programmatic_probe_depth_must_match_report_before_adapter_import(self):
        drill = Drill.load(self.config("ranked", **{"top_k = 3": "top_k = 5"}))
        result = run(drill)
        self.assertIs(result.verdict.status, Status.FAIL)
        self.assertIn("rank 3", result.verdict.detail)
        primary, *controls = drill.probes
        narrowed = replace(primary, rules=replace(primary.rules, top_k=1))
        with patch("setrya.runner.import_module") as load:
            with self.assertRaisesRegex(InputError, "including result depth"):
                run(replace(drill, probes=(narrowed, *controls)))
            load.assert_not_called()
        legitimately_shallow = replace(drill, rules=replace(drill.rules, probes=(narrowed.rules, *drill.rules.probes[1:])),
                                       probes=(narrowed, *controls))
        self.assertIs(run(legitimately_shallow).verdict.status, Status.PASS)

    def test_shared_document_id_is_rejected_and_ambiguous_baseline_cannot_pass(self):
        self.config(**{'before = ["refund@A"]': 'before = ["refund"]',
                       'require = ["refund@B"]': 'require = ["refund"]',
                       'forbid = ["refund@A"]': 'forbid = ["refund"]'})
        self.assert_exit(self.check(), 2, "required passages disjoint")
        self.config(**{'before = ["refund@A"]': 'before = ["refund"]',
                       'require = ["refund@B"]': 'require = ["refund"]'})
        self.assert_exit(self.check(), 2, "revision-specific required marker")
        self.config("ambiguous-baseline")
        self.assert_exit(self.check(), 3, "did not distinguish the original revision")
        self.assertNotIn("released", {event["kind"] for event in self.bundle()["events"]})
        self.assert_exit(self.replay(), 3, "did not distinguish the original revision")

    def test_publication_acknowledges_the_exact_replacement_and_document(self):
        mismatches = (("revision", "C"), ("document", "different"), ("run_id", "other-run"))
        # The reference app publishes B even if a drill asks for C.
        self.config(**{'new_revision = "B"': 'new_revision = "C"',
                       'factory = "examples.support_search.adapter:http_fixture"':
                           'factory = "examples.support_search.adapter:http_fixture"'})
        self.assert_exit(self.check(), 3, "Missing matching publication of revision C")
        self.assertNotIn("released", {event["kind"] for event in self.bundle()["events"]})
        self.assert_exit(self.replay(), 3, "Missing matching publication of revision C")
        self.config("missing-publication")
        self.assert_exit(self.check(), 3, "Missing matching publication")
        self.config()
        self.assert_exit(self.check(), 0, "Published refund revision B")
        valid = self.bundle()
        for field, replacement in mismatches:
            with self.subTest(field=field):
                mutated = json.loads(json.dumps(valid))
                publication = next(event["publication"] for event in mutated["events"] if event["kind"] == "published")
                publication[field] = replacement
                self.assert_exit(self.replay(mutated), 3, "Missing matching publication")

    def test_following_adapter_error_preserves_failure_and_report(self):
        for mode, detail in (("release-error", "release (ConnectionError)"),
                             ("malformed-after-fail", "search (InputError)")):
            with self.subTest(mode=mode):
                self.config(mode)
                checked = self.check()
                self.assert_exit(checked, 1, detail)
                self.assertIn("Observed forbidden refund@A", checked.stdout)
                replayed = self.replay()
                self.assert_exit(replayed, 1, detail)
                self.assertIn("Observed while the old worker was held", replayed.stdout)
                for output in (checked.stdout, checked.stderr, replayed.stdout, self.report.read_text(),
                               self.summary.read_text()):
                    self.assertNotIn(PRIVATE, output)
                self.assertIn("python3 -m setrya replay run.json", self.summary.read_text())

    def test_adapter_error_without_failure_is_replayable_execution_error(self):
        for mode, detail in (("early-error", "release (ConnectionError)"), ("close-error", "close (ConnectionError)")):
            with self.subTest(mode=mode):
                self.config(mode)
                self.assert_exit(self.check(), 2, detail)
                self.assert_exit(self.replay(), 2, detail)

    def test_legacy_report_remains_readable_but_cannot_claim_current_pass(self):
        self.config()
        self.assert_exit(self.check(), 0, "PASS")
        legacy = self.bundle()
        legacy.update(schema=1, checker=1)
        for event in legacy["events"]:
            for field in ("route", "scope", "publication"):
                event.pop(field, None)
        replay = self.replay(legacy)
        self.assert_exit(replay, 3, "Report schema 1 lacks current publication and caller evidence")
        self.assertIn("History", replay.stdout)
        self.assertIn("refund@B", replay.stdout)

    def test_demo_only_describes_observed_checkpoint_and_search_history(self):
        drill = self.config("missing-checkpoint", **{"checkpoint_ms = 2000": "checkpoint_ms = 250"})
        result = run(Drill.load(drill))
        output = StringIO()
        with patch("setrya.cli.demo_case", return_value=result), redirect_stdout(output):
            self.assertEqual(demo("opaque"), 0)
        self.assertIn("30 days", output.getvalue())
        self.assertNotIn("14 days", output.getvalue())
        self.assertNotIn("Old worker released", output.getvalue())
        self.assertNotIn("Checkpoint held", output.getvalue())

    def test_console_entry_resolves_adapter_in_working_checkout_without_implicit_cwd(self):
        (self.directory / "checkout_adapter.py").write_text("from test_trust import fixture\n")
        self.config(**{'factory = "examples.support_search.adapter:http_fixture"':
                           'factory = "checkout_adapter:fixture"'})
        isolated = ("-P", "-c", "from setrya.cli import main; raise SystemExit(main())")
        installed_entry = ("-P", "-c", "from setrya.cli import console_main; raise SystemExit(console_main())")
        self.assert_exit(self.cli("check", self.drill.name, entry=isolated), 2, "could not import adapter")
        self.assert_exit(self.cli("check", self.drill.name, entry=installed_entry), 0, "PASS")


if __name__ == "__main__":
    unittest.main()
