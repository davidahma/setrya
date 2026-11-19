"""Single-shard Elasticsearch fixture for native and refresh-only search evidence."""

import json
import os
from urllib.request import ProxyHandler, Request, build_opener

from setrya.adapter import Checkpoint, Publication, Receipt, SearchResult
from setrya.drill import InputError
from setrya.store_signals import elasticsearch_position, elasticsearch_refresh, passage_metadata


class Fixture:
    """A resumable indexing job kept in memory and a real disposable search index."""

    def __init__(self, context):
        self.run_id = context.run_id
        self.index = f"setrya-{self.run_id}"
        self.weak = context.drill.rules.visibility == "observed-after-refresh"
        self.broken = context.drill.options.get("mode") == "broken"
        self.port = int(os.environ["SETRYA_ELASTICSEARCH_PORT"])
        self.client = build_opener(ProxyHandler({}))
        self.created = False
        self.write = None

    def request(self, method, endpoint="", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(f"http://127.0.0.1:{self.port}/{self.index}{endpoint}", data=data,
                          method=method, headers={"Content-Type": "application/json"})
        with self.client.open(request, timeout=8) as response:
            return json.load(response)

    def seed(self):
        self.request("PUT", body={"settings": {"number_of_shards": 1, "number_of_replicas": 0,
                                                "refresh_interval": "-1"}})
        self.created = True
        self.uuid = self.request("GET", "/_settings")[self.index]["settings"]["index"]["uuid"]
        for doc, rev in (("refund", "A"), ("delivery", "1")):
            self.request("PUT", f"/_doc/{doc}", passage_metadata(doc, rev, "0"))
        self.request("POST", "/_refresh")

    def start_old(self):
        self.captured = self.request("GET", "/_doc/refund")["_source"]
        return "index-job"

    def checkpoint(self):
        return Checkpoint(self.run_id, "index-job", self.captured["document"], self.captured["revision"], "after_source_read")

    def publish(self):
        self.request("PUT", "/_doc/refund?refresh=true", passage_metadata("refund", "B", "0"))
        return Publication(self.run_id, "refund", "B")

    def release(self):
        # A merge-style worker records completion without replacing the passage.
        # Its broken counterpart writes the entire captured document back.
        patch = {**self.captured, "processed": True} if self.broken else {"processed": True}
        option = "" if self.weak else "?refresh=true"
        self.write = self.request("POST", "/_update/refund" + option, {"doc": patch})

    @property
    def refresh_generation(self):
        # Elasticsearch UUIDs are base64url and may start with "-" or "_".
        return "es-index-" + self.uuid

    def receipt(self):
        position = elasticsearch_position(self.write, index_uuid=self.uuid, shard=0)
        return Receipt(self.run_id, "index-job", "refund", self.captured["revision"], "committed",
                       self.refresh_generation if self.weak else position.generation, None if self.weak else position.commit)

    def refresh(self, receipt, probe):
        return elasticsearch_refresh(self.request("POST", "/_refresh"), receipt=receipt, generation=self.refresh_generation,
                                     route="support-search", scope="support-agent")

    def search(self, probe):
        if (probe.rules.route, probe.rules.scope) != ("support-search", "support-agent"):
            raise InputError("unknown fixture search route or caller")
        # Both synthetic records are public to this caller. Each result page can
        # witness the worker's document in its own single-shard search snapshot.
        topic = "delivery" if "delivery" in probe.question.lower() else "refund"
        response = self.request("POST", "/_search", {
            "query": {"bool": {"must": {"match_all": {}}, "should": {"term": {"document.keyword": topic}}}},
            "size": probe.rules.top_k, "seq_no_primary_term": True,
        })
        if response["timed_out"] or response["_shards"]["successful"] != 1 or response["_shards"]["failed"]:
            raise InputError("incomplete fixture search")
        hits = response["hits"]["hits"]
        witness = next((hit for hit in hits if hit["_id"] == "refund"), None)
        view = None
        if witness and not self.weak:
            view = elasticsearch_position(witness, index_uuid=self.uuid, shard=0).view("support-search", "support-agent")
        return SearchResult(tuple(hit["_source"].get("setrya_marker") for hit in hits), view=view,
                            route="support-search", scope="support-agent")

    def close(self):
        if self.created:
            self.request("DELETE")
