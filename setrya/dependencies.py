"""Portable, text-free dependencies captured from the inputs to one answer generation."""
from collections.abc import Iterable, Mapping, Set
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .drill import InputError, label, mapping
from .offboarding.inputs import InvalidReport, read_json, save
from .store_signals import passage_metadata


FORMAT = "setrya.answer-dependencies"
VERSION = 1
MAX_CHUNKS = 1024
_FIELDS = ("answer_id", "tenant", "access_scope", "access_version", "retrieval_scope", "retrieval_version")
_CHUNK_FIELDS = {"document", "revision", "chunk", "setrya_marker"}


@dataclass(frozen=True, slots=True)
class ChunkDependency:
    document: str
    revision: str
    chunk: str
    setrya_marker: str = field(init=False)

    def __post_init__(self):
        tag = passage_metadata(self.document, self.revision, self.chunk)
        object.__setattr__(self, "setrya_marker", tag["setrya_marker"])

    @classmethod
    def from_metadata(cls, metadata: Mapping) -> "ChunkDependency":
        """Select just the four identity fields from the application's stored metadata."""
        if not isinstance(metadata, Mapping) or not _CHUNK_FIELDS <= metadata.keys():
            raise InputError("each generation input needs complete passage metadata")
        ref = cls(metadata["document"], metadata["revision"], metadata["chunk"])
        if metadata["setrya_marker"] != ref.setrya_marker:
            raise InputError("the passage marker disagrees with the document, revision, or chunk")
        return ref


@dataclass(frozen=True, slots=True)
class AnswerDependencies:
    """Source order and application-supplied scope versions for one generated answer."""

    answer_id: str
    tenant: str
    access_scope: str
    access_version: str
    retrieval_scope: str
    retrieval_version: str
    chunks: tuple[ChunkDependency, ...]

    def __post_init__(self):
        for name in _FIELDS:
            label(getattr(self, name), name)
        if (not isinstance(self.chunks, tuple) or len(self.chunks) > MAX_CHUNKS
                or not all(isinstance(ref, ChunkDependency) for ref in self.chunks)):
            raise InputError(f"chunks must be an ordered tuple of up to {MAX_CHUNKS} chunk references")

    @classmethod
    def record(cls, *, answer_id: str, tenant: str, access_scope: str, access_version: str,
               retrieval_scope: str, retrieval_version: str,
               chunks: Iterable[Mapping]) -> "AnswerDependencies":
        """Capture the metadata of the ordered chunks actually passed to generation."""
        if not isinstance(chunks, Iterable) or isinstance(chunks, (str, bytes, Mapping, Set)):
            raise InputError("supply the generation inputs as an ordered iterable of passage metadata")
        refs = []
        for metadata in chunks:
            if len(refs) == MAX_CHUNKS:
                raise InputError(f"an answer record supports at most {MAX_CHUNKS} generation inputs")
            refs.append(ChunkDependency.from_metadata(metadata))
        return cls(answer_id, tenant, access_scope, access_version,
                   retrieval_scope, retrieval_version, tuple(refs))

    @property
    def markers(self) -> tuple[str, ...]:
        return tuple(ref.setrya_marker for ref in self.chunks)

    def to_dict(self) -> dict:
        return {"format": FORMAT, "version": VERSION, **{k: getattr(self, k) for k in _FIELDS},
                "chunks": [asdict(ref) for ref in self.chunks]}

    @classmethod
    def from_dict(cls, data: object) -> "AnswerDependencies":
        row = mapping(data, "answer dependency record", {"format", "version", "chunks", *_FIELDS})
        if row["format"] != FORMAT or type(row["version"]) is not int or row["version"] != VERSION:
            raise InputError("unsupported answer dependency record format or version")
        if not isinstance(row["chunks"], list) or len(row["chunks"]) > MAX_CHUNKS:
            raise InputError(f"chunks must be a list of up to {MAX_CHUNKS} generation inputs")
        return cls.record(**{k: row[k] for k in _FIELDS},
                          chunks=(mapping(ref, "chunk dependency", _CHUNK_FIELDS) for ref in row["chunks"]))

    def save(self, path: str | Path) -> None:
        """Atomically publish a record at a local path."""
        try:
            save(self.to_dict(), Path(path))
        except (InvalidReport, OSError) as error:
            raise InputError("could not write the answer dependency record") from error

    @classmethod
    def load(cls, path: str | Path) -> "AnswerDependencies":
        try:
            data, _ = read_json(Path(path), "answer dependency record")
        except InvalidReport as error:
            raise InputError(str(error)) from None
        return cls.from_dict(data)
