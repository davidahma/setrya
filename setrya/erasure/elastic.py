"""Direct subject-level erasure in one concrete Elasticsearch index."""

from __future__ import annotations

import re

from setrya.drill import InputError

from .journal import identifier
from .subject import Subject


def _complete(response, *, refresh=False):
    if response.get("timed_out") or response.get("terminated_early"):
        raise ValueError("Elasticsearch did not complete the request")
    shards = response.get("_shards")
    if shards and (shards["failed"] or (not refresh and shards["successful"] < shards["total"] - shards.get("skipped", 0))):
        raise ValueError("Elasticsearch returned incomplete shard results")


class ElasticsearchAdapter:
    proof = "direct"

    def __init__(self, client, index: str, *, subject_field="setrya_subject"):
        if not isinstance(index, str) or not re.fullmatch("[a-z0-9][a-z0-9_.-]{0,254}", index):
            raise InputError("choose a single concrete Elasticsearch index")
        if not isinstance(subject_field, str) or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", subject_field):
            raise InputError("choose a mapped keyword field for the subject reference")
        self.client, self.index, self.field = client, index, subject_field

    def _query(self, subject):
        if not isinstance(subject, Subject):
            raise InputError("expected a tenant-scoped Subject")
        mapping = self.client.indices.get_mapping(index=self.index)
        if set(mapping) != {self.index}:
            raise ValueError("Elasticsearch aliases and data streams cannot identify a concrete index")
        node = mapping[self.index]["mappings"]
        for part in self.field.split("."):
            if node.get("type") == "nested":
                raise ValueError("the subject keyword must be outside nested documents")
            node = node.get("properties", {}).get(part) or node.get("fields", {}).get(part) or {}
        if (node.get("type") != "keyword" or node.get("index", True) is not True
                or node.get("normalizer") or node.get("ignore_above", 64) < 64 or node.get("script")
                or node.get("null_value") is not None):
            raise ValueError("expected an indexed, exact keyword mapping for 64-character subject references")
        return {"term": {self.field: subject.ref}}

    def _copies(self, subject):
        query = self._query(subject)
        _complete(self.client.indices.refresh(index=self.index), refresh=True)
        opened = self.client.open_point_in_time(index=self.index, keep_alive="1m", allow_partial_search_results=False)
        pit_id = opened["id"]
        result = {}
        cursor = {}
        try:
            _complete(opened)
            while True:
                page = self.client.search(
                    pit={"id": pit_id, "keep_alive": "1m"}, query=query, sort=["_shard_doc"], size=128,
                    source=False, docvalue_fields=[self.field], stored_fields=["_routing"],
                    seq_no_primary_term=True, allow_partial_search_results=False, **cursor,
                )
                pit_id = page.get("pit_id", pit_id)
                _complete(page)
                hits = page["hits"]["hits"]
                for hit in hits:
                    key = identifier(hit["_id"], "Elasticsearch copy key")
                    if key in result:
                        raise ValueError("multiple Elasticsearch hits have the same logical copy key")
                    fields = hit["fields"]
                    if fields.get(self.field) != [subject.ref]:
                        raise ValueError("shared or unverifiable Elasticsearch document ownership")
                    action = {"_index": self.index, "_id": key, "if_seq_no": hit["_seq_no"],
                              "if_primary_term": hit["_primary_term"]}
                    if "_routing" in hit:
                        action["routing"] = hit["_routing"]
                    result[key] = {"delete": action}
                if len(hits) < 128:
                    return result
                cursor = {"search_after": hits[-1]["sort"]}
        finally:
            self.client.close_point_in_time(id=pit_id)

    def find(self, subject):
        return set(self._copies(subject))

    def delete(self, subject):
        actions = list(self._copies(subject).values())
        for start in range(0, len(actions), 128):
            result = self.client.bulk(operations=actions[start:start + 128], refresh=True)
            if result["errors"]:
                raise ValueError("Elasticsearch copy changed or could not be deleted")

    def verify_absent(self, subject):
        return self.find(subject)
