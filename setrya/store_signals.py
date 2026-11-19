"""Normalize native store observations without promoting them to global watermarks."""

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from .adapter import ReadView, Refresh, Receipt
from .drill import InputError, integer, label


@dataclass(frozen=True)
class Position:
    generation: str
    commit: int

    def view(self, route: str, scope: str) -> ReadView:
        return ReadView(self.generation, self.commit, route, scope)


def postgres_wal(lsn: str, *, generation: str) -> Position:
    """Convert a PostgreSQL X/Y position within one cluster and timeline."""
    if not isinstance(lsn, str) or not re.fullmatch(r"[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}", lsn):
        raise InputError("expected a PostgreSQL WAL position")
    high, low = lsn.split("/")
    return Position(label(generation, "cluster timeline"), int(high, 16) << 32 | int(low, 16))


def elasticsearch_position(response, *, index_uuid: str, shard: int) -> Position:
    """Normalize a write response or an actual search hit for one concrete document."""
    if not isinstance(response, dict):
        raise InputError("expected an Elasticsearch response")
    key = [index_uuid, response.get("_index"), response.get("_id")]
    if not all(isinstance(part, str) and part for part in key):
        raise InputError("expected a concrete Elasticsearch index and document identity")
    key += [integer(shard, "shard", 0, 2**31 - 1),
            integer(response.get("_primary_term"), "primary term", 1, 2**63 - 1)]
    generation = "es-" + sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
    return Position(generation, integer(response.get("_seq_no"), "sequence number", 0, 2**63 - 1))


def elasticsearch_refresh(response, *, receipt: Receipt, generation: str, route: str, scope: str) -> Refresh:
    """Accept a synchronous refresh response for the exact concrete index requested."""
    shards = response.get("_shards") if isinstance(response, dict) else None
    if (receipt.outcome != "committed" or receipt.generation != generation or not isinstance(shards, dict)
            or any(type(shards.get(k)) is not int for k in ("total", "successful", "failed"))
            or shards["total"] < 1 or shards["successful"] != shards["total"] or shards["failed"] != 0):
        raise InputError("refresh did not acknowledge all shards of the committed index")
    return Refresh(receipt.run_id, receipt.attempt, generation, route, scope)


def passage_metadata(document: str, revision: str, chunk: str) -> dict[str, str]:
    """Attach a deterministic identity to a chunk at ingestion time."""
    identity = [label(v, k) for v, k in ((document, "document"), (revision, "revision"), (chunk, "chunk"))]
    marker = "chunk-" + sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return {"document": document, "revision": revision, "chunk": chunk, "setrya_marker": marker}
