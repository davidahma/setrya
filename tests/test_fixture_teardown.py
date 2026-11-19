import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from setrya.examples.support_search.client import Client, FixtureError


class FixtureTeardownTests(unittest.TestCase):
    def test_server_reports_cleanup_failure(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "cleanup_case.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "from setrya.examples.support_search.__main__ import serve\n"
                "class Fixture:\n"
                "    def close(self):\n"
                "        raise RuntimeError('cleanup failed')\n"
                "serve(Fixture(), Path(sys.argv[2]), sys.argv[3])\n"
            )
            pythonpath = os.pathsep.join((folder, str(Path(__file__).resolve().parents[1])))
            with patch.dict(os.environ, PYTHONPATH=pythonpath):
                client = Client(directory, "run", "fixed", "refund", module="cleanup_case")
                with self.assertRaisesRegex(FixtureError, "cleanup failed"):
                    client.close()

    def test_forced_shutdown_is_reported(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 3), -9]
        client = Client.__new__(Client)
        client.process = process
        with self.assertRaisesRegex(FixtureError, "forced shutdown"):
            client.close()
        process.kill.assert_called_once()

    def test_cleanup_failure_preserves_startup_error(self):
        with TemporaryDirectory() as folder, patch("setrya.examples.support_search.client.subprocess.Popen") as spawn:
            spawn.return_value.poll.return_value = 1
            spawn.return_value.wait.return_value = 1
            with self.assertRaisesRegex(FixtureError, "did not start") as raised:
                Client(Path(folder), "run", "fixed", "refund")
        self.assertIn("cleanup also failed", raised.exception.__notes__[0])
