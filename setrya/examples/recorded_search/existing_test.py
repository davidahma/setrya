"""An ordinary pytest test that records evidence while keeping its own worker hooks."""

import os
import subprocess

from fixture_app.support_search.app import ROUTE, SCOPE
from fixture_app.support_search.baseline import wait_for
from fixture_app.support_search.client import Client
from setrya.adapter import Checkpoint, Publication, ReadView, Receipt, SearchResult


def test_old_worker_cannot_restore_refund_policy(tmp_path, setrya_record):
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    with setrya_record(
        os.environ["SETRYA_REVIEW_DRILL"], test_id="refund-worker",
        report=os.environ["SETRYA_REVIEW_REPORT"], revision=revision,
    ) as recorder:
        app = Client(tmp_path, recorder.run_id, os.environ.get("SETRYA_REVIEW_MODE", "fixed"), "refund")
        try:
            _check(app, recorder)
        finally:
            app.close()


def _check(app, recorder):
    def search(probe):
        rules = probe.rules
        response = app.search(probe.question, rules.route, rules.scope, rules.top_k)
        return SearchResult(
            passages=tuple(hit["id"] for hit in response["hits"]),
            origins=tuple(hit["origin"] for hit in response["hits"]),
            view=ReadView(**response["view"]) if response["view"] else None,
            route=response["route"], scope=response["scope"],
        )

    app.seed()
    recorder.seeded()
    assert "refund@A" in recorder.search("refund-window", search).passages
    assert "delivery@1" in recorder.search("delivery", search).passages

    attempt = app.start_old()
    recorder.started(attempt)
    held = wait_for(app.checkpoint, 2, "the old worker did not pause")
    recorder.held(Checkpoint(**held))
    recorder.published(Publication(**app.publish()))
    result = recorder.search("refund-window", search)
    assert (result.route, result.scope) == (ROUTE, SCOPE)
    assert "refund@B" in result.passages
    recorder.search("delivery", search)

    app.release()
    recorder.released(attempt)
    receipt = wait_for(app.receipt, 2, "the released worker had no terminal receipt")
    recorder.receipt(Receipt(**receipt))
    refund = recorder.search("refund-window", search)
    delivery = recorder.search("delivery", search)
    assert "refund@B" in refund.passages and "refund@A" not in refund.passages
    assert "delivery@1" in delivery.passages
