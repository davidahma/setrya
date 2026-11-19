"""Capture and evaluate search evidence inside an existing test."""

from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic_ns
from typing import Callable
from uuid import uuid4

from .adapter import Checkpoint, Publication, Receipt, SearchResult
from .checker import Verdict, evaluate
from .drill import Drill, InputError, Probe, label
from .evidence import SCHEMA, Report, _EventSequence, _STAGES, bind_inputs


@dataclass(frozen=True)
class RunResult:
    verdict: Verdict
    report: Report


class _Journal:
    def __init__(self, drill: Drill, run_id: str, *, test_id: str | None = None, revision: str | None = None):
        self.drill = drill
        self.run_id = run_id
        self.rules = drill.rules
        self.inputs = bind_inputs(drill, test_id=test_id, revision=revision)
        self.probes = {probe.rules.id: probe for probe in drill.probes}
        self.started_ns = monotonic_ns()
        self.events = []
        self._sequence = _EventSequence({probe.id: probe for probe in self.rules.probes}, SCHEMA)

    def time(self) -> int:
        return monotonic_ns() - self.started_ns

    def add(self, kind: str, **fields) -> dict:
        event = {"kind": kind, "seq": len(self.events) + 1, "ns": self.time(), **fields}
        self._sequence.accept(event)
        self.events.append(event)
        return event

    def add_error(self, operation: str, error: BaseException) -> None:
        try:
            error_type = label(type(error).__name__, "error type")
        except InputError:
            error_type = "ExecutionError"
        self.add("adapter_error", operation=operation, error=error_type)

    def result(self) -> RunResult:
        report = Report(self.run_id, self.rules, tuple(self.events), schema=SCHEMA, inputs=self.inputs)
        return RunResult(evaluate(report), report)

    def observe(self, probe: Probe, search: Callable[[Probe], SearchResult]) -> tuple[SearchResult, dict]:
        stage = _STAGES.get(self._sequence.stage)
        if stage is None or self._sequence.error:
            raise InputError("record search after seeding, publication, or release")
        started = self.time()
        observed = search(probe)
        view = asdict(observed.view) if observed.view is not None else None
        route = observed.route if observed.route is not None else view["route"] if view else None
        scope = observed.scope if observed.scope is not None else view["scope"] if view else None
        passages = tuple(observed.passages) if isinstance(observed.passages, (list, tuple)) else observed.passages
        origins = (None,) * len(passages) if observed.origins is None else observed.origins
        if isinstance(origins, list):
            origins = tuple(origins)
        event = self.add("probe", probe_id=probe.rules.id, stage=stage, started_ns=started, passages=passages,
                         origins=origins, view=view, route=route, scope=scope)
        return observed, event


class Recorder:
    """Record one serial observation sequence while the test controls the app."""

    def __init__(self, drill: Drill | str | Path, *, test_id: str, report: str | Path,
                 run_id: str | None = None, revision: str | None = None):
        if not isinstance(drill, Drill):
            if Path(drill).resolve() == Path(report).resolve():
                raise InputError("drill and recording report must use distinct files")
            drill = Drill.load(drill)
        drill.validate()
        self.run_id = uuid4().hex if run_id is None else label(run_id, "run ID")
        self._journal = _Journal(drill, self.run_id, test_id=label(test_id, "test ID"), revision=revision)
        self._path = Path(report)
        self._active = False
        self._result = None

    def __enter__(self) -> "Recorder":
        if self._active or self._result is not None:
            raise InputError("a recorder can only be used for one test run")
        self._active = True
        return self

    def __exit__(self, exc_type, error, traceback) -> None:
        self._active = False
        if error is not None:
            self._journal.add_error("test", error)
        self._result = self._journal.result()
        try:
            self.result.report.save(self._path)
        except InputError:
            if error is None:
                raise
            error.add_note("Setrya could not save the report.")

    @property
    def result(self) -> RunResult:
        if self._result is None:
            raise InputError("the recording is available after the test leaves the context")
        return self._result

    def _record(self, kind: str, **fields) -> None:
        if not self._active:
            raise InputError("record observations inside the Recorder context")
        self._journal.add(kind, **fields)

    def seeded(self) -> None:
        self._record("seeded")

    def started(self, attempt: str) -> None:
        self._record("started", attempt=attempt)

    def held(self, checkpoint: Checkpoint) -> None:
        self._record("held", **asdict(checkpoint))

    def published(self, publication: Publication | None) -> None:
        self._record("published", publication=asdict(publication) if publication is not None else None)

    def released(self, attempt: str) -> None:
        self._record("released", attempt=attempt)

    def receipt(self, receipt: Receipt) -> None:
        self._record("receipt", **asdict(receipt))

    def search(self, probe_id: str, search: Callable[[Probe], SearchResult]) -> SearchResult:
        """Measure a call with its configured question, route, caller and depth."""
        if not self._active:
            raise InputError("record observations inside the Recorder context")
        probe = self._journal.probes.get(probe_id)
        if probe is None:
            raise InputError("unknown recording probe")
        observed, _ = self._journal.observe(probe, search)
        return observed
