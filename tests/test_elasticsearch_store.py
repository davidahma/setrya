"""Run on a disposable, loopback Elasticsearch node with SETRYA_ELASTICSEARCH_PORT."""

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.checker import Status, evaluate
from setrya.cli import EXAMPLE
from setrya.drill import Drill
from setrya.evidence import Report
from setrya.runner import run
from setrya.store_signals import passage_metadata


@unittest.skipUnless(os.environ.get("SETRYA_ELASTICSEARCH_PORT"), "requires disposable Elasticsearch")
class ElasticsearchIntegrationTests(unittest.TestCase):
    def test_committed_worker_through_real_search_and_offline_replay(self):
        source = (EXAMPLE / "drill.toml").read_text().replace(
            'setrya.examples.support_search.adapter:http_fixture', 'setrya.examples.elasticsearch_search:Fixture')
        for doc, revision in (("refund", "A"), ("refund", "B"), ("delivery", "1")):
            source = source.replace(f"{doc}@{revision}", passage_metadata(doc, revision, "0")["setrya_marker"])
        with TemporaryDirectory() as temp:
            config, evidence = Path(temp, "drill.toml"), Path(temp, "run.json")
            config.write_text(source)
            drill = Drill.load(config)
            for policy, tier in (("strict", "write-confirmed"), ("observed-after-refresh", "observed-after-refresh")):
                for mode in ("fixed", "broken"):
                    with self.subTest(policy=policy, mode=mode):
                        result = run(replace(drill, rules=replace(drill.rules, visibility=policy), options={"mode":mode}))
                        self.assertIs(result.verdict.status, Status.PASS if mode == "fixed" else Status.FAIL,
                                      result.verdict.detail)
                        if mode == "fixed":
                            self.assertEqual(result.verdict.evidence_tier, tier)
                        result.report.save(evidence)
                        self.assertEqual(evaluate(Report.load(evidence)), result.verdict)
