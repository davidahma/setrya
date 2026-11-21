"""Exercise the documented adopter flow using the installed CLI outside a checkout."""
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def run(cwd, *args, expected=0):
    result = subprocess.run([sys.executable, "-I", "-m", "setrya", *args], cwd=cwd,
                            capture_output=True, text=True, timeout=60)
    if result.returncode != expected:
        raise RuntimeError(f"{args} exited {result.returncode}\n{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    with TemporaryDirectory() as scratch:
        cwd = Path(scratch)
        run(cwd, "discovery", "init", "--output", "sample")
        run(cwd, "discovery", "import", "--finding", "sample/finding.json", "--output", "normalized.json")
        binding = ("--finding", "normalized.json", "--mapping", "sample/mapping.json")
        run(cwd, "discovery", "run", *binding, "--fixture", "sample/fixture.py", "--mode", "late-write", "--output", "case")
        run(cwd, "discovery", "replay", "case", *binding, "--case-review", expected=3)
        run(cwd, "replay", "case")
        run(cwd, "discovery", "export", "case", *binding, "--output", "ticket.json")
        if (cwd / "ticket.json").read_bytes() != (cwd / "case/case-result.json").read_bytes():
            raise RuntimeError("Returned case attachment differs from the verified case result.")
        run(cwd, "discovery", "demo", "--output", "three-cases")
        for lane, expected in (("gap", 3), ("broken", 1), ("guarded", 0)):
            run(cwd, "discovery", "replay", f"three-cases/{lane}", expected=expected)
        print("Installed discovery import, fixture, three verdicts, bound export and case review verified.")
