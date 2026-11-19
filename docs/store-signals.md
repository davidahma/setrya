# Connect store evidence to a search drill

An adapter reports two things separately. The passage markers identify the versions the caller actually retrieved. A terminal receipt identifies what happened to the held write. A strict PASS after a commit also needs evidence that the read path had reached that write.

## Tag chunks at ingestion

Create the marker before dispatching an indexing job, when the authoritative source revision is available. Write the metadata alongside the vector, text and tenant filter. An old job must carry the revision it actually read.

```python
from setrya.store_signals import passage_metadata

metadata = passage_metadata("refund", "B", "returns-window")
index.upsert(vectors=[{"id": chunk_id, "values": embedding, "metadata": metadata}])
```

The deterministic `setrya_marker` identifies the document, revision and chunk together. Generate the expected markers from the known test inputs and put them in the drill's `before`, `require` and `forbid` lists. Map the metadata from each ranked hit into the same position in `SearchResult.passages`. Keep missing markers as `None`. Dropping an unidentified result changes the ranks and can hide a stale chunk.

For Pinecone, request metadata with the search. For a LangChain retriever, preserve the field through splitting and read it from each returned `Document.metadata`. A record ID alone cannot tell two revisions apart when ingestion overwrites that record. Use an opaque synthetic document identifier for public fixtures.

## PostgreSQL and pgvector

The bundled worker commits its index mutation, then records PostgreSQL's native `pg_current_wal_insert_lsn()` in its job receipt. The search handler reads the job receipt and the ranked hits in one read-only repeatable-read snapshot. A reader seeing that job row sees the already-committed index transaction in the same database. The separate stale-view fixture omits the watermark. [Runnable pgvector drill](pgvector.md) · [PostgreSQL WAL functions](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADMIN-BACKUP)

`postgres_wal(lsn, generation=...)` converts PostgreSQL's hexadecimal `X/Y` notation to a comparable position. Bind the generation to one cluster and timeline. The disposable fixture uses its unique run schema on a single primary. On a standby, use evidence of replay on that standby before opening a fresh read snapshot. A WAL position sampled independently of the snapshot does not establish what a cache, materialized view or search replica served.

## Elasticsearch

Elasticsearch returns `_seq_no` and `_primary_term` on writes. Request `seq_no_primary_term=true` on the **search** to receive the corresponding fields on individual hits. The helper scopes the token to the concrete index name and UUID, shard, document ID and primary term. The same sequence number in another shard has no bearing on this write. [Elasticsearch concurrency tokens](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/optimistic-concurrency-control)

```python
from setrya.store_signals import elasticsearch_position

write = elasticsearch_position(write_response.body, index_uuid=index_uuid, shard=shard_id)
hit = elasticsearch_position(search_hit, index_uuid=index_uuid, shard=shard_id)
# Bind write.generation and write.commit to the old attempt's committed Receipt.
# Attach this view only to the read that actually returned the witness.
view = hit.view(route="support-search", scope="support-agent")
```

A hit for the updated document can witness that document on its shard. Do not transfer that view to a different query, caller or multi-shard snapshot. A deleted document has no hit to carry its token. Use a scoped refresh and the weaker observation contract when the read cannot establish a write watermark.

## Observe after a refresh

Set `visibility = "observed-after-refresh"` under `[rules]` to allow the weaker tier for this drill. The default is `"strict"`. After observing a terminal committed receipt, Setrya calls an adapter's optional `refresh(receipt, probe)` once per distinct route and caller. A successful acknowledgment refers to the exact run, attempt, index generation, route and caller. The recorded search must begin afterward and all final probes must still meet the drill's assertions. With the Recorder, call `rec.refreshed(ack)` after `rec.receipt(...)` and before `rec.search(...)`.

```python
from setrya.store_signals import elasticsearch_refresh

def refresh(self, receipt, probe):
    response = self.client.indices.refresh(index=self.concrete_index)
    return elasticsearch_refresh(
        response.body, receipt=receipt, generation="es-index-" + self.index_uuid,
        route=self.search_route, scope=self.caller,
    )
```

Use the same `"es-index-" + index_uuid` generation in the committed receipt and refresh acknowledgment. The prefix accommodates Elasticsearch UUIDs that start with a hyphen or underscore. The receipt can leave `commit=None`. Resolve the index and caller from the application route. Only acknowledge a successful refresh of the concrete generation the application actually reads. The helper checks that all shards succeeded. Time elapsed, an HTTP 200 from a proxy, or a completed queue job does not establish a refresh. A store without a refresh acknowledgment or matching watermark yields NOT TESTED after a commit. [Elasticsearch refresh semantics](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-refresh)

A passing refresh-only run prints **Observed after refresh (write visibility unverified)** and exposes `verdict.evidence_tier == "observed-after-refresh"`. This answers whether the caller returned the expected passages after the refresh completed. Strict confirmation exposes `"write-confirmed"`. A stale hit still fails either policy. A read supplying a contradictory watermark cannot substitute the refresh acknowledgment.

## Run the Elasticsearch integration check

The [Elasticsearch fixture](../setrya/examples/elasticsearch_search.py) uses a new one-shard index per run with automatic refresh disabled. The held job captures revision A, Setrya publishes B, and release either writes the stale document back or merges a completion field into the current document. Each query ranks the topic and also returns the control. The strict adapter reads the refund hit's token from that exact response. The refresh-only adapter omits tokens and calls the real refresh endpoint. Each run saves a report and replays its verdict after deleting the index.

To repeat CI on a disposable loopback node, set `SETRYA_ELASTICSEARCH_PORT` to its HTTP port and run `python -m unittest discover -s tests -p test_elasticsearch_store.py -v`. The [CI job](../.github/workflows/ci.yml) starts the pinned Elasticsearch build and exercises both implementations under each evidence policy.
