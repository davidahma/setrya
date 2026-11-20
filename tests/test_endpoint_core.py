"""The public endpoint runner and its reviewer work without any bundled app."""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class EndpointCoreTests(unittest.TestCase):
    def test_check_and_replay_do_not_import_an_example(self):
        program = """
import sys
from contextlib import contextmanager
from pathlib import Path

class NoExamples:
    def find_spec(self, fullname, path, target=None):
        if fullname == 'setrya.examples' or fullname.startswith('setrya.examples.'):
            raise RuntimeError('unexpected example dependency: ' + fullname)

sys.meta_path.insert(0, NoExamples())
from setrya.endpoint import EndpointCase, MissingHook, check_endpoint
from setrya.cli import main
from setrya.status import Status

@contextmanager
def application(run_id):
    raise MissingHook('seed')
    yield

case = EndpointCase('route', 'reader', 'owner', 'Question?', 'policy', 'guide', 'private', 'public')
destination = Path(sys.argv[1]) / 'run'
verdict, text = check_endpoint(case, application, destination)
assert verdict.status == Status.NOT_TESTED and 'seed()' in text
for command in ('replay', 'diagnose'):
    assert main([command, str(destination)]) == Status.NOT_TESTED.exit_code
"""
        with TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, "-c", program, tmp], cwd=Path(__file__).resolve().parents[1],
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unexpected example dependency", proc.stdout)
