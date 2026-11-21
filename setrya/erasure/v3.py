"""Drive a deletion or late-write drill against one shared conversation."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from setrya.offboarding.inputs import publish_new, require
from .judges import AnswerJudges, TextAnswer
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
    callers: tuple[str, ...]
    target_question: str
    control_question: str
    artifacts: tuple[Artifact, ...]
    scan_stores: tuple[str, ...] = ()
    require_lineage: bool = False
    judges: AnswerJudges | None = None

    def spec(self):
        spec = {"conversation": self.conversation, "route": self.route, "callers": list(self.callers),
                "questions": [question_digest(self.target_question), question_digest(self.control_question)],
                "artifacts": [asdict(a) for a in self.artifacts], "scan_stores": list(self.scan_stores),
                "require_lineage": self.require_lineage}
        if self.judges is not None:
            require(isinstance(self.judges, AnswerJudges), "Expected target and control answer judges.")
            spec["judges"] = self.judges.spec()
        rules.check_spec(spec)
        return spec


def _stage(subject, case, application, spec, observe, discovered=None):
    def inspect(store, key):
        reading = application.inspect(subject, store, key)
        if not isinstance(reading, Stored) or reading.key != key:
            return None
        row = {"store": store, **asdict(reading)}
        rules.check_stored(row, store, key)
        return row

    answers = []
    for caller in case.callers:
        for question in (case.target_question, case.control_question):
            def read():
                answer = application.read(subject, caller, case.conversation, question)
                expected_type = TextAnswer if case.judges is not None else Answer
                if not isinstance(answer, expected_type) or (
                    answer.caller, answer.conversation, answer.route, answer.question
                ) != (caller, case.conversation, case.route, question):
                    return None
                if case.judges is None:
                    row = asdict(answer)
                else:
                    judged = case.judges.inspect(answer.text, observe)
                    row = {"caller": answer.caller, "conversation": answer.conversation, "route": answer.route,
                           "target_present": (judged["target"] or {}).get("present"),
                           "control_present": (judged["control"] or {}).get("present"),
                           "judgments": judged}
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


def observe_baseline(subject, case, application, *, external_items=()):
    """Read the declared fixture scope without holding a worker or applying erasure."""
    require(isinstance(subject, Subject) and isinstance(case, SharedCase), "Expected a subject and shared case.")
    spec = case.spec()
    external = [asdict(item) for item in external_items]
    rules.check_external(external, spec, subject.ref)

    def observe(_operation, fn):
        return fn()

    baseline = _stage(subject, case, application, spec, observe)
    rules.check_stage(baseline, spec)
    if not rules.baseline_ready(spec, baseline):
        return False
    if spec["require_lineage"]:
        lineage = application.lineage(subject)
        checks = {"case": spec, "baseline": baseline, "lineage": lineage}
        return not rules.lineage_gaps(checks, {(x["store"], x["key"]) for x in external})
    return True


def run_shared(subject, *, case, application, output, mode="late-write", external_items=(),
               journal=None, run_id=None, expected_producers=(), authorize_erase=None, authorize_hold=None,
               cancel_hold=None, erase_action=None):
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
    if hasattr(journal, "snapshot"):
        require(expected, "Declare expected producers for a collected journal.")
    require(not expected or run_id is not None, "Distributed journals need the run ID used by their producers.")
    if mode == "late-write" and cancel_hold is None:
        cancel_hold = getattr(application, "cancel_and_drain", None)
    require(cancel_hold is None or callable(cancel_hold), "Expected a callable worker cancellation hook.")
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    errors = []

    def observe(operation, fn):
        try:
            return fn()
        except Exception as exc:
            errors.append({"operation": operation, "error": type(exc).__name__})
            return None

    def held_by(fn):
        held = fn(subject)
        return identifier(held.attempt, "worker attempt") if isinstance(held, Hold) else None

    worker = dict(held=None, checkpoint=None, pre_release=None, drained=None)
    erase = None
    immediate = None
    lineage = None
    if mode == "late-write" and (authorize_hold is None or observe("authorize_hold", authorize_hold) is True):
        worker["held"] = observe("hold", lambda: held_by(application.hold))
    try:
        baseline = _stage(subject, case, application, spec, observe)
        if hasattr(application, "lineage"):
            def get_lineage():
                result = application.lineage(subject)
                rules.check_lineage(result)
                return result
            lineage = observe("lineage", get_lineage)
        if mode == "late-write":
            worker["checkpoint"] = observe("checkpoint", lambda: held_by(application.checkpoint))
        ready = rules.baseline_ready(spec, baseline) and rules.ready_for_mode(mode, worker)
        if ready and authorize_erase is not None:
            ready = observe("authorize_erase", lambda: authorize_erase(spec, baseline, lineage)) is True
        if ready:
            def delete():
                if erase_action is None:
                    application.erase(subject)
                    return True
                applied = erase_action(subject)
                require(type(applied) is bool, "A scoped erase must acknowledge commit or atomic refusal.")
                return applied
            applied = observe("erase", delete)
            # False from the scoped adapter is an atomic compare-and-write refusal:
            # it made no changes. An exception may follow a partial mutation.
            if applied is not False:
                erase = applied is True
                immediate = _stage(subject, case, application, spec, observe, rules.discovered_keys(spec, baseline["scans"]))
                if mode == "late-write":
                    worker["pre_release"] = observe("checkpoint", lambda: held_by(application.checkpoint))
    finally:
        if worker["held"] is not None and cancel_hold is not None and (
            erase is not True or worker["pre_release"] != worker["held"]
        ):
            def cancel():
                result = cancel_hold(subject, worker["held"])
                require(isinstance(result, Drain) and result.attempt == worker["held"] and result.drained is True,
                        "Cancellation must confirm the exact retired worker.")
            observe("cancel_and_drain", cancel)
        elif worker["held"] is not None:
            def release():
                result = application.release_and_drain(subject, worker["held"])
                if isinstance(result, Drain) and result.attempt == worker["held"] and result.drained is True:
                    return result.attempt
                return None
            worker["drained"] = observe("release_and_drain", release)
    final = _stage(subject, case, application, spec, observe, rules.discovered_keys(spec, baseline["scans"]))
    coverage = events = None
    if journal is not None:
        if expected and hasattr(application, "collect_journals"):
            observe("collect_journals", lambda: application.collect_journals(subject))
        budget = 2 * 1024 * 1024
        if expected:
            def snapshot():
                manifest, rows = journal.snapshot(subject, run_id=run, expected_producers=expected, max_bytes=budget)
                return manifest, [asdict(row) for row in rows]
            observation = observe("journal", snapshot)
            if observation is not None:
                coverage, events = observation
        else:
            events = observe("journal", lambda: [asdict(row) for row in journal.history(subject, max_bytes=budget)])
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
