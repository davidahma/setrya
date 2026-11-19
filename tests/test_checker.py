import unittest
from unittest.mock import patch

from examples.support_search.__main__ import FixtureServer, Handler

from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE, preflight
from setrya.drill import Drill, InputError
from setrya.evidence import Report


class History:
    def __init__(self):
        self.rules = Drill.load(EXAMPLE / "drill.toml").rules
        self.events = []
        self.identity = {"run_id": "run", "attempt": "attempt", "document": "refund", "revision": "A"}
        self.add("seeded", 0)
        self.probe(2, "baseline", ["refund@A"])
        self.probe(4, "baseline", ["delivery@1"], "delivery")
        self.add("started", 6, attempt="attempt")
        self.add("held", 8, **self.identity, name="after_source_read")
        self.add("published", 10, publication={"run_id": "run", "document": "refund", "revision": "B"})

    def add(self, kind, ms, **evidence):
        self.events.append({"kind": kind, "seq": len(self.events) + 1, "ns": ms * 1_000_000, **evidence})

    def probe(self, ms, stage, markers, name="refund-window", *, start=None, view=None, origins=None):
        self.add("probe", ms, probe_id=name, stage=stage, passages=markers, view=view,
                 started_ns=(ms - 1 if start is None else start) * 1_000_000,
                 origins=[None] * len(markers) if origins is None else origins,
                 route="support-search", scope="support-agent")

    def resume(self):
        self.probe(40, "freshness", ["refund@B"])
        self.add("released", 50, attempt="attempt")

    def receipt(self, outcome="committed", **details):
        fields = {"outcome": outcome, "generation": "generation", "commit": 3} if outcome == "committed" else {
            "outcome": outcome, "generation": None, "commit": None,
        }
        self.add("receipt", 60, **{**self.identity, **fields, **details})

    def verdict(self):
        return evaluate(Report("run", self.rules, tuple(self.events)))


def view(commit=3, generation="generation", scope="support-agent"):
    return {"generation": generation, "commit": commit, "route": "support-search", "scope": scope}


class VerdictTests(unittest.TestCase):
    def test_clean_results_with_write_proof_pass(self):
        for mode in ("committed", "rejected"):
            with self.subTest(mode=mode):
                history = History()
                history.resume()
                history.receipt(mode)
                history.probe(80, "final", ["refund@B"], view=view() if mode == "committed" else None)
                history.probe(82, "final", ["delivery@1"], "delivery", view=view(4))
                self.assertIs(history.verdict().status, Status.PASS)

    def test_generation_commit_and_scope_are_bound_to_each_probe(self):
        for wrong in (None, view(2), view(generation="other"), view(scope="different")):
            with self.subTest(wrong=wrong):
                history = History()
                history.resume()
                history.receipt()
                history.probe(80, "final", ["refund@B"], view=view())
                history.probe(82, "final", ["delivery@1"], "delivery", view=wrong)
                self.assertIs(history.verdict().status, Status.NOT_TESTED)

    def test_receipt_from_another_attempt_is_insufficient(self):
        history = History()
        history.resume()
        history.receipt("rejected", attempt="other")
        history.probe(80, "final", ["refund@B"])
        history.probe(82, "final", ["delivery@1"], "delivery")
        self.assertIs(history.verdict().status, Status.NOT_TESTED)

    def test_first_stale_observation_persists_and_can_predate_resume(self):
        history = History()
        history.probe(40, "freshness", ["refund@B"])
        history.probe(43, "freshness", ["other", "refund@A"])
        history.add("released", 50, attempt="attempt")
        history.receipt("rejected")
        history.probe(80, "final", ["refund@B"])
        history.probe(82, "final", ["delivery@1"], "delivery")
        verdict = history.verdict()
        self.assertIs(verdict.status, Status.FAIL)
        self.assertEqual(verdict.event, 8)
        self.assertIn("rank 2", verdict.detail)
        self.assertIn("while the old worker was held", verdict.attribution)

    def test_stale_hit_attributed_only_with_matching_snapshot_and_origin(self):
        for origins in (["attempt"], ["seed"]):
            with self.subTest(origins=origins):
                history = History()
                history.resume()
                history.receipt()
                history.probe(80, "final", ["refund@A"], view=view(), origins=origins)
                verdict = history.verdict()
                self.assertIs(verdict.status, Status.FAIL)
                self.assertEqual("traces to old attempt" in verdict.attribution, origins == ["attempt"])

    def test_a_query_from_wrong_scope_cannot_supply_a_violation(self):
        history = History()
        history.resume()
        history.receipt()
        history.probe(80, "final", ["refund@A"], view=view(scope="unrelated"))
        self.assertIs(history.verdict().status, Status.NOT_TESTED)

    def test_observed_breach_and_gap_are_distinct(self):
        for pre, expected in ((1202, Status.FAIL), (600, Status.NOT_TESTED)):
            for sampled in (["refund@A"], ["refund@B"]):
                with self.subTest(pre=pre, sampled=sampled):
                    history = History()
                    history.probe(pre, "freshness", ["refund@A"])
                    history.probe(1215, "freshness", sampled, start=1211)
                    self.assertIs(history.verdict().status, expected)

    def test_after_proof_missing_required_result_fails(self):
        history = History()
        history.resume()
        history.receipt("rejected")
        history.probe(80, "final", [])
        self.assertIs(history.verdict().status, Status.FAIL)

    def test_evidence_cannot_include_raw_query_field(self):
        history = History()
        history.events[1]["question"] = "some private content"
        with self.assertRaisesRegex(InputError, "unsupported fields"):
            history.verdict()

    def test_http_fixture_binds_without_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("reverse DNS is unavailable")):
            with FixtureServer(("127.0.0.1", 0), Handler) as server:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)

    def test_fts5_preflight_names_missing_capability(self):
        import sqlite3

        with patch("sqlite3.connect", side_effect=sqlite3.Error):
            with self.assertRaisesRegex(InputError, "FTS5 is unavailable"):
                preflight()


if __name__ == "__main__":
    unittest.main()
