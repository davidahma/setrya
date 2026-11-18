"""Drive a trusted adapter and record observations in runner clock order."""

from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic_ns, sleep
from uuid import uuid4

from .adapter import RunContext
from .checker import NS_PER_MS, Status, Verdict, evaluate
from .drill import Drill, InputError, Probe, label
from .evidence import Report, establishes_baseline, matches_search, validate_observation


@dataclass(frozen=True)
class RunResult:
    verdict: Verdict
    report: Report


def run(drill: Drill | str | Path) -> RunResult:
    """Run a drill against a reviewed adapter and return its verdict and evidence."""
    if not isinstance(drill, Drill):
        drill = Drill.load(drill)
    drill.validate()
    module, factory_name = drill.factory.split(":")
    try:
        factory = getattr(import_module(module), factory_name)
    except (ImportError, AttributeError):
        raise InputError("could not import adapter factory from the checkout or Python path") from None

    execution = _Execution(drill, uuid4().hex)
    with TemporaryDirectory(prefix="setrya-") as directory:
        try:
            execution.adapter = factory(RunContext(execution.run_id, Path(directory), drill))
        except Exception as error:
            execution.add_error("factory", error)
        else:
            try:
                execution.run()
            except Exception as error:
                execution.add_error(execution.operation, error)
            finally:
                try:
                    execution.adapter.close()
                except Exception as error:
                    execution.add_error("close", error)
    return execution.result()


class _Execution:
    def __init__(self, drill: Drill, run_id: str):
        self.drill = drill
        self.run_id = run_id
        self.rules = drill.rules
        self.probes = {probe.id: probe for probe in self.rules.probes}
        self.started = monotonic_ns()
        self.events = []

    def time(self) -> int:
        return monotonic_ns() - self.started

    def add(self, kind: str, **fields) -> dict:
        event = {"kind": kind, "seq": len(self.events) + 1, "ns": self.time(), **fields}
        validate_observation(event, self.probes)
        self.events.append(event)
        return event

    def add_error(self, operation: str, error: Exception) -> None:
        try:
            error_type = label(type(error).__name__, "adapter error type")
        except InputError:
            error_type = "AdapterError"
        self.add("adapter_error", operation=operation, error=error_type)

    def call(self, operation: str, *args):
        self.operation = operation
        return getattr(self.adapter, operation)(*args)

    def result(self) -> RunResult:
        report = Report(self.run_id, self.rules, tuple(self.events))
        return RunResult(evaluate(report), report)

    def probe(self, probe: Probe, stage: str) -> dict:
        started = self.time()
        observed = self.call("search", probe)
        view = asdict(observed.view) if observed.view is not None else None
        # A read view can independently supply route and caller identity.
        route = observed.route if observed.route is not None else view["route"] if view else None
        scope = observed.scope if observed.scope is not None else view["scope"] if view else None
        passages = tuple(observed.passages) if isinstance(observed.passages, (list, tuple)) else observed.passages
        origins = (None,) * len(passages) if observed.origins is None else observed.origins
        if isinstance(origins, list):
            origins = tuple(origins)
        return self.add("probe", probe_id=probe.rules.id, stage=stage, started_ns=started,
                        passages=passages, origins=origins, view=view, route=route, scope=scope)

    def wait_until(self, timestamp: int) -> None:
        sleep(max(0, (timestamp - self.time()) / 1_000_000_000))

    def run(self) -> None:
        freshness = next(probe for probe in self.drill.probes if probe.rules.id == self.rules.freshness_probe)
        self.call("seed")
        self.add("seeded")
        for probe in self.drill.probes:
            if probe.rules.before:
                event = self.probe(probe, "baseline")
                if probe == freshness and not establishes_baseline(event, probe.rules, freshness=True):
                    return

        attempt = self.call("start_old")
        start = self.add("started", attempt=attempt)["ns"]
        deadline = start + self.rules.checkpoint_ms * NS_PER_MS
        interval = self.rules.sample_ms * NS_PER_MS
        while self.time() <= deadline:
            held = self.call("checkpoint")
            received = self.time()
            if held is not None and received <= deadline:
                self.add("held", **asdict(held))
                break
            if received >= deadline:
                return
            self.wait_until(min(received + interval, deadline))
        else:
            return
        expected_hold = (self.run_id, attempt, self.rules.document, self.rules.old_revision, self.rules.checkpoint)
        if (held.run_id, held.attempt, held.document, held.revision, held.name) != expected_hold:
            return

        publication = self.call("publish")
        published = self.add("published", publication=asdict(publication) if publication is not None else None)
        if publication is None or (publication.run_id, publication.document, publication.revision) != (
            self.run_id, self.rules.document, self.rules.new_revision
        ):
            return

        cutoff = published["ns"] + self.rules.freshness_ms * NS_PER_MS
        while True:
            event = self.probe(freshness, "freshness")
            if matches_search(event, freshness.rules) and set(freshness.rules.require) <= set(event["passages"]):
                if event["ns"] > cutoff:
                    return
                break
            if event["started_ns"] >= cutoff:
                return
            self.wait_until(min(event["started_ns"] + interval, cutoff))

        # A second probe can expose a stale read while the worker is still held.
        for probe in self.drill.probes:
            self.probe(probe, "freshness")
        self.call("release")
        release = self.add("released", attempt=attempt)["ns"]
        final_cutoff = release + self.rules.settle_ms * NS_PER_MS
        receipt = None
        while self.time() <= final_cutoff:
            sample_started = self.time()
            if receipt is None:
                receipt = self.call("receipt")
                if receipt is not None:
                    self.add("receipt", **asdict(receipt))
            for probe in self.drill.probes:
                self.probe(probe, "final")
            if receipt is not None and self.result().verdict.status in {Status.PASS, Status.FAIL}:
                return
            if self.time() >= final_cutoff:
                break
            self.wait_until(min(sample_started + interval, final_cutoff))
