"""Drive a deletion or late-write drill against one shared conversation."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from setrya.offboarding.inputs import publish_new, require
from .journal import identifier
from .model import Drain, Hold, question_digest
from .receipt import EVIDENCE, MAX_BUNDLE_FILE_BYTES, RECEIPT
from .subject import Subject
from . import v3_receipt as rules


@dataclass(frozen=True)
class Artifact:
    store: str
    key: str
    action: str  # delete, rewrite, or preserve


@dataclass(frozen=True)
class Stored:
    """A read of the exact key, including the observed revision or confirmed absence."""
    key: str
    exists: bool
    target_present: bool | None
    control_present: bool | None
    revision: str | None


@dataclass(frozen=True)
class Answer:
    """Identity and content observations from the actual serving response."""
    caller: str
    conversation: str
    route: str
    question: str
    target_present: bool | None
    control_present: bool | None


@dataclass(frozen=True)
class ExternalItem:
    store: str
    key: str
    owner: str
    kind: str
    evidence: str
    follow_up: str | None = None
    subject_ref: str | None = None


@dataclass(frozen=True)
class SharedCase:
    conversation: str
    route: str
    callers: tuple[str, str]
    target_question: str
    control_question: str
    artifacts: tuple[Artifact, ...]
    scan_stores: tuple[str, ...] = ()
    require_lineage: bool = False

    def spec(self):
        spec = {"conversation": self.conversation, "route": self.route, "callers": list(self.callers),
                "questions": [question_digest(self.target_question), question_digest(self.control_question)],
                "artifacts": [asdict(a) for a in self.artifacts], "scan_stores": list(self.scan_stores),
                "require_lineage": self.require_lineage}
        rules.check_spec(spec)
        return spec


def run_shared(subject, *, case, application, output, mode="late-write", external_items=(),
               journal=None, run_id=None, expected_producers=()):
    """Run trusted callbacks only against a disposable, authorized application fixture."""
    start = monotonic()
    require(isinstance(subject, Subject) and isinstance(case, SharedCase), "Expected a subject and shared case.")
    require(mode in ("deletion", "late-write"), "Expected deletion or late-write mode.")
    run = identifier(uuid4().hex if run_id is None else run_id, "run")
    spec = case.spec()
    external = [asdict(item) for item in external_items]
    rules.check_external(external, spec, subject.ref)
    expected = sorted(identifier(p, "producer") for p in expected_producers)
    require(len(expected) <= 256 and len(expected) == len(set(expected)), "Invalid expected producer list.")
    require(not hasattr(journal, "coverage") or expected, "Declare expected producers for a collected journal.")
    require(not expected or run_id is not None, "Distributed journals need the run ID used by their producers.")
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    errors = []

    def observe(operation, fn):
        try:
            return fn()
        except Exception as exc:
            errors.append({"operation": operation, "error": type(exc).__name__})
            return None

    def inspect(store, key):
        reading = application.inspect(subject, store, key)
        if not isinstance(reading, Stored) or reading.key != key:
            return None
        row = {"store": store, **asdict(reading)}
        rules.check_stored(row, store, key)
        return row

    def stage(discovered=None):
        answers = []
        for caller in case.callers:
            for question in (case.target_question, case.control_question):
                def read():
                    answer = application.read(subject, caller, case.conversation, question)
                    if not isinstance(answer, Answer) or (
                        answer.caller, answer.conversation, answer.route, answer.question
                    ) != (caller, case.conversation, case.route, question):
                        return None
                    row = asdict(answer)
                    row["question"] = question_digest(answer.question)
                    rules.fact_flags(row)
                    return row
                answers.append(observe("read", read))
        stored = [observe("inspect", lambda: inspect(a.store, a.key)) for a in case.artifacts]
        scans = {}
        remaining_keys, remaining_bytes = 512, 256 * 1024
        for store in case.scan_stores:
            def scan():
                nonlocal remaining_keys, remaining_bytes
                keys = application.scan(subject, store)
                require(isinstance(keys, (set, frozenset)) and len(keys) <= 512, "Expected bounded canary matches.")
                valid = sorted(identifier(k, "canary location") for k in keys)
                remaining_keys -= len(valid)
                remaining_bytes -= sum(map(len, valid))
                require(remaining_keys >= 0 and remaining_bytes >= 0, "Canary scope exceeds the stage budget.")
                return valid
            scans[store] = observe("scan", scan)
        if discovered is None:
            discovered = rules.discovered_keys(spec, scans)
        found = [observe("inspect", lambda: inspect(store, key)) for store, key in discovered]
        return {"answers": answers, "stored": stored, "scans": scans, "discovered": found}

    def held_by(fn):
        held = fn(subject)
        return identifier(held.attempt, "worker attempt") if isinstance(held, Hold) else None

    worker = dict(held=None, checkpoint=None, pre_release=None, drained=None)
    erase = None
    immediate = None
    lineage = None
    if mode == "late-write":
        worker["held"] = observe("hold", lambda: held_by(application.hold))
    try:
        baseline = stage()
        if hasattr(application, "lineage"):
            def get_lineage():
                result = application.lineage(subject)
                rules.check_lineage(result)
                return result
            lineage = observe("lineage", get_lineage)
        if mode == "late-write":
            worker["checkpoint"] = observe("checkpoint", lambda: held_by(application.checkpoint))
        if rules.baseline_ready(spec, baseline) and rules.ready_for_mode(mode, worker):
            def delete():
                application.erase(subject)
                return True
            erase = observe("erase", delete) is True
            immediate = stage(rules.discovered_keys(spec, baseline["scans"]))
            if mode == "late-write":
                worker["pre_release"] = observe("checkpoint", lambda: held_by(application.checkpoint))
    finally:
        if worker["held"] is not None:
            def release():
                result = application.release_and_drain(subject, worker["held"])
                if isinstance(result, Drain) and result.attempt == worker["held"] and result.drained is True:
                    return result.attempt
                return None
            worker["drained"] = observe("release_and_drain", release)
    final = stage(rules.discovered_keys(spec, baseline["scans"]))
    coverage = events = None
    if journal is not None:
        if expected and hasattr(application, "collect_journals"):
            observe("collect_journals", lambda: application.collect_journals(subject))
        if expected:
            coverage = observe("coverage", lambda: journal.coverage(subject, run_id=run, expected_producers=expected))
        events = observe("journal", lambda: [asdict(row) for row in journal.history(subject, max_bytes=2 * 1024 * 1024)])
    evidence = {"format": rules.EVIDENCE_FORMAT, "run": run, "scope": "person",
                "observed_at": datetime.now(timezone.utc).isoformat(), "subject": asdict(subject),
                "mode": mode, "case": spec, "worker": worker, "external": external,
                "lineage": lineage, "baseline": baseline, "erased": erase, "immediate": immediate, "final": final,
                "expected_producers": expected, "coverage": coverage, "journal": events,
                "errors": errors, "drill_duration_ms": 0}
    receipt = rules.derive(evidence, "0" * 64)
    evidence["drill_duration_ms"] = receipt["drill_duration_ms"] = round((monotonic() - start) * 1000)
    receipt["evidence"]["sha256"] = publish_new(evidence, destination / EVIDENCE, max_bytes=MAX_BUNDLE_FILE_BYTES)
    publish_new(receipt, destination / RECEIPT, max_bytes=MAX_BUNDLE_FILE_BYTES)
    return receipt
