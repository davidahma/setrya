"""Start the disposable local application and run its ordinary Setrya HTTP case."""

import os
import re
import sys
from argparse import ArgumentParser
from pathlib import Path
from secrets import token_hex
from shutil import which
from subprocess import Popen, TimeoutExpired, run
from tempfile import TemporaryDirectory
from time import monotonic, sleep


def wait_for_origin(app, output):
    deadline = monotonic() + 10
    while app.poll() is None and monotonic() < deadline:
        match = re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}\n", output.read_text(encoding="utf-8"))
        if match:
            return match[0].strip()
        sleep(0.05)
    raise SystemExit("The disposable app did not start. Check its output and use Node.js 24 LTS or later.")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("guarded", "broken"), default="guarded")
    parser.add_argument("--output", required=True, type=Path, help="fresh receipt directory")
    args = parser.parse_args()
    node = which("node")
    if not node:
        parser.error("Install Node.js 24 LTS or later and put node on PATH.")
    folder = Path(__file__).resolve().parent
    setrya = [sys.executable, "-I", "-m", "setrya"]
    with TemporaryDirectory(prefix="setrya-typescript-") as temp:
        root = Path(temp)
        env = dict(os.environ, SETRYA_TEST_DB=str(root / "notes.sqlite"), SETRYA_SUBJECT_REF=token_hex(32),
                   SETRYA_READER_TOKEN=token_hex(24), SETRYA_REVIEWER_TOKEN=token_hex(24),
                   SETRYA_ERASER_TOKEN=token_hex(24), SETRYA_FIXTURE_MODE=args.mode)
        env.pop("SETRYA_COPY_JOURNAL", None)
        output = root / "origin"
        with output.open("w", encoding="utf-8") as stdout, Popen(
            [node, str(folder / "fixture.mts")], cwd=root, env=env, stdout=stdout
        ) as app:
            try:
                env["SETRYA_TEST_ORIGIN"] = wait_for_origin(app, output)
                doctor = run([*setrya, "doctor", "--case", str(folder / "case.yml")], check=False, env=env)
                if doctor.returncode:
                    return doctor.returncode
                return run([*setrya, "run", str(folder / "case.yml"), "--output", str(args.output)],
                           check=False, env=env).returncode
            finally:
                if app.poll() is None:
                    app.terminate()
                    try:
                        app.wait(timeout=5)
                    except TimeoutExpired:
                        app.kill()


if __name__ == "__main__":
    raise SystemExit(main())
