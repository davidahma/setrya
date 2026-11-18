"""Public adapter contract for disposable search environments."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .drill import Drill, Probe


@dataclass(frozen=True)
class RunContext:
    run_id: str
    directory: Path
    drill: Drill


@dataclass(frozen=True)
class Checkpoint:
    run_id: str
    attempt: str
    document: str
    revision: str
    name: str


@dataclass(frozen=True)
class Publication:
    run_id: str
    document: str
    revision: str


@dataclass(frozen=True)
class Receipt:
    run_id: str
    attempt: str
    document: str
    revision: str
    outcome: str
    generation: str | None = None
    commit: int | None = None


@dataclass(frozen=True)
class ReadView:
    generation: str
    commit: int
    route: str
    scope: str


@dataclass(frozen=True)
class SearchResult:
    # Ordered, top-k passage markers. None represents an unidentified result.
    passages: tuple[str | None, ...]
    view: ReadView | None = None
    origins: tuple[str | None, ...] | None = None
    # The actual retrieval route and caller, even when no watermark is available.
    route: str | None = None
    scope: str | None = None


class Adapter(Protocol):
    """All methods return promptly. Pollable observations return None while unavailable."""

    def seed(self) -> None: ...
    def start_old(self) -> str: ...
    def checkpoint(self) -> Checkpoint | None: ...
    def publish(self) -> Publication | None: ...
    def search(self, probe: Probe) -> SearchResult: ...
    def release(self) -> None: ...
    def receipt(self) -> Receipt | None: ...
    def close(self) -> None: ...
