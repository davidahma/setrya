"""Replay the exact search exhibits distributed with the Setrya v0.7.0 release."""
from pathlib import Path
import unittest

from setrya.checker import evaluate
from setrya.evidence import Report
from setrya.status import Status


class ReleasedEvidenceTests(unittest.TestCase):
    def test_v070_review_bundle_retains_its_verdicts(self):
        folder = Path(__file__).parent / "fixtures" / "search-v070"
        for name, expected in (("base", Status.FAIL), ("candidate", Status.PASS), ("opaque", Status.NOT_TESTED)):
            with self.subTest(exhibit=name):
                self.assertIs(evaluate(Report.load(folder / f"{name}.json")).status, expected)
