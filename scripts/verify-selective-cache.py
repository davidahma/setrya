"""Verify the release fixture returned fresh answers and preserved the unrelated hit."""
import argparse
from pathlib import Path

from setrya.dependencies import AnswerDependencies
from setrya.examples.answer_cache.metrics import bundle_metrics
from setrya.offboarding.inputs import read_json


def verify(path, guarded):
    trace, _ = read_json(path / "selective-run.json", "selective cache journal")
    pending = {"run_id": trace["run_id"], "attempt": trace["task"], "receipt": None}
    if trace["receipt_checks"] != {"published": pending, "before_release": pending}:
        raise ValueError("selected task must still be pending across publication and caller reads")
    rows = trace["reads"]
    expected_stages = [(stage, scope) for stage in ("baseline", "published", "released", "refilled")
                       for scope in ("refund", "delivery")]
    if [(row["stage"], row["scope"]) for row in rows] != expected_stages:
        raise ValueError("expected eight HTTP observations in the recorded order")
    for row in rows:
        stage, scope = row["stage"], row["scope"]
        body = "delivery" if scope == "delivery" else ("old-refund" if stage == "baseline" else "new-refund")
        hit = stage == "baseline" or (guarded if scope == "delivery" else stage == "refilled")
        if (row["caller"] != "retained" or row["http_status"] != 200 or row["route"] != "answer"
                or row["body"] != body or row["cache_hit"] is not hit or row["generation_skipped"] is not hit):
            raise ValueError(f"{path.name}: unexpected answer or generation decision at {stage} for {scope}")
        record = AnswerDependencies.from_dict(row["dependencies"])
        if scope == "delivery" and guarded:
            if record.answer_id != rows[1]["dependencies"]["answer_id"] or row["cache_writer"] != rows[1]["cache_writer"]:
                raise ValueError("delivery hit must keep its original writer and answer ID")
    if (trace["old_receipt"]["attempt"] != trace["task"] or trace["old_receipt"]["outcome"] != "rejected"
            or trace["new_receipt"]["attempt"] != trace["refill"] or trace["new_receipt"]["outcome"] != "committed"
            or rows[-2]["cache_writer"] != trace["refill"]):
        raise ValueError("expected old refund fill rejection and subsequent fresh worker hit")
    counts = bundle_metrics([path]).snapshot()
    if (counts.requests, counts.checked, counts.stale_serves, counts.hits, counts.measured_generations,
        counts.skipped_generations, counts.rejected_fills) != (8, 8, 0, 6 if guarded else 3, 8, 6 if guarded else 3, 1):
        raise ValueError(f"{path.name}: recorded counters disagree with the eight endpoint responses")
    print(f"{path.name}: confirmed refund replacement, delivery outcome and generation counters")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    root = parser.parse_args().directory
    for name in ("broken", "guarded"):
        verify(root / name, name == "guarded")
