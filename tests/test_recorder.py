from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.adapter import Checkpoint, Publication, ReadView, Receipt, SearchResult
from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE, main
from setrya.comparison import compare
from setrya.drill import Drill, InputError
from setrya.evidence import Report
from setrya.recorder import Recorder
from setrya.review import review
from setrya.runner import run

BASE = "a" * 40
CANDIDATE = "b" * 40


class RecorderTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.drill = self.directory / "test.toml"
        fixture = (EXAMPLE / "drill.toml").read_text()
        version, remainder = fixture.split("[adapter]", 1)
        self.drill.write_text(version + "[rules]" + remainder.split("[rules]", 1)[1])

    def create(self, revision=BASE, *, test_id="refund-test"):
        return Recorder(self.drill, test_id=test_id, report=self.directory / f"{revision}.json", revision=revision)

    def observe(self, recorder, probe_id, *passages, view=None, caller="support-agent"):
        def search(probe):
            self.assertEqual(probe.rules.top_k, 3)
            self.assertIn(probe.question, (item.question for item in Drill.load(self.drill).probes))
            return SearchResult(passages, view=view, route="support-search", scope=caller)

        return recorder.search(probe_id, search)

    def sequence(self, recorder, *, result="refund@B", receipt="rejected", view=True, caller="support-agent",
                 before_checkpoint=lambda: None):
        recorder.seeded()
        self.observe(recorder, "refund-window", "refund@A")
        self.observe(recorder, "delivery", "delivery@1")
        recorder.started("attempt")
        before_checkpoint()
        recorder.held(Checkpoint(recorder.run_id, "attempt", "refund", "A", "after_source_read"))
        recorder.published(Publication(recorder.run_id, "refund", "B"))
        self.observe(recorder, "refund-window", "refund@B", caller=caller)
        recorder.released("attempt")
        watermark = None
        if receipt == "committed":
            recorder.receipt(Receipt(recorder.run_id, "attempt", "refund", "A", receipt, "gen", 5))
            if view:
                watermark = ReadView("gen", 5, "support-search", "support-agent")
        elif receipt:
            recorder.receipt(Receipt(recorder.run_id, "attempt", "refund", "A", receipt))
        self.observe(recorder, "refund-window", result, view=watermark)
        self.observe(recorder, "delivery", "delivery@1", view=watermark)

    def record(self, revision=BASE, **options):
        recorder = self.create(revision)
        with recorder:
            self.sequence(recorder, **options)
        return Report.load(self.directory / f"{revision}.json")

    def test_existing_test_records_pass_and_uses_the_bound_search_inputs(self):
        recorder = self.create()
        with recorder:
            self.sequence(recorder)
        self.assertIs(recorder.result.verdict.status, Status.PASS)
        report = Report.load(self.directory / f"{BASE}.json")
        self.assertEqual(report.inputs["revision"], BASE)
        self.assertIs(evaluate(report).status, Status.PASS)
        self.assertNotIn("How long do I have", (self.directory / f"{BASE}.json").read_text())
        with self.assertRaisesRegex(InputError, "requires an adapter"):
            run(self.drill)

    def test_foreign_scope_or_missing_write_evidence_cannot_pass(self):
        observations = ({"caller": "preview"}, {"receipt": "committed", "view": False}, {"receipt": None})
        for options in observations:
            with self.subTest(options=options):
                self.assertIs(evaluate(self.record(**options)).status, Status.NOT_TESTED)

    def test_late_checkpoint_cannot_be_reported_as_pass(self):
        monotonic = 0

        def advance():
            nonlocal monotonic
            monotonic += 3_000_000_000

        with patch("setrya.recorder.monotonic_ns", side_effect=lambda: monotonic):
            report = self.record(before_checkpoint=advance)
        verdict = evaluate(report)
        self.assertIs(verdict.status, Status.NOT_TESTED)
        self.assertIn("checkpoint acknowledgment arrived after", verdict.detail)

    def test_assertion_failure_preserves_fail_and_suppresses_error_text(self):
        for marker, expected in (("refund@A", Status.FAIL), ("refund@B", Status.ERROR)):
            with self.subTest(marker=marker):
                recorder = self.create()
                with self.assertRaises(AssertionError):
                    with recorder:
                        self.sequence(recorder, result=marker)
                        raise AssertionError("private payload")
                report_path = self.directory / f"{BASE}.json"
                self.assertNotIn("private payload", report_path.read_text())
                self.assertIs(evaluate(Report.load(report_path)).status, expected)

    def test_report_cannot_overwrite_the_drill(self):
        original = self.drill.read_text()
        with self.assertRaisesRegex(InputError, "distinct files"):
            Recorder(self.drill, test_id="test", report=self.drill)
        self.assertEqual(self.drill.read_text(), original)

    def test_misordered_event_preserves_a_readable_error_report(self):
        recorder = self.create()
        with self.assertRaisesRegex(InputError, "invalid lifecycle"):
            with recorder:
                recorder.released("attempt")
        self.assertIs(evaluate(Report.load(self.directory / f"{BASE}.json")).status, Status.ERROR)

    def test_review_distinguishes_pass_fix_regression_and_incomplete(self):
        base_pass = self.record()
        base_fail = self.record(result="refund@A")
        candidate_pass = self.record(CANDIDATE)
        candidate_fail = self.record(CANDIDATE, result="refund@A")
        candidate_incomplete = self.record(CANDIDATE, receipt=None)
        for base, candidate, exits, expected, headline in (
            (base_pass, candidate_pass, (0, 0), 0, "PASS"),
            (base_fail, candidate_pass, (1, 0), 0, "FIX VERIFIED"),
            (base_pass, candidate_fail, (0, 1), 1, "REGRESSION"),
            (base_pass, candidate_incomplete, (0, 0), 3, "INCONCLUSIVE"),
            (base_pass, candidate_pass, (0, 1), 2, "ERROR"),
            (base_pass, candidate_pass, (0, 5), 2, "ERROR"),
            (base_fail, candidate_pass, (0, 0), 2, "ERROR"),
        ):
            with self.subTest(headline=headline):
                code, text = review(base, candidate, base_commit=BASE, candidate_commit=CANDIDATE,
                                    base_exit=exits[0], candidate_exit=exits[1])
                self.assertEqual(code, expected)
                self.assertTrue(text.startswith(headline), text)
                self.assertIn(CANDIDATE, text)
        self.assertEqual(compare(base_pass, candidate_pass)[0], 3)

    def test_review_summary_includes_replay_commands_without_private_paths(self):
        base = self.directory / "base.json"
        candidate = self.directory / "private-filename.json"
        summary = self.directory / "review.md"
        self.record(result="refund@A").save(base)
        self.record(CANDIDATE).save(candidate)
        with redirect_stdout(StringIO()):
            code = main(["review", str(base), str(candidate), "--base-commit", BASE,
                         "--candidate-commit", CANDIDATE, "--base-exit", "1", "--candidate-exit", "0",
                         "--summary", str(summary)])
        self.assertEqual(code, 0)
        text = summary.read_text()
        self.assertIn("python3 -m setrya replay base.json", text)
        self.assertIn('python3 -m setrya replay "$CANDIDATE_REPORT"', text)
        self.assertNotIn(str(self.directory), text)
        self.assertNotIn(candidate.name, text)

        base = self.record()
        candidate = self.record(CANDIDATE)
        code, text = review(base, candidate, base_commit=CANDIDATE, candidate_commit=BASE,
                            base_exit=0, candidate_exit=0)
        self.assertEqual(code, 3)
        self.assertIn("expected commit", text)
        recorder = self.create(CANDIDATE, test_id="different-test")
        with recorder:
            self.sequence(recorder)
        self.assertEqual(compare(base, recorder.result.report)[0], 3)

    def test_report_schema_three_still_replays(self):
        loaded = self.record()
        report = replace(loaded, schema=3, inputs={key: value for key, value in loaded.inputs.items() if key != "revision"})
        path = self.directory / "legacy.json"
        report.save(path)
        self.assertIs(evaluate(Report.load(path)).status, Status.PASS)


if __name__ == "__main__":
    unittest.main()
