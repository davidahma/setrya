"""Live conformance checks for adapter authors using disposable applications."""
from collections.abc import Callable
from pathlib import Path
import math
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from .adapter import Checkpoint, Publication, Receipt
from .checker import Status, evaluate
from .drill import Drill, label
from .evidence import Report, matches_search
from .runner import run


class ContractError(AssertionError):
    """An adapter's observed behavior did not meet the selected contract."""


def _require(ok, message):
    if not ok:
        raise ContractError(message)


def _terminal(report):
    events = {e["kind"]: e for e in report.events if e["kind"] in ("started", "held", "released", "receipt")}
    _require(len(events) == 4, "expected a started, held and released attempt with a terminal receipt")
    start, hold, release, receipt = (events[name] for name in ("started", "held", "released", "receipt"))
    for event in (hold, receipt):
        _require((event["run_id"], event["attempt"], event["document"], event["revision"])
                 == (report.run_id, start["attempt"], report.rules.document, report.rules.old_revision),
                 "checkpoint and receipt must identify the actual captured input and attempt")
    _require(hold["name"] == report.rules.checkpoint and release["attempt"] == start["attempt"]
             and receipt["ns"] - release["ns"] <= report.rules.settle_ms * 1_000_000,
             "expected the declared checkpoint, release and timely terminal write")
    return receipt


def assert_store_contract(*, broken: Drill, guarded: Drill, opaque: Drill,
                          output: str | Path | None = None, expected_tier: str = "write-confirmed"):
    """Run three fixtures for the same drill and replay the evidence after teardown.

    The broken worker must expose a forbidden passage after committing. The
    opaque worker must commit and return correct final content without evidence
    of write visibility. Supply a new directory to retain the three journals.
    """
    _require(expected_tier in ("write-confirmed", "observed-after-refresh"), "unknown passing evidence tier")
    for case in (broken, guarded, opaque):
        case.validate()
        _require(case.factory and (case.factory, case.probes, case.rules) == (guarded.factory, guarded.probes, guarded.rules),
                 "contract cases must use one adapter, rule set, caller and query set")
    with TemporaryDirectory(prefix="setrya-contract-") as scratch:
        out = Path(scratch)
        if output is not None:
            out = Path(output)
            out.mkdir(parents=True, exist_ok=False)
        reports = {}
        for name, case in (("broken", broken), ("guarded", guarded), ("opaque", opaque)):
            live = run(case)
            path = out / (name + ".json")
            live.report.save(path)
            report = Report.load(path)
            verdict = evaluate(report)
            _require(verdict == live.verdict, f"{name}: saved evidence must reproduce the live verdict")
            _require(not any(e["kind"] == "adapter_error" for e in report.events), f"{name}: adapter execution error")
            receipt = _terminal(report)
            if name == "guarded":
                _require((verdict.status, verdict.evidence_tier) == (Status.PASS, expected_tier),
                         f"guarded: expected PASS with {expected_tier}; observed {verdict.status.label} ({verdict.detail})")
            elif name == "broken":
                _require(verdict.status is Status.FAIL and verdict.event is not None,
                         f"broken: expected an observed failure; got {verdict.status.label}")
                probe = next((p for p in case.rules.probes if p.id == verdict.probe), None)
                decisive = report.events[verdict.event - 1]
                _require(probe is not None and decisive.get("stage") == "final"
                         and set(probe.forbid).intersection(decisive["passages"]),
                         "broken: the decisive forbidden response must occur after worker release")
                _require(receipt["outcome"] == "committed" and any(
                    e.get("stage") == "final" and e["probe_id"] == probe.id and matches_search(e, probe)
                    and e["started_ns"] >= receipt["ns"] and set(probe.forbid).intersection(e["passages"])
                    for e in report.events),
                         "broken: the tested caller must retrieve a forbidden passage after the committed receipt")
            else:
                _require(verdict.status is Status.NOT_TESTED and receipt["outcome"] == "committed"
                         and verdict.detail == "Missing search visibility evidence for the old write",
                         f"opaque: expected missing read visibility after a commit; got {verdict.status.label} ({verdict.detail})")
                for probe in case.rules.probes:
                    reads = [e for e in report.events if e.get("stage") == "final" and e["probe_id"] == probe.id]
                    last = reads[-1] if reads else None
                    _require(last and matches_search(last, probe) and last["started_ns"] >= receipt["ns"]
                             and None not in last["passages"] and set(probe.require) <= set(last["passages"])
                             and set(probe.forbid).isdisjoint(last["passages"]),
                             f"opaque: missing a clean post-write response for {probe.id}")
            reports[name] = report
        return reports


def _poll(read, timeout, description):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        value = read()
        if value is not None and monotonic() <= deadline:
            return value
        sleep(min(0.01, max(0, deadline - monotonic())))
    raise ContractError(f"timed out waiting for {description}")


def assert_queue_contract(*, dispatch: Callable[[], str], checkpoint: Callable, publish: Callable,
                          release: Callable, receipt: Callable, change: Publication, old_revision: str,
                          checkpoint_name: str, outcome: str, timeout: float = 10) -> Receipt:
    """Exercise a native task in a caller-owned disposable worker fixture.

    Observation callbacks must return promptly, or None while waiting. The test
    owns shutdown, including teardown following a failed assertion.
    """
    _require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
             and math.isfinite(timeout) and timeout > 0, "timeout must be positive and finite")
    _require(isinstance(change, Publication), "expected the application's declared publication")
    for value, field in ((change.run_id, "run ID"), (change.document, "document"),
                         (change.revision, "published revision"), (old_revision, "captured revision"),
                         (checkpoint_name, "checkpoint name")):
        label(value, field)
    _require(outcome in ("committed", "rejected") and old_revision != change.revision,
             "select the expected terminal outcome and distinct change revision")
    task_id = label(dispatch(), "native task ID")
    expected = Checkpoint(change.run_id, task_id, change.document, old_revision, checkpoint_name)
    _require(_poll(checkpoint, timeout, "capture") == expected, "worker captured a different input or task")
    _require(receipt() is None, "worker completed before the test published the change")
    _require(publish() == change, "application did not acknowledge the expected change")
    _require(checkpoint() == expected and receipt() is None, "worker did not stay held across the change")
    release()
    terminal = _poll(receipt, timeout, "terminal receipt")
    _require(isinstance(terminal, Receipt) and (terminal.run_id, terminal.attempt, terminal.document,
             terminal.revision, terminal.outcome) == (change.run_id, task_id, change.document, old_revision, outcome),
             "expected the released task's own terminal receipt")
    if outcome == "rejected":
        _require(terminal.generation is None and terminal.commit is None, "a rejection cannot claim a committed write")
    else:
        label(terminal.generation, "native store generation")
        _require(terminal.commit is None or type(terminal.commit) is int and 0 <= terminal.commit < 2**64,
                 "committed receipt has an invalid position")
    return terminal
