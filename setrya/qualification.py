"""Executable acceptance cases with expectations independent of the checker."""

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from .cli import EXAMPLE, PGVECTOR
from .checker import Status, evaluate
from .drill import Drill, InputError
from .runner import run


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def command(*args, timeout=15):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=timeout)


def outcome(completed, expected, text):
    require(completed.returncode == expected and text in completed.stdout + completed.stderr,
            f"expected exit {expected} and {text!r}, got {completed.returncode}\n"
            f"{completed.stdout}{completed.stderr}")


def scenario(mode, filename="drill.toml"):
    drill = Drill.load(EXAMPLE / filename)
    return run(replace(drill, options={**drill.options, "mode": mode}))


def configuration(directory, mode, filename="drill.toml", question=None, *, examples=EXAMPLE):
    path = Path(directory) / f"{mode}.toml"
    contents = (examples / filename).read_text(encoding="utf-8").replace('mode = "fixed"', f'mode = "{mode}"')
    if question is not None:
        contents = contents.replace("How long do I have to return an order?", question)
    path.write_text(contents, encoding="utf-8")
    return path


def check_status(result, expected, text):
    require(result.verdict.status is expected, f"{result.verdict.status.label}: {result.verdict.detail}")
    require(text in result.verdict.detail, result.verdict.detail)


def demo():
    started = monotonic()
    completed = command("-m", "setrya", "demo", timeout=120)
    outcome(completed, 0, "Old write rejected")
    require(monotonic() - started < 120, "demo exceeded the two-minute target")
    for text in ("FAIL", "PASS", "NOT TESTED", "30 days", "14 days", "visibility evidence",
                 "demo --case broken", "demo --case fixed", "demo --case opaque"):
        require(text in completed.stdout, f"demo omitted {text}")


def late_write():
    broken = scenario("broken")
    check_status(broken, Status.FAIL, "refund@A at rank")
    require("traces to old attempt" in broken.verdict.attribution, "missing correlated write attribution")
    probes = [event for event in broken.report.events if event.get("probe_id") == "refund-window"]
    require(any(event["stage"] == "freshness" and "refund@B" in event["passages"] for event in probes),
            "broken fixture never showed revision B before resume")
    require(any(event["stage"] == "final" and "refund@A" in event["passages"] for event in probes),
            "broken fixture never showed revision A after resume")
    check_status(scenario("fixed"), Status.PASS, "rejected")


def retired_chunks():
    broken = scenario("broken", "retired.toml")
    check_status(broken, Status.FAIL, "refund@A.5")
    fixed = scenario("fixed", "retired.toml")
    check_status(fixed, Status.PASS, "All final probes")
    for result in (broken, fixed):
        initial = [event for event in result.report.events
                   if event.get("probe_id") == "retired-clause" and event["stage"] == "baseline"]
        require(initial and "refund@A.5" in initial[0]["passages"], "retired material was absent before replacement")
    final = {event["probe_id"]: event["passages"] for event in fixed.report.events if event.get("stage") == "final"}
    require("refund@A.5" not in final["retired-clause"] and "refund@B" in final["retired-clause"]
            and "delivery@1" in final["delivery"], "fixed replacement or unrelated control failed")


def early_stale():
    result = scenario("early-stale")
    check_status(result, Status.FAIL, "refund@A")
    require(result.verdict.attribution == "Observed while the old worker was held", "incorrect attribution")
    release = next(event["seq"] for event in result.report.events if event["kind"] == "released")
    require(result.verdict.event < release, "decisive probe occurred after release")


def missing_evidence():
    opaque = scenario("opaque")
    check_status(opaque, Status.NOT_TESTED, "visibility evidence")
    final = [event for event in opaque.report.events
             if event.get("probe_id") == "refund-window" and event["stage"] == "final"]
    require(final and all("refund@B" in event["passages"] for event in final), "opaque view did not stay clean")
    exposed = scenario("lag-exposed")
    check_status(exposed, Status.FAIL, "refund@A")
    final = [event["passages"] for event in exposed.report.events
             if event.get("probe_id") == "refund-window" and event["stage"] == "final"]
    require(any("refund@B" in rows for rows in final[:-1]) and "refund@A" in final[-1],
            "lagging view did not expose both revisions in order")
    check_status(scenario("missing-identity"), Status.NOT_TESTED, "identity")
    check_status(scenario("missing-checkpoint"), Status.NOT_TESTED, "checkpoint acknowledgment")


def adapter_handoff():
    with TemporaryDirectory(prefix="setrya-handoff-") as directory:
        for mode, code, text in (("fixed", 0, "PASS"), ("broken", 1, "refund@A at rank")):
            path = configuration(directory, mode)
            outcome(command("-m", "setrya", "check", str(path)), code, text)
            for flags in ((), ("-O",)):
                outcome(command(*flags, "-m", "examples.support_search.baseline", "--mode", mode), code, text)
    outcome(command("-m", "setrya", "check", str(EXAMPLE / "python.toml")), 0, "PASS")


def report_replay():
    secret = "SetryaQA-private-query"
    with TemporaryDirectory(prefix="setrya-replay-") as directory:
        artifact = Path(directory) / "run.json"
        summary = Path(directory) / "summary.md"
        for mode, code, detail in (("fixed", 0, "rejected"), ("broken", 1, "traces to old attempt"),
                                   ("opaque", 3, "visibility evidence")):
            path = configuration(directory, mode, question=f"return order {secret}")
            original = command("-m", "setrya", "check", str(path), "--report", str(artifact),
                               "--summary", str(summary))
            outcome(original, code, detail)
            summary_text = summary.read_text()
            require("setrya replay" in summary_text, "review summary omitted replay command")
            if mode == "fixed":
                require("Probe refund-window" in summary_text and "Published refund revision B" in summary_text,
                        "review summary omitted the primary probe or published revision")
            for artifact_text in (original.stdout, original.stderr, artifact.read_text(), summary.read_text()):
                require(all(text not in artifact_text for text in (secret, "14 days", "30 days", "127.0.0.1", directory)),
                        "default output contained application text or local addresses")
            path.write_text("changed drill that no longer parses", encoding="utf-8")
            replay = command("-m", "setrya", "replay", str(artifact))
            outcome(replay, code, detail)
            require(replay.stdout.splitlines()[0] == original.stdout.splitlines()[0], "replay changed the verdict")
        data = json.loads(artifact.read_text())
        data["schema"] = 999
        artifact.write_text(json.dumps(data), encoding="utf-8")
        outcome(command("-m", "setrya", "replay", str(artifact)), 2, "unsupported report schema")
        artifact.write_text("not JSON", encoding="utf-8")
        outcome(command("-m", "setrya", "replay", str(artifact)), 2, "could not read report")
        outcome(command("-m", "setrya", "check", str(path)), 2, "could not read drill")


def report_comparison():
    with TemporaryDirectory(prefix="setrya-compare-") as directory:
        target = Path(directory)
        reports = {}
        for mode, code in (("broken", 1), ("fixed", 0), ("opaque", 3)):
            path = configuration(directory, mode)
            report = target / f"{mode}.json"
            outcome(command("-m", "setrya", "check", str(path), "--report", str(report)), code, "")
            path.unlink()
            reports[mode] = str(report)
        summary = target / "compare.md"
        checked = command("-m", "setrya", "compare", reports["broken"], reports["fixed"], "--summary", str(summary))
        outcome(checked, 0, "FIX VERIFIED")
        require("support-agent" in summary.read_text() and "Before FAIL · event" in summary.read_text(),
                "comparison did not show the caller and decisive failing observation")
        outcome(command("-m", "setrya", "compare", reports["fixed"], reports["broken"]), 1, "REGRESSION")
        outcome(command("-m", "setrya", "compare", reports["broken"], reports["opaque"]), 3, "visibility evidence")
        outcome(command("-m", "setrya", "diagnose", reports["opaque"]), 3, "Missing search visibility evidence")
        for old, new, detail in (("How long do I have to return an order?", "Can I return an order?", "Question changed"),
                                 ("top_k = 3", "top_k = 1", "Search depth changed")):
            path = configuration(directory, "fixed")
            path.write_text(path.read_text().replace(old, new, 1))
            report = target / "changed.json"
            outcome(command("-m", "setrya", "check", str(path), "--report", str(report)), 0, "PASS")
            if detail.startswith("Question"):
                require(new not in report.read_text(), "report included raw query text")
            outcome(command("-m", "setrya", "compare", reports["broken"], str(report)), 3, detail)
        legacy = json.loads(Path(reports["fixed"]).read_text())
        legacy.update(schema=2, checker=2)
        legacy.pop("inputs")
        Path(reports["fixed"]).write_text(json.dumps(legacy))
        outcome(command("-m", "setrya", "replay", reports["fixed"]), 0, "PASS")
        outcome(command("-m", "setrya", "compare", reports["broken"], reports["fixed"]), 3, "input fingerprints")


def pgvector():
    with TemporaryDirectory(prefix="setrya-vector-qualification-") as directory:
        reports = {}
        for filename, failure_text in (("drill.toml", "refund@A at rank"), ("reupload.toml", "refund@B.3 was absent")):
            for mode, code, detail in (("broken", 1, failure_text), ("fixed", 0, "PASS"),
                                       ("opaque", 3, "Missing search visibility evidence")):
                path = configuration(directory, mode, filename, examples=PGVECTOR)
                report = Path(directory) / f"{filename}-{mode}.json"
                outcome(command("-m", "setrya", "check", str(path), "--report", str(report)), code, detail)
                reports[mode] = report
            outcome(command("-m", "setrya", "compare", str(reports["broken"]), str(reports["fixed"])), 0, "FIX VERIFIED")
            outcome(command("-m", "setrya", "diagnose", str(reports["opaque"])), 3, "Missing search visibility evidence")
            if filename == "reupload.toml":
                broken = json.loads(reports["broken"].read_text())
                fixed = json.loads(reports["fixed"].read_text())

                def final_probes(bundle):
                    return {event["probe_id"]: event["passages"] for event in bundle["events"]
                            if event.get("stage") == "final"}

                observed = final_probes(broken)
                require(observed["refund-window"] == ["refund@B", "refund@B.2"],
                        "broken ranked response did not contain exactly two current passages")
                require(observed["refund-broad"] == ["refund@B", "refund@B.2", "refund@B.3"],
                        "the broad search did not still find the missing top-three passage")
                expected = final_probes(fixed)
                require(expected["refund-window"] == observed["refund-broad"] and expected["delivery"] == ["delivery@1"],
                        "guarded write did not restore all three passages and preserve the one-result control")


def _wait_for_fixture(temp, process):
    deadline = monotonic() + 5
    while process.poll() is None and monotonic() < deadline:
        directories = list(temp.glob("setrya-*"))
        if len(directories) == 1 and (directories[0] / "port").exists():
            return directories[0]
        sleep(0.005)
    raise AssertionError("could not observe concurrent HTTP fixture")


def _wait_for_held_worker(directory, process):
    database = directory / "support.sqlite"
    deadline = monotonic() + 5
    while process.poll() is None and monotonic() < deadline:
        if database.is_file():
            try:
                with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=0.2)) as db:
                    row = db.execute("SELECT status FROM queue").fetchone()
                    if row and row[0] == "held":
                        return
            except sqlite3.OperationalError:
                # The subprocess may still be initializing the database.
                pass
        sleep(0.01)
    raise AssertionError("first worker did not acknowledge the held checkpoint")


def isolation():
    with TemporaryDirectory(prefix="setrya-isolation-") as directory:
        temp = Path(directory)
        blocked_path = configuration(temp, "missing-checkpoint")
        blocked_path.write_text(blocked_path.read_text().replace("checkpoint_ms = 2000", "checkpoint_ms = 8000"))
        fixed_path = configuration(temp, "fixed")
        first_report, second_report = temp / "first.json", temp / "second.json"

        def start(path, report):
            return subprocess.Popen([sys.executable, "-m", "setrya", "check", str(path), "--report", str(report)],
                                    env={**os.environ, "TMPDIR": directory},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        first = start(blocked_path, first_report)
        second = None
        try:
            held_dir = _wait_for_fixture(temp, first)
            _wait_for_held_worker(held_dir, first)
            second = start(fixed_path, second_report)
            timeout = monotonic() + 5
            other_dirs = []
            while monotonic() < timeout and second.poll() is None:
                other_dirs = [path for path in temp.glob("setrya-*")
                              if path != held_dir and (path / "port").exists()]
                if other_dirs:
                    break
                sleep(0.005)
            require(len(other_dirs) == 1 and first.poll() is None, "fixtures did not overlap")
            port = (held_dir / "port").read_text()
            require(port != (other_dirs[0] / "port").read_text(), "concurrent fixtures shared a port")
            first.send_signal(signal.SIGINT)
            stdout, stderr = first.communicate(timeout=5)
            require(first.returncode == 2 and "interrupted" in stderr.lower(), f"interruption was not actionable: {stdout}{stderr}")
            stdout, stderr = second.communicate(timeout=8)
            require(second.returncode == 0 and "PASS" in stdout, f"other run failed: {stdout}{stderr}")
            require(second_report.exists() and not first_report.exists() and not list(temp.glob("setrya-*")),
                    "fixture cleanup or output isolation failed")
            restarted = start(fixed_path, first_report)
            try:
                stdout, stderr = restarted.communicate(timeout=8)
                require(restarted.returncode == 0 and first_report.exists(), f"restart failed: {stdout}{stderr}")
            finally:
                if restarted.poll() is None:
                    restarted.kill()
                    restarted.communicate(timeout=3)
            require(json.loads(first_report.read_text())["run_id"] != json.loads(second_report.read_text())["run_id"],
                    "concurrent and restarted reports shared a run ID")
        finally:
            for proc in (first, second):
                if proc is not None and proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=3)


def offboarding():
    """Replay the two offboarding decisions after the example application exits."""
    cli = ("-m", "setrya", "offboarding")
    with TemporaryDirectory(prefix="setrya-offboarding-") as directory:
        bundle = Path(directory) / "example"
        outcome(command(*cli, "demo", "--output", bundle, timeout=45), 0, "")
        for case, code, verdict in (("broken", 1, "FAIL"), ("fixed", 0, "PASS"),
                                    ("inconclusive", 2, "INCONCLUSIVE")):
            replay = command(*cli, "gate", "--policy", bundle / "policy.json",
                             "--observations", bundle / f"{case}.json", "--expect-run", case)
            outcome(replay, code, f"Gate {verdict}")
        case_dir = bundle / "receipt"
        checked = command(*cli, "verify-receipt", "--policy", case_dir / "policy.json",
                          "--request", case_dir / "request.json", "--receipt", case_dir / "receipt.json",
                          "--expect-request", (case_dir / "request.sha256").read_text().strip(),
                          "--evidence", case_dir / "evidence.json",
                          "--record", case_dir / "provider.json", case_dir / "provider.txt",
                          "--record", case_dir / "backups.json", case_dir / "backups.txt")
        outcome(checked, 2, "Receipt verified.")
        require("Technical verdict PASS" in checked.stdout and "Review status OPEN_ITEMS" in checked.stdout,
                "offboarding replay lost the distinction between a technical pass and an open review")


def offboarding_compatibility():
    """Exercise the independently runnable producers and offline report readers."""
    cli = ("-m", "setrya", "offboarding")
    with TemporaryDirectory(prefix="setrya-compatibility-") as directory:
        root = Path(directory)
        reference = root / "reference.json"
        outcome(command(*cli, "reference-demo", "--output", reference), 0, "")
        outcome(command(*cli, "validate", reference), 0, "Report valid")
        policy = root / "policy.json"
        exported = command(*cli, "sqlite-policy")
        outcome(exported, 0, "sqlite-support")
        policy.write_text(exported.stdout, encoding="utf-8")
        packet = root / "reads.json"
        for case, code, expected in (("clean", 0, "PASS"), ("residual", 1, "FAIL"),
                                     ("unavailable", 2, "INCONCLUSIVE")):
            outcome(command(*cli, "observe-sqlite", "--case", case, "--run-id", case, "--output", packet), 0, "")
            outcome(command(*cli, "gate", "--policy", policy, "--observations", packet, "--expect-run", case),
                    code, f"Gate {expected}")
        original = EXAMPLE.parent / "opendesk" / "observed.json"
        outcome(command(*cli, "opendesk", "--verify-report", original), 0, "report matches")
        outcome(command(*cli, "opendesk", "--from-report", original, "--output", root / "trial"),
                2, "Gate INCONCLUSIVE")


def repeatability():
    with TemporaryDirectory(prefix="setrya-repeat-") as directory:
        for mode, code, verdict in (("broken", 1, "FAIL"), ("fixed", 0, "PASS"), ("opaque", 3, "NOT TESTED")):
            path = configuration(directory, mode)
            durations = []
            for sample in range(1, 31):
                started = monotonic()
                try:
                    outcome(command("-m", "setrya", "check", str(path)), code, verdict)
                except (AssertionError, subprocess.TimeoutExpired) as error:
                    raise AssertionError(f"{mode} first-attempt sample {sample}/30 failed: {error}") from error
                durations.append(monotonic() - started)
            print(f"  {mode} 30/30 first attempts · {min(durations):.2f}–{max(durations):.2f} s", flush=True)


CASES = {
    "demo": demo, "late-write": late_write, "retired-chunks": retired_chunks, "early-stale": early_stale,
    "missing-evidence": missing_evidence, "adapter-handoff": adapter_handoff, "report-replay": report_replay,
    "report-comparison": report_comparison, "isolation": isolation, "offboarding": offboarding,
    "offboarding-compatibility": offboarding_compatibility,
    "repeatability": repeatability,
}


def qualify(selected=None, quick=False):
    available = {**CASES, "pgvector": pgvector}
    if selected and selected not in available:
        raise InputError(f"unknown case. Choose from {', '.join(available)}.")
    names = [selected] if selected else [name for name in CASES if not quick or name != "repeatability"]
    failed = False
    for name in names:
        started = monotonic()
        try:
            available[name]()
        except (AssertionError, subprocess.TimeoutExpired) as error:
            print(f"FAIL  {name} · {error}", flush=True)
            failed = True
        else:
            print(f"PASS  {name} · {monotonic() - started:.2f} s", flush=True)
    return 1 if failed else 0
