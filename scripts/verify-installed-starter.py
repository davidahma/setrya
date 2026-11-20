"""Exercise the installed endpoint starter in a new consumer project."""

from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


def run(directory, command, expected_exit):
    result = subprocess.run(command, cwd=directory, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=60)
    if result.returncode != expected_exit:
        raise RuntimeError(f"{command} exited {result.returncode}, expected {expected_exit}\n{result.stdout}")
    return result.stdout


def verify(project):
    cli = Path(sys.executable).with_name("setrya")
    run(project, [cli, "init", "endpoint"], 0)
    starter = project / "tests" / "test_setrya_endpoint.py"
    if not starter.is_file():
        raise RuntimeError("setrya init endpoint did not write tests/test_setrya_endpoint.py")

    artifacts = project / "pytest-output"
    output = run(project, [sys.executable, "-I", "-m", "pytest", "-q", "--color=no",
                           "--basetemp", artifacts, starter], 1)
    if "NOT TESTED" not in output or "seed" not in output:
        raise RuntimeError(f"pytest did not identify the missing seed hook\n{output}")
    handoff = re.search(r"\bReplay\s+(setrya replay [^\n]+)", output)
    if handoff is None:
        raise RuntimeError(f"The pytest failure did not print a replay command\n{output}")
    command = shlex.split(handoff.group(1))
    if len(command) != 3 or command[:2] != ["setrya", "replay"]:
        raise RuntimeError(f"Invalid replay command: {handoff.group(1)}")
    report = (project / command[2]).resolve()
    if not report.is_dir() or not report.is_relative_to(artifacts.resolve()):
        raise RuntimeError(f"The replay command did not point into this test run: {report}")
    replayed = run(project, [cli, "replay", report], 3)
    if "NOT TESTED" not in replayed or "seed" not in replayed:
        raise RuntimeError(f"Offline replay did not identify the missing seed hook\n{replayed}")
    print("Installed starter identified the missing seed hook in pytest and offline replay.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="setrya consumer ") as directory:
        verify(Path(directory))
