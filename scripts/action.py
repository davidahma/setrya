"""Run the installed CLI, or post a bounded reviewer summary without loading fixture code."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

LABELS = ("PASS", "FAIL", "ERROR", "NOT TESTED")
EXPECTED = {"PASS": {"OPEN_ITEMS", "READY_FOR_REVIEW"}, "FAIL": {"RESIDUAL_FOUND"},
            "ERROR": {"OPEN_ITEMS", "UNAVAILABLE"}, "NOT TESTED": {"OPEN_ITEMS", "UNAVAILABLE"}}


def summary(code, review):
    if code not in ("0", "1", "2", "3") or review not in EXPECTED[LABELS[int(code)]]:
        raise ValueError("invalid receipt summary")
    return f"**Setrya · {LABELS[int(code)]}** (exit {code})  \nCase review: `{review}`."


def append(variable, message):
    with Path(os.environ[variable]).open("a", encoding="utf-8") as out:
        print(message, file=out)


def run():
    folder = Path(os.environ["SETRYA_OUTPUT"]).absolute()
    review = "UNAVAILABLE"
    code = 2
    try:
        from setrya.receipts import replay
        if os.path.lexists(folder):
            raise ValueError("select a fresh output directory")
        args = ["--starter", os.environ["SETRYA_STARTER"], "--mode", os.environ["SETRYA_MODE"], "--output", str(folder)]
        result = subprocess.run([sys.executable, "-I", "-m", "setrya", "run", *args], check=False)
        if (folder / "erasure-receipt.json").is_file():
            receipt = replay(folder)
            technical = receipt["technical_verdict"]
            if result.returncode != LABELS.index(technical):
                raise ValueError("run and replay disagree")
            code, review = result.returncode, receipt["review_status"]
        elif result.returncode == 3:
            code = 3
        body = summary(str(code), review)
    except Exception as exc:
        print(f"Setrya action error ({type(exc).__name__}).", file=sys.stderr)
        code, review = 2, "UNAVAILABLE"
        body = summary("2", review)
    append("GITHUB_OUTPUT", f"exit-code={code}\nreview-status={review}")
    append("GITHUB_STEP_SUMMARY", body)
    return code


def comment():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        raise ValueError("use the comment operation only on a pull_request event")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    repo = os.environ["GITHUB_REPOSITORY"]
    number, run_id = event["number"], os.environ["GITHUB_RUN_ID"]
    if (type(number) is not int or number < 1 or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
            or not run_id.isascii() or not run_id.isdecimal() or event["pull_request"]["base"]["repo"]["full_name"] != repo):
        raise ValueError("invalid pull request coordinates")
    body = summary(os.environ["SETRYA_CODE"], os.environ["SETRYA_REVIEW"])
    body += f"\n\n[Open the run and download the receipt](https://github.com/{repo}/actions/runs/{run_id})."
    subprocess.run(["gh", "api", "--hostname", "github.com", "--method", "POST", f"repos/{repo}/issues/{number}/comments",
                    "--input", "-", "--silent"], input=json.dumps({"body": body}), text=True, check=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit({"run": run, "comment": comment}[sys.argv[1]]())
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Setrya action error ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(2)
