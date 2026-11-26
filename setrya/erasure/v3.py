"""Drive a deletion or late-write drill against one shared conversation."""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

from setrya.offboarding.inputs import publish_new, publish_new_bytes, require
from .judges import AnswerJudges, TextAnswer, consensus
from .journal import CopyJournal, identifier
from .modes import evidence_mode
from setrya import provenance as source_provenance
from setrya.core import scan_scope
from .fs_canary import CanaryMatches
from .model import Drain, Hold, question_digest
from .receipt import EVIDENCE, MAX_BUNDLE_FILE_BYTES, RECEIPT
from .subject import Subject
from . import v3_receipt as rules


_RUN_OUTPUT = "SETRYA_LIVE_RUN_OUTPUT"
_RUN_CONFIRMATION = "SETRYA_LIVE_RUN_CONFIRMATION"


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
class Maintenance:
    """Acknowledge that the named maintenance operation finished."""
    step: str


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
    samples: int = 3
    target_variants: tuple[str, ...] = ()
    control_variants: tuple[str, ...] = ()
    model_backed: bool | None = None
    controls: tuple[dict[str, str], ...] = ()
    maintenance_steps: tuple[str, ...] = ()

    def questions(self):
        require(all(isinstance(group, tuple) and len(group) <= 7 for group in
                    (self.target_variants, self.control_variants)), "Supply at most seven variants per fact.")
        return (self.target_question, self.control_question, *self.target_variants, *self.control_variants)

    def spec(self):
        spec = {"conversation": self.conversation, "route": self.route, "callers": list(self.callers),
                "questions": [question_digest(q) for q in self.questions()],
                "sampling": {"samples": self.samples,
                             "question_roles": ["target", "control"] + ["target"] * len(self.target_variants)
                                               + ["control"] * len(self.control_variants),
                             "source": "application-signals" if self.judges is None else "answer-text"},
                "artifacts": [asdict(a) for a in self.artifacts], "scan_stores": list(self.scan_stores),
                "require_lineage": self.require_lineage}
        require(type(self.model_backed) in (bool, type(None)), "Invalid model-backed route setting.")
        require(self.judges is not None or self.model_backed is not True,
                "Model-backed routes require actual answer text and configured judges.")
        if self.judges is not None:
            require(isinstance(self.judges, AnswerJudges), "Expected target and control answer judges.")
            spec["judges"] = self.judges.spec()
        require(isinstance(self.controls, tuple), "Use a tuple of control references.")
        if self.controls:
            from setrya.control_references import validate_controls
            spec["controls"] = validate_controls(list(self.controls))
        require(isinstance(self.maintenance_steps, tuple), "Use a tuple of maintenance step names.")
        if self.maintenance_steps:
            spec["maintenance_steps"] = list(self.maintenance_steps)
        rules.check_spec(spec)
        _check_answer_budget(spec, self.judges)
        return spec


def _check_answer_budget(spec, judges):
    """Reserve room for all three stages before any fixture hook can run."""
    reads_per_caller = len(spec["questions"]) * spec["sampling"]["samples"]
    # 512 bytes cover the indented JSON fields and fixed-length digests of one
    # answer or decision. Caller, route and conversation IDs are restricted to ASCII.
    row_bytes = 512 + len(spec["route"]) + len(spec["conversation"])
    if judges is not None:
        row_bytes += judges.maximum_decision_bytes()
    stage_bytes = reads_per_caller * sum(row_bytes + len(caller) for caller in spec["callers"])
    # Leave half the file for direct reads, provenance and durable journal events.
    require(3 * stage_bytes <= MAX_BUNDLE_FILE_BYTES // 2,
            "Answer evidence could exceed the replay size budget. Use digest capture or reduce samples, questions or judges.")


def _inspect(subject, application, store, key):
    reading = application.inspect(subject, store, key)
    if not isinstance(reading, Stored) or reading.key != key:
        return None
    row = {"store": store, **asdict(reading)}
    rules.check_stored(row, store, key)
    return row


def _stage(subject, case, application, spec, observe, discovered=None):
    answers = []
    questions = dict(zip(spec["questions"], case.questions()))
    for caller, digest, _, ordinal in rules.answer_slots(spec):
        question = questions[digest]

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
                policy = spec["judges"]
                row = {"caller": answer.caller, "conversation": answer.conversation, "route": answer.route,
                       "target_present": consensus(judged["target"], policy["target"]),
                       "control_present": consensus(judged["control"], policy["control"]),
                       "judgments": judged}
            row.update(question=digest, sample=ordinal)
            rules.fact_flags(row)
            return row
        answers.append(observe("read", read))
    stored = [observe("inspect", lambda: _inspect(subject, application, a.store, a.key)) for a in case.artifacts]
    scans, scan_details = {}, {}
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
            if isinstance(keys, CanaryMatches):
                candidate = {**scan_details, store: keys.coverage}
                scan_scope.check_stage(candidate, {**scans, store: valid})
                scan_details[store] = deepcopy(keys.coverage)
            return valid
        scans[store] = observe("scan", scan)
    if discovered is None:
        discovered = rules.discovered_keys(spec, scans)
    found = [observe("inspect", lambda: _inspect(subject, application, store, key)) for store, key in discovered]
    return {"answers": answers, "stored": stored, "scans": scans, "discovered": found,
            **({"scan_details": scan_details} if scan_details else {})}


def observe_baseline(subject, case, application, *, external_items=(), on_gap=None, on_warning=None):
    """Read the fixture without erasing or holding a worker. Optionally report individual gaps."""
    require(isinstance(subject, Subject) and isinstance(case, SharedCase), "Expected a subject and shared case.")
    spec = case.spec()
    external = [asdict(item) for item in external_items]
    rules.check_external(external, spec, subject.ref)

    def observe(operation, fn):
        if on_gap is None:
            return fn()
        try:
            return fn()
        except Exception as error:
            # Keep checking the other boundaries. Hook errors can include secrets.
            on_gap(operation, None, None, f"Hook reported {type(error).__name__}.")
            return None

    baseline = _stage(subject, case, application, spec, observe)
    rules.check_stage(baseline, spec)
    issues = set(rules.baseline_issues(spec, baseline))
    if case.maintenance_steps and not callable(observe("maintain", lambda: getattr(application, "maintain", None))):
        issues.add(("maintain", None, None, "Connect maintain(subject, step) for the declared maintenance steps."))
    if on_warning is not None:
        for store, details in baseline.get("scan_details", {}).items():
            if details.get("skipped"):
                counts = ", ".join(f"{name} ({amount})" for name, amount in sorted(details["skipped"].items()))
                on_warning("scan", store, None, f"{store} has unmatched files in scan scope: {counts}. "
                           "Review include patterns or configure a separate store scanner.")
    if on_gap is not None:
        for issue in sorted(issues):
            on_gap(*issue)
    if issues:
        return False
    if spec["require_lineage"]:
        lineage = observe("lineage", lambda: application.lineage(subject))
        checks = {"format": rules.EVIDENCE_FORMAT, "case": spec, "baseline": baseline, "lineage": lineage}
        gaps = rules.lineage_gaps(checks, {(x["store"], x["key"]) for x in external})
        if on_gap is not None:
            for reason in sorted(gaps):
                on_gap("lineage", None, None, reason)
        return not gaps
    return True


def run_shared(subject, *, case, application, output, mode="late-write", external_items=(),
               journal=None, run_id=None, expected_producers=(), authorize_erase=None, authorize_hold=None,
               cancel_hold=None, erase_action=None, provenance=None, deployment_evidence=None):
    """Run trusted callbacks only against a disposable, authorized application fixture."""
    start = monotonic()
    require(isinstance(subject, Subject) and isinstance(case, SharedCase), "Expected a subject and shared case.")
    mode = evidence_mode(mode)
    require(mode in ("deletion", "late-write"), "Expected deletion or write-back mode.")
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
    if os.environ.get(_RUN_OUTPUT) == str(destination.resolve()):
        provided = source_provenance.from_environment()
        if provided is not None:
            require(provenance is None, "The live caller supplies provenance for this CLI run.")
            provenance = provided
    producer = source_provenance.live_provenance(provenance)
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
    maintenance = [None] * len(case.maintenance_steps)
    maintain = observe("maintain", lambda: getattr(application, "maintain", None)) if maintenance else None
    maintenance_ready = not maintenance or callable(maintain)
    lineage = None
    release_mark = None
    local_journal = isinstance(journal, CopyJournal) and not expected
    if maintenance_ready and mode == "late-write" and (authorize_hold is None or observe("authorize_hold", authorize_hold) is True):
        worker["held"] = observe("hold", lambda: held_by(application.hold))
    try:
        baseline = _stage(subject, case, application, spec, observe)
        if hasattr(application, "lineage"):
            def get_lineage():
                result = application.lineage(subject)
                rules.check_lineage(result)
                return result
            lineage = observe("lineage", get_lineage)
        if worker["held"] is not None:
            worker["checkpoint"] = observe("checkpoint", lambda: held_by(application.checkpoint))
        ready = maintenance_ready and rules.baseline_ready(spec, baseline) and rules.ready_for_mode(mode, worker)
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
                if erase:
                    for index, step in enumerate(case.maintenance_steps):
                        completed = observe("maintain", lambda: maintain(subject, step))
                        if not isinstance(completed, Maintenance) or completed.step != step:
                            break
                        stored = [observe("inspect", lambda: _inspect(subject, application, a.store, a.key))
                                  for a in case.artifacts]
                        maintenance[index] = {"step": step, "stored": stored}
                if mode == "late-write":
                    worker["pre_release"] = observe("checkpoint", lambda: held_by(application.checkpoint))
                    if erase and worker["pre_release"] == worker["held"] and local_journal:
                        release_mark = observe("journal_mark", journal.mark)
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
    drain_mark = (observe("journal_mark", journal.mark)
                  if release_mark is not None and worker["drained"] is not None else None)
    final = _stage(subject, case, application, spec, observe, rules.discovered_keys(spec, baseline["scans"]))
    late_residuals = []
    if drain_mark is not None:
        late_residuals = [observe("inspect_late_residual", lambda: _inspect(subject, application, store, key))
                          for store, key in rules.new_residual_keys(spec, baseline["scans"], final["scans"])]
    coverage = events = chain = window = None
    if journal is not None:
        if expected and hasattr(application, "collect_journals"):
            observe("collect_journals", lambda: application.collect_journals(subject))
        budget = 2 * 1024 * 1024
        if expected:
            def snapshot():
                if hasattr(journal, "verified_snapshot"):
                    manifest, heads, rows = journal.verified_snapshot(subject, run_id=run, expected_producers=expected,
                                                                        max_bytes=budget)
                else:
                    manifest, rows = journal.snapshot(subject, run_id=run, expected_producers=expected, max_bytes=budget)
                    heads = dict.fromkeys(manifest["received"])
                return manifest, heads, [row.wire() if hasattr(row, "wire") else asdict(row) for row in rows]
            observation = observe("journal", snapshot)
            if observation is not None:
                coverage, chain, events = observation
        else:
            def snapshot():
                bounds = None
                if release_mark is not None and drain_mark is not None:
                    head, rows, bounds = journal.verified_window(subject, release_mark, drain_mark, max_bytes=budget)
                elif hasattr(journal, "verified_history"):
                    head, rows = journal.verified_history(subject, max_bytes=budget)
                else:
                    head, rows = None, journal.history(subject, max_bytes=budget)
                return head, [row.wire() if hasattr(row, "wire") else asdict(row) for row in rows], bounds
            observation = observe("journal", snapshot)
            if observation is not None:
                chain, events, window = observation
    evidence = {"format": rules.EVIDENCE_FORMAT, "run": run, "scope": "person",
                "observed_at": datetime.now(timezone.utc).isoformat(), "subject": asdict(subject),
                "mode": mode, "case": spec, "worker": worker, "external": external,
                "lineage": lineage, "baseline": rules.group_samples(baseline, spec), "erased": erase,
                "immediate": rules.group_samples(immediate, spec), "final": rules.group_samples(final, spec),
                "expected_producers": expected, "coverage": coverage, "journal": events, "journal_chain": chain,
                "errors": errors, "drill_duration_ms": 0, "provenance": producer}
    if maintenance:
        evidence["maintenance"] = maintenance
    if deployment_evidence is not None:
        evidence.update(format=rules.DEPLOYMENT_EVIDENCE_FORMAT, deployment=deployment_evidence)
    if window and window["start"] < window["stop"]:
        evidence["late_write_window"] = window
        if late_residuals:
            evidence["late_residuals"] = late_residuals
    receipt = rules.derive(evidence, "0" * 64)
    evidence["drill_duration_ms"] = receipt["drill_duration_ms"] = round((monotonic() - start) * 1000)
    receipt["evidence"]["sha256"] = publish_new(evidence, destination / EVIDENCE, max_bytes=MAX_BUNDLE_FILE_BYTES)
    publish_new(receipt, destination / RECEIPT, max_bytes=MAX_BUNDLE_FILE_BYTES)
    # Bind a CLI invocation to what this live runner issued, before fixture teardown.
    confirmation = os.environ.get(_RUN_CONFIRMATION)
    if confirmation and os.environ.get(_RUN_OUTPUT) == str(destination.resolve()):
        publish_new_bytes(receipt["evidence"]["sha256"].encode("ascii"), Path(confirmation))
    return receipt
