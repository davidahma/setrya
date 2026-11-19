# Record the sources supplied to one answer

An answer dependency record describes the generation inputs for a cached answer. It contains the answer ID, tenant, access scope, retrieval scope, their versions, and the ordered document revisions and chunks. The SDK uses the Python standard library and stores the record wherever your application writes it.

Suppose a retriever returns ten passages and a reranker selects three that fit the prompt. Pass those same three chunks, in order, to the record and the generator. The record describes what the generator received. It cannot determine which passages influenced the prose.

## Capture and save

```python
from setrya.dependencies import AnswerDependencies
from setrya.store_signals import passage_metadata

# Normally read this metadata back from each stored chunk in the final context.
context_metadata = [
    passage_metadata("refund-policy", "B", "section-2"),
    passage_metadata("returns-guide", "1", "section-4"),
]

record = AnswerDependencies.record(
    answer_id="answer-184",
    tenant="shop-7",
    access_scope="user-19",
    access_version="12",
    retrieval_scope="refund-policy",
    retrieval_version="8",
    chunks=context_metadata,
)
record.save("evidence/answer-184.json")
assert AnswerDependencies.load("evidence/answer-184.json") == record
```

For LangChain documents returned by `tag_documents`, pass `chunks=(doc.metadata for doc in final_context)`. For an existing cache, store `record.to_dict()` alongside the generated answer in its own transaction. Read it with `AnswerDependencies.from_dict(value)`. A standalone file save atomically replaces the named record. Use a distinct answer ID for each generation you want to distinguish.

## Choose the versions

- **Access scope** identifies the principal or authorization context used to obtain the passages. Its version changes when that context's relevant permissions change. Derive it from your application's authenticated state.
- **Retrieval scope** identifies the set of material searched, such as one tenant's refund-policy collection. Its version changes when a candidate for that retrieval could change, including a newly published policy that the original answer never cited. Teams must choose scopes that cover every source that could affect the query.
- **Chunk identity** uses the document ID, immutable document revision, and chunk ID from ingestion. `record.markers` returns their ordered `setrya_marker` values for use with other Setrya observations. An empty context can be recorded explicitly. Its retrieval version still matters when a new matching passage appears.

Read access, retrieval versions and source identities from a consistent snapshot. At cache publication or reuse, the application checks its current scope and source state before serving the answer. Replacing a prior JSON file or copying a dependency record by itself only records the claimed inputs.

## Local record format

Version 1 uses the format identifier `setrya.answer-dependencies`. The top-level keys are `format`, `version`, `answer_id`, `tenant`, `access_scope`, `access_version`, `retrieval_scope`, `retrieval_version`, and `chunks`. Each chunk contains exactly `document`, `revision`, `chunk`, and `setrya_marker`. Chunk order and repeated inputs survive a round trip.

The writer selects only those identity fields from source metadata. The reader recomputes every passage marker and rejects unsupported versions, unknown fields, duplicate JSON keys, and invalid input. Scope IDs and versions use Setrya's short ASCII identifiers. Choose opaque, shareable values for all IDs. Records omit answer text, prompts, chunk text, credentials and arbitrary metadata. A v1 reader accepts up to 1,024 chunks in a record and a local file of up to 1 MiB.

The answer dependency record and the worker drill report can be stored separately. The drill supplies the caller-visible observations needed for a release verdict. [Answer-cache drill](answer-cache.md) · [Passage metadata at ingestion](store-signals.md)
