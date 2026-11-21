"""Exercise the public CLI from an empty directory with only the installed distribution."""
from importlib.metadata import metadata
from pathlib import Path
import json
import subprocess
import sys
from tempfile import TemporaryDirectory


def call(cwd, *args, code=0):
    result = subprocess.run([sys.executable, "-I", "-m", "setrya", *args], cwd=cwd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    if result.returncode != code:
        raise RuntimeError(f"{args} exited {result.returncode}\n{result.stdout}")
    return result.stdout


with TemporaryDirectory() as tmp:
    path = Path(tmp)
    if metadata("setrya")["Summary"] not in call(path, "--help"):
        raise RuntimeError("Installed package summary and CLI help differ.")
    call(path, "demo")
    call(path, "replay", "workspace/broken", code=1)
    call(path, "replay", "workspace/guarded")
    call(path, "replay", "workspace/guarded", "--case-review", code=3)
    call(path, "init")
    fixture = ("--starter", "setrya-case/test_erasure.py")
    for mode in ("deletion", "late-write"):
        call(path, "doctor", *fixture, "--mode", mode)
        call(path, "run", *fixture, "--mode", mode, "--output", mode)
        call(path, "replay", mode)
        receipt = json.loads((path / mode / "erasure-receipt.json").read_bytes())
        if len(receipt["case"]["callers"]) != 1 or receipt["case"]["artifacts"][0]["action"] != "delete":
            raise RuntimeError("The starter must exercise a single-caller delete-only case.")
print("Installed workspace demo, case review, starter, doctor, deletion, late writer and replay passed.")
