"""Replay the downloaded access-fix example with the installed CLI, outside any checkout."""

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def verify(archive, published_report, work):
    lanes = ("base", "candidate")
    members = ["review.md", "run-pr.json", "fixture.bundle", "check-pr-endpoint.py"]
    for lane in lanes:
        members.extend((f"{lane}/answer-cache.json", f"{lane}/run.json"))
    with ZipFile(archive) as bundle:
        if sorted(bundle.namelist()) != sorted(members):
            raise ValueError("the example must contain both complete runs, the handoff and its fixture commits")
        for name in members:
            bundle.extract(name, work)

    report = (work / "review.md").read_bytes()
    if report != published_report.read_bytes():
        raise ValueError("the linked report and the downloaded report differ")
    metadata = json.loads((work / "run-pr.json").read_bytes())
    if metadata["format"] != "setrya.endpoint-pr" or metadata["exit_contract"] != "setrya":
        raise ValueError("expected a commit-bound endpoint handoff with native Setrya exits")
    # A bare, local clone verifies the archived history without starting the fixture application.
    subprocess.run(["git", "clone", "--quiet", "--bare", "fixture.bundle", "fixture.git"], cwd=work, check=True)
    revisions = subprocess.check_output(
        ["git", "--git-dir=fixture.git", "rev-parse", "refs/tags/base", "refs/heads/main", "refs/heads/main^"],
        cwd=work, text=True,
    ).splitlines()
    base, candidate, parent = revisions
    if base != parent:
        raise ValueError("the fixed fixture revision must follow the broken revision")
    review = ["setrya", "review", *lanes]
    for lane, commit, expected_exit, revoked_answer in (
        ("base", base, 1, "private-answer"),
        ("candidate", candidate, 0, "public-only"),
    ):
        entry = metadata[lane]
        if (entry["commit"], entry["test_exit"]) != (commit, expected_exit):
            raise ValueError(f"{lane} commit or native exit differs from the expected case")
        raw = (work / lane / "answer-cache.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["manifest_sha256"]:
            raise ValueError(f"{lane} manifest differs from the captured handoff")
        final = [(row["caller"], row["body"]) for row in json.loads(raw)["observations"] if row["stage"] == "final"]
        if sorted(final) != [("owner", "private-answer"), ("reviewer", revoked_answer)]:
            raise ValueError(f"{lane} does not show the expected final response for each caller")
        result = subprocess.run(["setrya", "replay", lane], cwd=work, timeout=30)
        if result.returncode != expected_exit:
            raise ValueError(f"{lane} replay exited {result.returncode}, expected {expected_exit}")
        review.extend([f"--{lane}-commit", entry["commit"]])

    review.extend(["--base-exit", "1", "--candidate-exit", "0", "--exit-contract", "setrya"])
    if f"Review again  {shlex.join(review)}" not in report.decode().splitlines():
        raise ValueError("the report must give the exact replay command")
    decision = subprocess.check_output(review, cwd=work, text=True, timeout=30)
    if not decision.startswith("FIX VERIFIED  "):
        raise ValueError(f"comparison did not verify the fix: {decision}")
    print(decision, end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("published_report", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="setrya-offline-review-") as directory:
        verify(args.archive, args.published_report, Path(directory))
