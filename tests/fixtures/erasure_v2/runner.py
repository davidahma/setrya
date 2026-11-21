"""Drive a pre-write hold, scoped deletes, two caller reads and post-drain store checks."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from setrya.offboarding.inputs import publish_new, require
from setrya.erasure import inventory as boundary
from setrya.erasure.journal import identifier
from setrya.erasure.model import Drain, Hold, Read, question_digest
from setrya.erasure.subject import Subject


def run_erasure(subject, *, journal, inventory, adapters, application, case, output, dispositions=()):
    from setrya.erasure.receipt import EVIDENCE, MAX_BUNDLE_FILE_BYTES, RECEIPT, derive

    start = monotonic()
    require(isinstance(subject, Subject), "Expected a tenant-bound test subject.")
    declared = boundary.load(Path(inventory))
    spec = case.spec()
    claims = [asdict(item) for item in dispositions]
    from setrya.erasure.receipt import validate_claims
    validate_claims(claims, declared, subject.ref)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    errors = []

    def observe(operation, fn):
        try:
            return fn()
        except Exception as exc:
            errors.append({"operation": operation, "error": type(exc).__name__})
            return None

    def attempt(fn):
        value = fn(subject)
        return identifier(value.attempt, "attempt") if isinstance(value, Hold) else None

    def reads():
        result = []
        for caller in case.callers:
            def read():
                r = application.read(subject, caller, case.question)
                if not isinstance(r, Read) or (r.caller, r.route, r.question) != (caller, case.route, case.question):
                    return None
                require(all(type(v) in (bool, type(None)) for v in (r.subject_present, r.control_present)),
                        "Invalid application observation.")
                return {"caller": caller, "route": r.route, "question_sha256": question_digest(r.question),
                        "subject_present": r.subject_present, "control_present": r.control_present}
            result.append(observe("read", read))
        return result

    remaining_key_bytes = 2 * 1024 * 1024
    remaining_keys = 8192

    def keys(fn):
        nonlocal remaining_key_bytes, remaining_keys
        found = fn(subject)
        require(isinstance(found, (set, frozenset)) and len(found) <= 4096, "Expected scoped copy keys.")
        valid = sorted(identifier(k, "copy key") for k in found)
        remaining_keys -= len(valid)
        remaining_key_bytes -= sum(map(len, valid))
        require(remaining_keys >= 0 and remaining_key_bytes >= 0, "Scope exceeds the evidence budget.")
        return valid

    stores = {}
    direct_adapters = set()
    for name, system in declared.items():
        adapter = adapters.get(system["adapter"]) if system["adapter"] != "none" else None
        proof = getattr(adapter, "proof", "none")
        require(proof in ("direct", "configuration", "none"), "Invalid adapter proof level.")
        if system["role"] == "provider" and proof == "direct":
            proof = "configuration"
        if proof == "direct":
            require(id(adapter) not in direct_adapters, "A direct adapter cannot stand in for two inventory systems.")
            direct_adapters.add(id(adapter))
        stores[name] = dict(proof=proof, before=None, deleted=False, immediate=None, final=None)

    held = observe("hold", lambda: attempt(application.hold))
    drained = None
    before = checkpoint = pre_release = None
    immediate = [None, None]
    try:
        before = reads()
        for name, row in stores.items():
            if row["proof"] == "direct":
                adapter = adapters[declared[name]["adapter"]]
                row["before"] = observe("find", lambda: keys(adapter.find))
        checkpoint = observe("checkpoint", lambda: attempt(application.checkpoint))
        armed = held is not None and checkpoint == held and all(
            r and r["subject_present"] is True and r["control_present"] is True for r in before)
        if armed:
            for name, row in stores.items():
                if row["before"] is not None:
                    adapter = adapters[declared[name]["adapter"]]
                    def delete():
                        adapter.delete(subject)
                        return True
                    row["deleted"] = observe("delete", delete) is True
            immediate = reads()
            # A later deletion or a reopened chat can repopulate an earlier store.
            # Check the whole boundary after these operations, while the worker is held.
            for name, row in stores.items():
                if row["before"] is not None:
                    adapter = adapters[declared[name]["adapter"]]
                    row["immediate"] = observe("verify_absent", lambda: keys(adapter.verify_absent))
            pre_release = observe("checkpoint", lambda: attempt(application.checkpoint))
    finally:
        if held is not None:
            def release():
                result = application.release_and_drain(subject, held)
                return bool(isinstance(result, Drain) and result.attempt == held and result.drained is True)
            drained = observe("release_and_drain", release)
    after = reads()
    for name, row in stores.items():
        if row["before"] is not None:
            adapter = adapters[declared[name]["adapter"]]
            row["final"] = observe("verify_absent", lambda: keys(adapter.verify_absent))

    history = observe("journal", lambda: [asdict(r) for r in journal.history(subject, max_bytes=2 * 1024 * 1024)])
    evidence = {"format": "setrya.erasure.evidence.v2", "run": uuid4().hex, "subject": asdict(subject),
                "observed_at": datetime.now(timezone.utc).isoformat(), "case": spec, "inventory": list(declared.values()),
                "worker": {"held": held, "checkpoint": checkpoint, "pre_release": pre_release, "drained": drained},
                "reads": {"before": before, "immediate": immediate, "final": after}, "stores": stores,
                "journal": history, "dispositions": claims, "errors": errors,
                "time_to_first_verdict_ms": 0}
    # Compute the decision before stopping its clock. Then bind the exact timed evidence bytes.
    receipt = derive(evidence, "0" * 64)
    elapsed = round(1000 * (monotonic() - start))
    evidence["time_to_first_verdict_ms"] = receipt["time_to_first_verdict_ms"] = elapsed
    digest = publish_new(evidence, destination / EVIDENCE, max_bytes=MAX_BUNDLE_FILE_BYTES)
    receipt["evidence"]["sha256"] = digest
    publish_new(receipt, destination / RECEIPT, max_bytes=MAX_BUNDLE_FILE_BYTES)
    return receipt
