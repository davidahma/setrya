"""Plain integration test for the same late-write defect. No Setrya imports."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from .app import ATTEMPT, CHECKPOINT, ROUTE, SCOPE
from .client import Client

REFUND = "How long do I have to return an order?"
DELIVERY = "When can I track my delivery?"


def wait_for(observe, seconds, message):
    deadline = monotonic() + seconds
    while monotonic() <= deadline:
        result = observe()
        if result is not None and monotonic() <= deadline:
            return result
        sleep(0.02)
    raise AssertionError(message)


def ids(result):
    return [hit["id"] for hit in result["hits"]]


def check(mode="fixed"):
    run_id = uuid4().hex
    with TemporaryDirectory(prefix="support-baseline-") as directory:
        client = Client(Path(directory), run_id, mode, "refund")
        try:
            client.seed()
            assert "refund@A" in ids(client.search(REFUND, ROUTE, SCOPE, 3)), "baseline refund passage was absent"
            assert "delivery@1" in ids(client.search(DELIVERY, ROUTE, SCOPE, 3)), "baseline delivery passage was absent"
            attempt = client.start_old()
            held = wait_for(client.checkpoint, 2.0, "old attempt never reached the read checkpoint")
            assert held == {"run_id": run_id, "attempt": attempt, "document": "refund",
                            "revision": "A", "name": CHECKPOINT}, "checkpoint exposed the wrong attempt"
            client.publish()

            def fresh():
                result = client.search(REFUND, ROUTE, SCOPE, 3)
                return result if "refund@B" in ids(result) else None

            assert "refund@A" not in ids(wait_for(fresh, 1.2, "revision B missed the freshness deadline")), (
                "revision A was present alongside B"
            )
            assert "refund@A" not in ids(client.search(REFUND, ROUTE, SCOPE, 3)), (
                "revision A returned while the old attempt was held"
            )
            client.release()
            deadline = monotonic() + 2.0
            receipt = wait_for(client.receipt, max(0, deadline - monotonic()), "old attempt had no terminal receipt")
            assert all(receipt[key] == held[key] for key in ("run_id", "attempt", "document", "revision")), (
                "terminal receipt exposed the wrong attempt"
            )
            for question, required, forbidden in ((REFUND, "refund@B", "refund@A"),
                                                  (DELIVERY, "delivery@1", None)):
                while True:
                    result = client.search(question, ROUTE, SCOPE, 3)
                    observed = ids(result)
                    assert forbidden is None or forbidden not in observed, (
                        f"expected {required}. Observed {forbidden} at rank {observed.index(forbidden) + 1}."
                    )
                    view = result["view"]
                    if receipt["outcome"] == "rejected" or (
                        view and view["generation"] == receipt["generation"] and view["commit"] >= receipt["commit"]
                        and view["route"] == ROUTE and view["scope"] == SCOPE
                    ):
                        assert monotonic() <= deadline, "final search missed the deadline"
                        assert None not in observed and required in observed, f"expected {required}. Observed {observed}."
                        break
                    assert monotonic() < deadline, "search visibility was unverified"
                    sleep(0.02)
        finally:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fixed", "broken"], default="fixed")
    args = parser.parse_args()
    try:
        check(args.mode)
    except AssertionError as error:
        print(f"FAIL  {error}")
        return 1
    print(f"PASS  the old write did not displace revision B (attempt {ATTEMPT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
