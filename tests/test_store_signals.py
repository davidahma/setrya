from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.adapter import Receipt
from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE
from setrya.drill import Drill, InputError
from setrya.evidence import Report, bind_inputs
from setrya.store_signals import elasticsearch_position, elasticsearch_refresh, passage_metadata, postgres_wal
from test_checker import History, view
from test_runtime_evidence import make_report


class VisibilityTests(unittest.TestCase):
    def history(self, *, policy="observed-after-refresh", refresh=True, late=False, watermark=None):
        history = History()
        history.rules = replace(history.rules, visibility=policy)
        history.resume()
        history.receipt(commit=None)
        acknowledgment = {"attempt": "attempt", "run_id": "run", "generation": "generation",
                          "route": "support-search", "scope": "support-agent"}
        if refresh and not late:
            history.add("refreshed", 65, **acknowledgment)
        history.probe(70, "final", ["refund@B"], view=watermark)
        history.probe(72, "final", ["delivery@1"], "delivery", view=watermark)
        if refresh and late:
            history.add("refreshed", 74, **acknowledgment)
        return history

    def test_refresh_is_opt_in_scoped_and_precedes_search(self):
        history = self.history()
        with TemporaryDirectory() as root:
            path = Path(root, "report.json")
            make_report(history).save(path)
            payload = json.loads(path.read_bytes())
            self.assertEqual((payload["format"], payload["schema"], payload["checker"]), ("setrya.search", 1, 1))
            verdict = evaluate(Report.load(path))
            self.assertIs(verdict.status, Status.PASS)
            self.assertEqual(verdict.evidence_tier, "observed-after-refresh")
        for changes in ({"refresh": False}, {"policy": "strict"}, {"late": True},
                        {"watermark": view(generation="wrong")}):
            with self.subTest(changes=changes):
                self.assertIs(evaluate(make_report(self.history(**changes))).status, Status.NOT_TESTED)
        for field in ("attempt", "generation", "scope"):
            with self.subTest(field=field), self.assertRaises(InputError):
                changed = deepcopy(history)
                next(e for e in changed.events if e["kind"] == "refreshed")[field] = "other"
                evaluate(make_report(changed))

    def test_duplicate_singletons_rejected_before_any_verdict(self):
        for kind in ("held", "receipt", "published", "started", "released", "seeded"):
            history = self.history()
            index = next(i for i, e in enumerate(history.events) if e["kind"] == kind)
            history.events.insert(index + 1, deepcopy(history.events[index]))
            for n, event in enumerate(history.events, 1):
                event["seq"] = n
            with self.subTest(kind=kind), self.assertRaisesRegex(InputError, "duplicate"):
                evaluate(make_report(history))

    def test_bundled_adapter_rename_preserves_comparison_fingerprints(self):
        current = Drill.load(EXAMPLE / "drill.toml")
        historical = replace(current, factory=current.factory.removeprefix("setrya."))
        self.assertEqual(bind_inputs(current), bind_inputs(historical))

    def test_store_positions_and_markers_never_alias_across_identities(self):
        self.assertEqual(postgres_wal("1/0000000A", generation="cluster-1").commit, 2**32 + 10)
        for invalid in ("1", "0/G", "0/000000000", -1):
            with self.assertRaises(InputError):
                postgres_wal(invalid, generation="cluster-1")
        response = {"_id": "doc", "_index": "concrete", "_seq_no": 5, "_primary_term": 1}
        normal = elasticsearch_position(response, index_uuid="uuid", shard=0)
        for terms in ({"_id": "other"}, {"_index": "alias"}, {"_primary_term": 2}):
            self.assertNotEqual(normal.generation, elasticsearch_position(response | terms, index_uuid="uuid", shard=0).generation)
        for uuid, shard in (("uuid2", 0), ("uuid", 1)):
            self.assertNotEqual(normal.generation, elasticsearch_position(response, index_uuid=uuid, shard=shard).generation)
        with self.assertRaises(InputError):
            elasticsearch_position(response | {"_seq_no": True}, index_uuid="uuid", shard=0)
        receipt = Receipt("run", "attempt", "doc", "A", "committed", "index-uuid")
        with self.assertRaises(InputError):
            elasticsearch_refresh({"_shards": {"total": 2, "successful": 1, "failed": 1}}, receipt=receipt,
                                  generation="index-uuid", route="search", scope="support")
        marker = passage_metadata("doc", "A", "chunk-1")
        self.assertEqual(marker, passage_metadata("doc", "A", "chunk-1"))
        self.assertNotEqual(marker["setrya_marker"], passage_metadata("doc", "B", "chunk-1")["setrya_marker"])
