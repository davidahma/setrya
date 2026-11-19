"""Check that the shipped synthetic drill demonstrated the intended late-fill race."""
import argparse
from pathlib import Path

from setrya.evidence import Report
from setrya.examples.answer_cache.evidence import replay
from setrya.offboarding.inputs import read_json
from setrya.status import Status


def verify(directory, mode):
    path = directory / mode
    verdict, _ = replay(path)
    report = Report.load(path / "run.json")
    trace, _ = read_json(path / "answer-cache.json", "late cache fill")
    broken = mode == "broken"
    expected = Status.FAIL if broken else Status.PASS
    if verdict.status != expected or trace["mode"] != mode:
        raise ValueError(f"{mode}: expected {expected.label} for the selected worker policy")
    if trace.get("version") != 2 or trace.get("receipt_before_release") is not False:
        raise ValueError(f"{mode}: missing the pending receipt check before release")
    attempt = trace["worker_capture"]["attempt"]
    receipt = next(event for event in report.events if event["kind"] == "receipt")
    if (receipt["attempt"] != attempt or trace["held_after_reads"]["attempt"] != attempt
            or receipt["outcome"] != ("committed" if broken else "rejected")):
        raise ValueError(f"{mode}: captured task, hold and write outcome must agree")

    rows = trace["observations"]
    for stage in ("baseline", "freshness", "final"):
        samples = [r for r in rows if r["stage"] == stage]
        if {r["caller"] for r in samples} != {"revoked", "retained"}:
            raise ValueError(f"{mode}: both callers must be observed at {stage}")
        for row in samples:
            late = broken and stage == "final" and row["caller"] == "revoked"
            private = stage == "baseline" or row["caller"] == "retained" or late
            if (row["status"] != 200 or row["route"] != "answer" or row["cache_hit"] is not private
                    or row["body"] != ("private-and-public" if private else "public-only")):
                raise ValueError(f"{mode}: unexpected {stage} answer for {row['caller']}")
            if late and (row["cache_writer"] != attempt or row["generation"] != receipt["generation"]
                         or row["commit"] is None or row["commit"] < receipt["commit"]):
                raise ValueError("the broken endpoint must serve the selected task's committed answer")
    print(f"{mode}: actual task outcome and both callers' HTTP answers confirmed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory containing broken and guarded bundles")
    root = parser.parse_args().directory
    for policy in ("broken", "guarded"):
        verify(root, policy)
