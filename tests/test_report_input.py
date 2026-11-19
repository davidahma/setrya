import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from setrya.cli import EXAMPLE
from setrya.drill import Drill, InputError
from setrya.evidence import Report


class ReportInputTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "run.json"
        self.report = Report("run", Drill.load(EXAMPLE / "drill.toml").rules, ())
        self.report.save(self.path)

    def test_symlink_to_regular_report_is_readable(self):
        link = self.path.with_name("current.json")
        link.symlink_to(self.path)
        self.assertEqual(Report.load(link), self.report)

    @unittest.skipUnless(hasattr(os, "O_NONBLOCK"), "nonblocking descriptor flag unavailable")
    def test_the_open_itself_is_nonblocking(self):
        with patch("setrya.evidence.os.open", wraps=os.open) as opening:
            self.assertEqual(Report.load(self.path), self.report)
        _, flags = opening.call_args.args
        self.assertTrue(flags & os.O_NONBLOCK)

    def test_nonregular_descriptor_is_rejected_before_decode(self):
        with patch("setrya.evidence.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFIFO)), \
             patch("setrya.evidence.json.loads") as decode:
            with self.assertRaisesRegex(InputError, "regular file"):
                Report.load(self.path)
            decode.assert_not_called()

    def test_size_and_decoder_failures_use_the_report_error_contract(self):
        with patch("setrya.evidence.MAX_REPORT_BYTES", 16):
            with self.assertRaisesRegex(InputError, "exceeds size limit"):
                Report.load(self.path)
        with patch("setrya.evidence.json.loads", side_effect=ValueError("integer conversion limit")):
            with self.assertRaisesRegex(InputError, "could not read report as JSON"):
                Report.load(self.path)
