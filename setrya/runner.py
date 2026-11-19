"""Drive a trusted adapter and record observations in runner clock order."""

from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from uuid import uuid4

from .adapter import Checkpoint, RunContext
from .checker import NS_PER_MS, Status
from .drill import Drill, InputError, Probe
from .evidence import establishes_baseline, matches_search, observed_change
from .recorder import RunResult, _Journal


def run(drill: Drill | str | Path, *, revision: str | None = None) -> RunResult:
    """Run a drill against a reviewed adapter and return its verdict and evidence."""
    if not isinstance(drill, Drill):
        drill = Drill.load(drill)
    drill.validate()
    if drill.factory is None:
        raise InputError("check requires an adapter. Use Recorder for a test-owned drill.")
    execution = _Execution(drill, uuid4().hex, revision=revision)
    module, factory_name = drill.factory.split(":")
    # Drills saved before the bundled fixtures moved into the Setrya namespace.
    if module in {"examples.support_search.adapter", "examples.pgvector_search.adapter"}:
        module = "setrya." + module
    try:
        factory = getattr(import_module(module), factory_name)
    except (ImportError, AttributeError):
        raise InputError("could not import adapter factory from the checkout or Python path") from None
    except (Exception, KeyboardInterrupt) as error:
        execution.add_error("factory", error)
        return execution.result()

    with TemporaryDirectory(prefix="setrya-") as directory:
        try:
            execution.adapter = factory(RunContext(execution.run_id, Path(directory), drill))
        except (Exception, KeyboardInterrupt) as error:
            execution.add_error("factory", error)
        else:
            try:
                execution.run()
            except (Exception, KeyboardInterrupt) as error:
                execution.add_error(execution.operation, error)
            finally:
                try:
                    execution.adapter.close()
                except (Exception, KeyboardInterrupt) as error:
                    execution.add_error("close", error)
    return execution.result()


class _Execution(_Journal):
    operation = "seed"

    def call(self, operation: str, *args):
        self.operation = operation
        return getattr(self.adapter, operation)(*args)

    def refresh_after_commit(self, receipt) -> None:
        if self.rules.visibility != "observed-after-refresh" or receipt.outcome != "committed":
            return
        if not hasattr(self.adapter, "refresh"):
            return
        paths = set()
        for probe in self.drill.probes:
            path = (probe.rules.route, probe.rules.scope)
            if path not in paths:
                paths.add(path)
                ack = self.call("refresh", receipt, probe)
                if ack is not None:
                    if (ack.route, ack.scope) != path:
                        raise InputError("refresh acknowledged a different search route or caller")
                    self.add("refreshed", **asdict(ack))

    def probe(self, probe: Probe) -> dict:
        _, event = self.observe(probe, lambda configured: self.call("search", configured))
        return event

    def wait_until(self, timestamp: int) -> None:
        sleep(max(0, (timestamp - self.time()) / 1_000_000_000))

    def run(self) -> None:
        self.call("seed")
        if not self.baseline() or not self.hold() or not self.pending():
            return
        if self.changed(self.published(self.call("publish"))) and self.pending():
            self.finish()

    def baseline(self):
        freshness = next(probe for probe in self.drill.probes if probe.rules.id == self.rules.freshness_probe)
        self.add("seeded")
        for probe in self.drill.probes:
            if probe.rules.before:
                event = self.probe(probe)
                if probe == freshness and not establishes_baseline(event, probe.rules, freshness=True):
                    return False

        return True

    def hold(self):
        attempt = self.call("start_old")
        self.attempt = attempt
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
                return False
            self.wait_until(min(received + interval, deadline))
        else:
            return False
        expected_hold = (self.run_id, attempt, self.rules.document, self.rules.old_revision, self.rules.checkpoint)
        if (held.run_id, held.attempt, held.document, held.revision, held.name) != expected_hold:
            return False

        return True

    def pending(self):
        expected = Checkpoint(self.run_id, self.attempt, self.rules.document,
                              self.rules.old_revision, self.rules.checkpoint)
        return self.call("checkpoint") == expected and self.call("receipt") is None

    def published(self, publication):
        return self.add("published", publication=asdict(publication) if publication is not None else None)

    def changed(self, published):
        freshness = next(probe for probe in self.drill.probes if probe.rules.id == self.rules.freshness_probe)
        interval = self.rules.sample_ms * NS_PER_MS
        publication = published["publication"]
        if publication is None or (publication["run_id"], publication["document"], publication["revision"]) != (
            self.run_id, self.rules.document, self.rules.new_revision
        ):
            return False

        cutoff = published["ns"] + self.rules.freshness_ms * NS_PER_MS
        while True:
            event = self.probe(freshness)
            if matches_search(event, freshness.rules) and observed_change(event, freshness.rules):
                if event["ns"] > cutoff:
                    return False
                break
            if event["started_ns"] >= cutoff:
                return False
            self.wait_until(min(event["started_ns"] + interval, cutoff))

        # A second probe can expose a stale read while the worker is still held.
        for probe in self.drill.probes:
            event = self.probe(probe)
            if freshness.rules.absence and probe != freshness and (
                not matches_search(event, probe.rules) or not observed_change(event, probe.rules)
                or None in event["passages"]
            ):
                return False
        return True

    def finish(self):
        attempt = self.attempt
        interval = self.rules.sample_ms * NS_PER_MS
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
                    self.refresh_after_commit(receipt)
            for probe in self.drill.probes:
                self.probe(probe)
            if receipt is not None and self.result().verdict.status in {Status.PASS, Status.FAIL}:
                return
            if self.time() >= final_cutoff:
                break
            self.wait_until(min(sample_started + interval, final_cutoff))
