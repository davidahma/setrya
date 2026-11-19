"""Plain pytest comparator for the same HTTP endpoints, hooks and queries."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from setrya.examples.support_search.app import CHECKPOINT, ROUTE, SCOPE
from setrya.examples.support_search.baseline import ids, verify, wait_for
from setrya.examples.support_search.client import Client
from . import DELIVERY_QUESTION, MODULE, REFUND_QUESTION, REUPLOAD_QUESTION

# This is an independent assertion set. It uses the same app as the TOML drills.
_EXPECTED = {
    "refund": (("refund-window", REFUND_QUESTION, 3, ("refund@B",), "refund@A"),),
    "reupload": (
        ("refund-window", REUPLOAD_QUESTION, 3, ("refund@B", "refund@B.2", "refund@B.3"), "refund@A"),
        ("refund-broad", REUPLOAD_QUESTION, 10, ("refund@B", "refund@B.2", "refund@B.3"), "refund@A"),
    ),
}


def test_indexing_change():
    scenario = os.environ.get("SETRYA_BENCH_SCENARIO", "refund")
    mode = os.environ.get("SETRYA_BENCH_MODE", "fixed")
    primary, *additional = _EXPECTED[scenario]
    probes = (primary, ("delivery", DELIVERY_QUESTION, 3, ("delivery@1",), None), *additional)
    started = monotonic()
    run_id = uuid4().hex
    with TemporaryDirectory(prefix="setrya-plain-") as directory:
        client = Client(Path(directory), run_id, mode, scenario, module=MODULE)
        try:
            _check(client, run_id, probes, started)
        finally:
            client.close()


def _check(client, run_id, probes, started):
    client.seed()
    for name, question, depth, required, retired in probes:
        observed = ids(client.search(question, ROUTE, SCOPE, depth))
        expected = {retired} if retired else set(required)
        verify(expected <= set(observed), f"{name} baseline is missing {', '.join(sorted(expected - set(observed)))}")
        if retired:
            verify(set(required).isdisjoint(observed), f"{name} baseline already contains a replacement marker")

    attempt = client.start_old()
    held = wait_for(client.checkpoint, 4, "checkpoint did not acknowledge the held worker")
    verify(held == {"run_id": run_id, "document": "refund", "revision": "A",
                    "attempt": attempt, "name": CHECKPOINT}, "checkpoint identified the wrong attempt")
    print(f"Fixture ready seconds: {monotonic() - started:.6f}")
    verify(client.publish() == {"run_id": run_id, "document": "refund", "revision": "B"},
           "publication did not acknowledge revision B")
    name, question, depth, required, retired = probes[0]

    def fresh():
        observed = ids(client.search(question, ROUTE, SCOPE, depth))
        return observed if set(required) <= set(observed) else None

    verify(retired not in wait_for(fresh, 1.2, f"{name} missed the freshness deadline"),
           f"{name} freshness returned the retired passage")
    for name, question, depth, _, retired in probes:
        observed = ids(client.search(question, ROUTE, SCOPE, depth))
        verify(retired is None or retired not in observed, f"{name} freshness returned {retired} before release")

    client.release()
    deadline = monotonic() + 3
    receipt = wait_for(client.receipt, max(0, deadline - monotonic()), "old attempt supplied no terminal receipt")
    verify(all(receipt[key] == held[key] for key in ("run_id", "document", "revision", "attempt")),
           "write receipt identified the wrong attempt")
    for name, question, depth, required, retired in probes:
        while True:
            response = client.search(question, ROUTE, SCOPE, depth)
            observed = ids(response)
            if retired is not None and retired in observed:
                rank = observed.index(retired) + 1
                raise AssertionError(f"{name} final returned {retired} at rank {rank}")
            view = response["view"]
            if receipt["outcome"] == "rejected" or (
                view and view["generation"] == receipt["generation"] and view["commit"] >= receipt["commit"]
                and (view["route"], view["scope"]) == (ROUTE, SCOPE)
            ):
                missing = set(required) - set(observed)
                verify(monotonic() <= deadline, f"{name} final search missed the deadline")
                verify(None not in observed, f"{name} final search lacks passage identity")
                verify(not missing, f"{name} final missing {', '.join(sorted(missing))}. Returned {', '.join(observed)}")
                break
            verify(monotonic() < deadline, f"{name} final search could not establish write visibility")
