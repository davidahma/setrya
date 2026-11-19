"""Run on a disposable, loopback Elasticsearch node with SETRYA_ELASTICSEARCH_PORT."""

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.conformance import assert_store_contract
from setrya.cli import EXAMPLE
from setrya.drill import Drill
from setrya.store_signals import passage_metadata


@unittest.skipUnless(os.environ.get("SETRYA_ELASTICSEARCH_PORT"), "requires disposable Elasticsearch")
class ElasticsearchIntegrationTests(unittest.TestCase):
    def test_committed_worker_through_real_search_and_offline_replay(self):
        source = (EXAMPLE / "drill.toml").read_text().replace(
            'setrya.examples.support_search.adapter:http_fixture', 'setrya.examples.elasticsearch_search:Fixture')
        for doc, revision in (("refund", "A"), ("refund", "B"), ("delivery", "1")):
            source = source.replace(f"{doc}@{revision}", passage_metadata(doc, revision, "0")["setrya_marker"])
        with TemporaryDirectory() as temp:
            config = Path(temp, "drill.toml")
            config.write_text(source)
            drill = Drill.load(config)
            for policy, tier in (("strict", "write-confirmed"), ("observed-after-refresh", "observed-after-refresh")):
                with self.subTest(policy=policy):
                    configured = replace(drill, rules=replace(drill.rules, visibility=policy))
                    assert_store_contract(
                        broken=replace(configured, options={"mode": "broken"}),
                        guarded=replace(configured, options={"mode": "fixed"}),
                        opaque=replace(configured, options={"mode": "opaque"}),
                        expected_tier=tier,
                    )
