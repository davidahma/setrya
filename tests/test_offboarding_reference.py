import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from setrya.offboarding.cli import main
from examples.offboarding.fixture import reference_case
from setrya.offboarding.reference import (
    CASES,
    ROLES,
    InvalidReport,
    assess,
    reproduced_reference_cases,
    validate,
)


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {name: reference_case(name) for name in CASES}

    def case(self, name="fixed"):
        return copy.deepcopy(self.cases[name])

    def report(self):
        cases = [self.case(name) for name in CASES]
        for case in cases:
            case["assessment"] = assess(case)
        return {
            "schema_version": 1,
            "fixture_sha256": "ab" * 32,
            "read_roles": dict(ROLES),
            "cases": cases,
        }

    def test_baseline_and_control_are_needed_for_a_pass(self):
        for subject, phase in (("acme", "baseline"), ("birch", "worker_finished")):
            with self.subTest(subject=subject):
                case = self.case()
                row = next(
                    r
                    for r in case["observations"]
                    if (r["subject"], r["phase"], r["surface"])
                    == (subject, phase, "support")
                )
                row["state"] = "unavailable"
                assessment = assess(case)
                self.assertEqual(assessment["verdict"], "INCONCLUSIVE")
                self.assertIn(f"{phase}.{subject}.support", assessment["gaps"])

    def test_no_worker_observation_never_passes(self):
        case = self.case()
        case["worker"]["terminal"] = "unobserved"
        self.assertEqual(assess(case)["verdict"], "INCONCLUSIVE")

    def test_observed_residue_remains_a_failure_with_unrelated_gaps(self):
        case = self.case("broken")
        case["worker"] = {"captured_before_delete": False, "terminal": "unobserved"}
        result = assess(case)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("worker_finished.acme.support", result["failures"])
        self.assertIn("worker.checkpoint", result["gaps"])

    def test_malformed_duplicate_and_content_bearing_rows_fail(self):
        rows = self.case()["observations"]
        for mutated in (
            rows[:-1],
            [*rows[:-1], rows[0]],
            rows[::-1],
            [{**rows[0], "observed_at": "2025-02-31T12:00:00.000000Z"}, *rows[1:]],
            [{**rows[0], "state": "returned"}, *rows[1:]],
            [{**rows[0], "answer": "private content"}, *rows[1:]],
        ):
            case = self.case()
            case["observations"] = mutated
            with self.assertRaises(InvalidReport):
                assess(case)

    def test_validator_recomputes_verdict_and_rejects_extra_metadata(self):
        report = self.report()
        self.assertEqual(validate(report), report)
        report["cases"][0]["assessment"]["verdict"] = "PASS"
        with self.assertRaises(InvalidReport):
            validate(report)
        report = self.report()
        report["private_context"] = "should never be exported"
        with self.assertRaises(InvalidReport):
            validate(report)

    def test_known_match_with_missing_baseline_is_still_found(self):
        case = self.case("inconclusive")
        row = next(
            r
            for r in case["observations"]
            if (r["phase"], r["subject"], r["surface"])
            == ("worker_finished", "acme", "support")
        )
        row["state"] = "returned"
        result = assess(case)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("baseline.acme.support", result["gaps"])
        self.assertIn("worker_finished.acme.support", result["failures"])

    def test_different_failure_at_same_verdict_does_not_reproduce_demo(self):
        cases = self.report()["cases"]
        self.assertTrue(reproduced_reference_cases(cases))
        broken = cases[0]
        row = next(
            r
            for r in broken["observations"]
            if (r["phase"], r["subject"], r["surface"])
            == ("offboarded", "acme", "source")
        )
        row["state"] = "present"
        broken["assessment"] = assess(
            {k: v for k, v in broken.items() if k != "assessment"}
        )
        self.assertEqual(broken["assessment"]["verdict"], "FAIL")
        self.assertFalse(reproduced_reference_cases(cases))

    def test_missing_permission_signature_requires_an_unavailable_baseline(self):
        cases = self.report()["cases"]
        incomplete = cases[2]
        row = next(
            r
            for r in incomplete["observations"]
            if (r["phase"], r["subject"], r["surface"])
            == ("baseline", "acme", "support")
        )
        row["state"] = "not_returned"
        incomplete["assessment"] = assess(
            {k: v for k, v in incomplete.items() if k != "assessment"}
        )
        self.assertEqual(incomplete["assessment"]["verdict"], "INCONCLUSIVE")
        self.assertFalse(reproduced_reference_cases(cases))

    def test_cli_round_trip_and_rejection(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            path = Path(folder) / "subdir" / "sample.json"
            self.assertEqual(main(["reference-demo", "--output", str(path)]), 0)
            self.assertEqual(main(["validate", str(path)]), 0)
            content = json.loads(path.read_text())
            self.assertEqual(
                [c["assessment"]["verdict"] for c in content["cases"]],
                ["FAIL", "PASS", "INCONCLUSIVE"],
            )
            path.write_text('{"schema_version":1,"schema_version":1}')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["validate", str(path)]), 1)

    def test_legacy_validate_enforces_utf8_and_one_mib_on_file_and_symlink(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            path = Path(directory) / "report.json"
            link = Path(directory) / "report-link"
            link.symlink_to(path)
            raw = json.dumps(self.report()).encode("utf-8")
            at_limit = raw.ljust(1024 * 1024, b" ")
            for data, code in ((at_limit, 0), (at_limit + b" ", 1), (raw + b"\xff", 1)):
                with self.subTest(length=len(data)):
                    path.write_bytes(data)
                    self.assertEqual(main(["validate", str(link)]), code)
