"""Run the same two changes through Setrya and ordinary pytest."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from .app import connection

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "pgvector-search"
SCENARIOS = {"refund": ("drill.toml", "refund@A at rank"), "reupload": ("reupload.toml", "refund@B.3")}


def measure(args, log, *, env=None):
    started = monotonic()
    completed = subprocess.run([sys.executable, *args], env=env, text=True, capture_output=True, timeout=30)
    elapsed = monotonic() - started
    log.write_text(completed.stdout + completed.stderr)
    return completed, elapsed


def verify_result(completed, expected, label, detail=None):
    if completed.returncode != expected or (detail and detail not in completed.stdout):
        raise RuntimeError(f"{label} did not produce the expected result. See its log.")


def sample(output, number, scenario, filename, failure_detail, configurations):
    settings = (EXAMPLES / filename).read_text()
    directory = output / scenario / f"trial-{number}"
    directory.mkdir(parents=True)
    lanes = ("setrya", "pytest") if number % 2 else ("pytest", "setrya")
    measurements = []
    for mode, expected in (("broken", 1), ("fixed", 0)):
        config = configurations / f"{scenario}-{mode}.toml"
        config.write_text(settings.replace('mode = "fixed"', f'mode = "{mode}"'))
        for lane in lanes:
            if lane == "setrya":
                report = directory / f"{mode}.json"
                command = ["-m", "setrya", "check", str(config), "--report", str(report)]
                result, elapsed = measure(command, directory / f"setrya-{mode}.txt")
                verify_result(result, expected, f"Setrya {scenario} {mode}", failure_detail if expected else None)
                events = json.loads(report.read_text())["events"]
                setup = next(event["ns"] for event in events if event["kind"] == "held") / 1e9
            else:
                path = Path(__file__).with_name("pytest_reference.py")
                env = {**os.environ, "SETRYA_BENCH_SCENARIO": scenario, "SETRYA_BENCH_MODE": mode,
                       "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
                result, elapsed = measure(
                    ["-m", "pytest", "-qs", "--tb=short", str(path)], directory / f"pytest-{mode}.txt", env=env
                )
                verify_result(result, expected, f"pytest {scenario} {mode}", failure_detail if expected else None)
                setup = float(re.search(r"Fixture ready seconds: ([0-9.]+)", result.stdout).group(1))
            measurements.append({"trial": number, "scenario": scenario, "mode": mode, "lane": lane,
                                 "fixture_to_checkpoint_seconds": round(setup, 6),
                                 "command_wall_seconds": round(elapsed, 6), "exit_code": result.returncode})

    result, elapsed = measure(["-m", "setrya", "compare", str(directory / "broken.json"), str(directory / "fixed.json")],
                              directory / "compare.txt")
    verify_result(result, 0, f"offline comparison for {scenario}", "FIX VERIFIED")
    return measurements, {"trial": number, "scenario": scenario, "compare_wall_seconds": round(elapsed, 6)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 30:
        parser.error("repeats must be between 1 and 30")
    args.output.mkdir(parents=True)
    with connection("public") as db:
        database = db.execute(
            "SELECT current_setting('server_version') AS postgresql, extversion AS pgvector "
            "FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    if database is None:
        parser.error("enable the vector extension first")
    observations, handoffs = [], []
    with TemporaryDirectory(prefix="setrya-comparison-") as temporary:
        for trial in range(1, args.repeats + 1):
            for scenario, (filename, failure_detail) in SCENARIOS.items():
                measures, handoff = sample(args.output, trial, scenario, filename, failure_detail, Path(temporary))
                observations.extend(measures)
                handoffs.append(handoff)
    (args.output / "measurements.json").write_text(json.dumps({
        "environment": {**database, "platform": platform.platform(), "python": platform.python_version()},
        "scope": "Warm disposable database, synthetic fixtures, process timings",
        "observations": observations, "offline_handoffs": handoffs,
    }, indent=2) + "\n")
    lines = ["# Setrya and plain pytest", "",
             "Medians in seconds across synthetic trials with a running PostgreSQL service.", "",
             f"PostgreSQL {database['postgresql']} · pgvector {database['pgvector']} · Python {platform.python_version()}", "",
             "| Change | Tool | Mode | Fixture to checkpoint | Whole command |", "| --- | --- | --- | ---: | ---: |"]
    for scenario in SCENARIOS:
        for lane in ("setrya", "pytest"):
            for mode in ("broken", "fixed"):
                group = [row for row in observations if (row["scenario"], row["lane"], row["mode"]) == (scenario, lane, mode)]
                setup = median(row["fixture_to_checkpoint_seconds"] for row in group)
                elapsed = median(row["command_wall_seconds"] for row in group)
                lines.append(f"| {scenario} | {lane} | {mode} | {setup:.3f} | {elapsed:.3f} |")
    replay_time = median(row["compare_wall_seconds"] for row in handoffs)
    lines += ["", f"Median offline comparison took {replay_time:.3f} seconds.",
              "Human setup, diagnosis, and reuse times were not measured."]
    (args.output / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
