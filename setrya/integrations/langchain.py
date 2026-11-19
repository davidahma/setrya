"""Tag a revision's LangChain chunks and observe the documents returned to a caller."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from setrya.adapter import SearchResult
from setrya.drill import InputError, label
from setrya.store_signals import passage_metadata

if TYPE_CHECKING:
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
    from langchain_core.runnables import RunnableConfig


def tag_documents(
    chunks: Iterable[Document],
    *,
    document: str,
    revision: str,
    chunk_id: Callable[[Document, int], str] | None = None,
) -> list[Document]:
    """Return copies with Setrya metadata. Pass one immutable revision in split order.

    For incremental ingestion or batches within a revision, supply a stable unique
    chunk_id(chunk, ordinal). The default numbers chunks from zero in each call.
    """
    label(document, "document")
    label(revision, "revision")
    tagged = []
    seen = set()
    for ordinal, chunk in enumerate(chunks):
        key = str(ordinal) if chunk_id is None else chunk_id(chunk, ordinal)
        identity = passage_metadata(document, revision, key)
        if key in seen:
            raise InputError("each chunk in a revision needs a distinct ID")
        seen.add(key)
        if any(k in chunk.metadata and chunk.metadata[k] != v for k, v in identity.items()):
            raise InputError("existing passage metadata conflicts with the supplied chunk identity")
        tagged.append(chunk.model_copy(update={"metadata": {**chunk.metadata, **identity}}))
    return tagged


def _marker(chunk: Document) -> str | None:
    metadata = getattr(chunk, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    try:
        expected = passage_metadata(metadata["document"], metadata["revision"], metadata["chunk"])
    except (KeyError, InputError):
        return None
    marker = expected["setrya_marker"]
    return marker if metadata.get("setrya_marker") == marker else None


def observe_documents(documents: Iterable[Document], *, route: str, scope: str) -> SearchResult:
    """Capture ranks and verified chunk identities from the actual caller-filtered read.

    An untagged or inconsistent result stays at its original rank as an unknown
    passage. The retrieved text is never included in the Setrya observation.
    """
    return SearchResult(tuple(_marker(doc) for doc in documents),
                        route=label(route, "route"), scope=label(scope, "scope"))


def retrieve_with_evidence(
    retriever: BaseRetriever,
    query: str,
    *,
    route: str,
    scope: str,
    config: RunnableConfig | None = None,
    **kwargs,
) -> tuple[list[Document], SearchResult]:
    """Invoke the application's scoped retriever once. Return its documents and evidence.

    Use observe_documents after filtering if your application authorizes or reranks
    results downstream of this retriever.
    """
    documents = retriever.invoke(query, config=config, **kwargs)
    return documents, observe_documents(documents, route=route, scope=scope)
