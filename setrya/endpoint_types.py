"""Observations supplied by a disposable application to a two-user endpoint drill."""

from typing import NotRequired, Protocol, TypedDict

from .adapter import Checkpoint, Publication, Receipt


class EndpointRead(TypedDict):
    """Return observations from the serving path and the actual HTTP answer."""

    http_status: int
    route: str | None
    caller: str | None
    query_sha256: str | None
    answer: str | None


class EndpointResponse(EndpointRead):
    """Extend the served response with the cache and retrieval evidence for a race."""

    sources: list[str | None] | tuple[str | None, ...]
    cache_hit: bool | None
    cache_writer: str | None
    generation: NotRequired[str | None]
    commit: NotRequired[int | None]
    generation_skipped: NotRequired[bool | None]


class WorkerCapture(TypedDict):
    """Input recorded by the selected task before the access change."""

    run_id: str
    attempt: str
    caller: str
    revision: str
    question: str
    answer: str
    sources: list[str] | tuple[str, ...]


class EndpointReadApplication(Protocol):
    """Two already prepared synthetic sessions on the application's own endpoint."""

    def answer(self, caller: str, question: str) -> EndpointRead: ...


class EndpointApplication(Protocol):
    """Live hooks on the object yielded by the application's test fixture."""

    def seed(self) -> None: ...
    def answer(self, caller: str, question: str) -> EndpointResponse: ...
    def start_old(self) -> str: ...
    def checkpoint(self) -> Checkpoint | None: ...
    def captured(self) -> WorkerCapture | None: ...
    def revoke(self) -> Publication | None: ...
    def release(self) -> None: ...
    def receipt(self) -> Receipt | None: ...
