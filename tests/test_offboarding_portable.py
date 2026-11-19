"""Build and run Setrya's portable offboarding commands outside the checkout."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/build_offboarding.py"


class PortableOffboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory(prefix="setrya portable ")
        cls.addClassCleanup(temporary.cleanup)
        cls.work = Path(temporary.name)
        cls.archive = cls.work / "setrya-offboarding.pyz"
        cls.run_python(BUILDER, "--output", cls.archive, check=True)

    @classmethod
    def run_python(cls, *args, check=False):
        return subprocess.run(
            [sys.executable, "-I", "-S", *args], cwd=cls.work,
            capture_output=True, text=True, timeout=45, check=check,
        )

    def test_standalone_gate_and_receipt_commands(self):
        evidence = self.work / "sample"
        demo = self.run_python(self.archive, "demo", "--output", evidence, check=True)
        for case, code, verdict in (("broken", 1, "FAIL"), ("fixed", 0, "PASS"),
                                    ("inconclusive", 3, "NOT TESTED")):
            replay = self.run_python(
                self.archive, "gate", "--policy", evidence / "policy.json",
                "--observations", evidence / f"{case}.json", "--expect-run", case,
            )
            self.assertEqual(replay.returncode, code, replay.stderr)
            self.assertIn("Gate " + verdict, replay.stdout)

        # The demo must print a working portable command for a reviewer who has
        # neither the package installed nor the original source directory.
        reviewer = self.work / "reviewer"
        shutil.copytree(evidence / "receipt", reviewer)
        command = shlex.split(demo.stdout.splitlines()[-1])
        self.assertEqual(command[:3], ["python3", "-I", str(self.archive.resolve())])
        env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        replay = subprocess.run(command, cwd=reviewer, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(replay.returncode, 3, replay.stderr)
        self.assertIn("Receipt verified.", replay.stdout)
        self.assertIn("Technical verdict PASS", replay.stdout)
        self.assertIn("Review status OPEN_ITEMS", replay.stdout)

    def test_driven_deletion_schedules_inside_archive(self):
        clean = self.run_python(self.archive, "drill", "--schedule", "all", "--mode", "fixed",
                                "--output", self.work / "controlled", check=True)
        self.assertIn("reverse  PASS", clean.stdout)
        broken = self.run_python(self.archive, "drill", "--schedule", "reverse", "--mode", "broken",
                                 "--output", self.work / "unguarded")
        self.assertEqual(broken.returncode, 1)
        self.assertIn("FAIL", broken.stdout)

    def test_reference_and_sqlite_observer_inside_archive(self):
        report = self.work / "reference.json"
        self.run_python(self.archive, "reference-demo", "--output", report, check=True)
        self.run_python(self.archive, "validate", report, check=True)
        policy = self.work / "policy.json"
        policy.write_text(self.run_python(self.archive, "sqlite-policy", check=True).stdout)
        for case, code in (("clean", 0), ("residual", 1), ("unavailable", 3)):
            observations = self.work / f"sqlite-{case}.json"
            self.run_python(self.archive, "observe-sqlite", "--case", case, "--output", observations, check=True)
            gate = self.run_python(self.archive, "gate", "--policy", policy, "--observations", observations)
            self.assertEqual(gate.returncode, code, gate.stderr)
        self.assertTrue(json.loads(report.read_bytes())["cases"])

    def test_release_archive_is_reproducible_and_excludes_local_files(self):
        # Rebuild from the manifest alone: no .git, sibling project, or packaging
        # metadata. Converting all inputs to CRLF must preserve the published bytes.
        with ZipFile(self.archive) as archive:
            names = [name for name in archive.namelist() if name != "__main__.py"]
        with TemporaryDirectory(dir=self.work) as folder:
            checkout = Path(folder)
            builder = checkout / "scripts/build_offboarding.py"
            builder.parent.mkdir()
            shutil.copyfile(BUILDER, builder)
            for name in names:
                target = checkout / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            (checkout / "setrya/offboarding/local_debug.py").write_text("local_only = True\n")
            rebuilt = checkout / "new.pyz"
            self.run_python(builder, "--output", rebuilt, check=True)
            self.assertEqual(rebuilt.read_bytes(), self.archive.read_bytes())
            source = checkout / "setrya/offboarding/gate.py"
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            hardlink = checkout / "alias"
            os.link(source, hardlink)
            symlink = checkout / "symbolic"
            symlink.symlink_to(source)
            for path in (source, hardlink, symlink, builder):
                self.assertNotEqual(self.run_python(builder, "--output", path).returncode, 0)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            source.unlink()
            self.assertNotEqual(self.run_python(builder, "--output", rebuilt).returncode, 0)
            self.assertEqual(rebuilt.read_bytes(), self.archive.read_bytes())
