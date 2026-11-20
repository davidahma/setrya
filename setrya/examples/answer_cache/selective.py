"""Check a superseding refund policy and a delivery answer that should stay cached."""
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from setrya.cache_metrics import CacheMetrics
from setrya.checker import Status, Verdict
from setrya.dependencies import AnswerDependencies
from setrya.drill import InputError, label
from setrya.offboarding.inputs import read_json, save
from .contract import CHECKPOINT, DELIVERY, DELIVERY_QUERY, DELIVERY_TEXT, NEXT, NEXT_TEXT, PRIVATE, PRIVATE_TEXT, REFUND_QUERY

FORMAT = "setrya.selective-cache"
JOURNAL = "selective-run.json"
STAGES = ("baseline", "published", "released", "refilled")
QUERIES = {"refund": REFUND_QUERY, "delivery": DELIVERY_QUERY}
BODIES = {PRIVATE_TEXT: "old-refund", NEXT_TEXT: "new-refund", DELIVERY_TEXT: "delivery"}
MARKERS = {"old-refund": PRIVATE, "new-refund": NEXT, "delivery": DELIVERY}


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _body(text):
    return BODIES.get(text) if isinstance(text, str) else None


def _row(stage, scope, response):
    return {
        "stage": stage, "scope": scope, "body": _body(response.get("answer")),
        **{key: response.get(key) for key in (
            "http_status", "caller", "route", "query_sha256", "cache_hit", "generation_skipped",
            "cache_writer", "sources", "dependencies", "generation", "commit",
        )},
    }


def _capture_fact(captured):
    if captured is None:
        return None
    return {
        "run_id": captured["run_id"], "attempt": captured["attempt"], "caller": captured["caller"],
        "body": _body(captured["answer"]), "query_sha256": _digest(captured["question"]),
        "dependencies": captured["dependencies"],
    }


def _checkpoint(run_id, task):
    return {"run_id": run_id, "attempt": task, "document": "policy", "revision": "granted", "name": CHECKPOINT}


def _located(row, run_id, scope):
    return (row.get("http_status") == 200 and row.get("caller") == "retained" and row.get("route") == "answer"
            and row.get("query_sha256") == _digest(QUERIES[scope]) and row.get("generation") == run_id)


def _saw_change(trace):
    # Release only after the intended caller observed the replacement while
    # the worker was held. The delivery control can legitimately be a miss.
    if len(trace["reads"]) < 4:
        return False
    refund, delivery = trace["reads"][2:4]
    return (isinstance(refund, dict) and isinstance(delivery, dict)
            and (refund.get("stage"), refund.get("scope")) == ("published", "refund")
            and (delivery.get("stage"), delivery.get("scope")) == ("published", "delivery")
            and _located(refund, trace["run_id"], "refund") and _located(delivery, trace["run_id"], "delivery")
            and refund.get("body") == "new-refund" and refund.get("sources") == [NEXT])


def _exercise(app, trace, wait_seconds):
    from setrya.endpoint_runner import _wait

    def observe(stage):
        for scope, question in QUERIES.items():
            response = app.answer("retained", question)
            trace["reads"].append(_row(stage, scope, response))

    def pending(stage):
        attempt = trace["task"]
        terminal = app.receipt(attempt)
        trace["receipt_checks"][stage] = {
            "run_id": app.run_id, "attempt": attempt, "receipt": asdict(terminal) if terminal else None,
        }
        return terminal is None

    app.seed()
    observe("baseline")
    trace["task"] = app.start_old("retained", REFUND_QUERY)
    held = _wait(app.checkpoint, wait_seconds)
    if held is None:
        return
    trace["held"] = asdict(held)
    if trace["held"] != _checkpoint(trace["run_id"], trace["task"]):
        return
    trace["capture"] = _capture_fact(app.captured())
    if _worker_input(trace["capture"], trace["run_id"], trace["task"], "old-refund") is None:
        return
    trace["published"] = app.store.supersede_refund()
    if not pending("published"):
        return
    observe("published")
    if not _saw_change(trace) or app.checkpoint() != held:
        return
    trace["still_held"] = asdict(held)
    if not pending("before_release"):
        return
    app.release()
    receipt = _wait(app.receipt, wait_seconds)
    if receipt is None:
        return
    trace["old_receipt"] = asdict(receipt)
    observe("released")
    trace["refill"] = app.refill("retained", REFUND_QUERY)
    receipt = _wait(lambda: app.receipt(trace["refill"]), wait_seconds)
    if receipt is None:
        return
    trace["new_receipt"] = asdict(receipt)
    trace["refill_capture"] = _capture_fact(app.store.captured(trace["refill"]))
    observe("refilled")


def _new_trace():
    trace = dict.fromkeys(("task", "held", "capture", "published", "still_held", "old_receipt",
                          "refill", "new_receipt", "refill_capture", "error"))
    trace.update(run_id=uuid4().hex, reads=[], receipt_checks={})
    return trace


def run(directory, mode):
    if mode not in ("guarded", "broken"):
        raise InputError("unknown selective cache mode")
    try:
        from .app import Application
    except ModuleNotFoundError:
        raise InputError("install the answer-cache extra to run the live drill") from None

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=False)
    trace = _new_trace()
    try:
        with TemporaryDirectory(prefix="setrya-selective-") as scratch:
            with Application(trace["run_id"], "guarded" if mode == "guarded" else "global", scratch) as app:
                _exercise(app, trace, 30)
    except (Exception, KeyboardInterrupt) as exc:
        trace["error"] = type(exc).__name__
    save(trace, out / JOURNAL)
    digest = sha256((out / JOURNAL).read_bytes()).hexdigest()
    save({"format": FORMAT, "version": 1, "run_id": trace["run_id"], "mode": mode,
          "trace_sha256": digest}, out / "answer-cache.json")
    return replay(out)


def _load(directory):
    path = Path(directory)
    meta, _ = read_json(path / "answer-cache.json", "selective cache manifest")
    trace, digest = read_json(path / JOURNAL, "selective cache journal")
    if (not isinstance(meta, dict) or set(meta) != {"format", "version", "run_id", "mode", "trace_sha256"}
            or meta["format"] != FORMAT or type(meta["version"]) is not int or meta["version"] != 1
            or meta["mode"] not in ("broken", "guarded") or meta["trace_sha256"] != digest
            or not isinstance(trace, dict) or trace.get("run_id") != meta["run_id"]
            or set(trace) != {"run_id", "reads", "task", "held", "capture", "published", "still_held", "old_receipt",
                              "refill", "new_receipt", "refill_capture", "receipt_checks", "error"}):
        raise InputError("selective cache journal does not match its manifest")
    label(meta["run_id"], "run ID")
    if not isinstance(trace["reads"], list) or len(trace["reads"]) > 8:
        raise InputError("invalid selective cache request count")
    if not isinstance(trace["receipt_checks"], dict) or not set(trace["receipt_checks"]) <= {"published", "before_release"}:
        raise InputError("invalid selective receipt lookup stages")
    return trace


def _receipt(value, run_id, task, outcome):
    if not isinstance(value, dict) or not task:
        return False
    expected = {"run_id": run_id, "attempt": task, "document": "policy", "revision": "granted", "outcome": outcome}
    if set(value) != {*expected, "commit", "generation"} or any(value[k] != v for k, v in expected.items()):
        return False
    if outcome == "rejected":
        return value.get("generation") is None and value.get("commit") is None
    return value.get("generation") == run_id and type(value.get("commit")) is int and value["commit"] > 0


def _bound(record, run_id, scope):
    return (record.tenant == run_id and record.access_scope == "retained"
            and record.retrieval_scope == scope)


def _worker_input(fact, run_id, attempt, body):
    if (not attempt or not isinstance(fact, dict) or not fact.get("dependencies")
            or (fact.get("run_id"), fact.get("attempt"), fact.get("caller"), fact.get("body"),
                fact.get("query_sha256")) != (run_id, attempt, "retained", body, _digest(REFUND_QUERY))):
        return None
    record = AnswerDependencies.from_dict(fact["dependencies"])
    if _bound(record, run_id, "refund") and record.markers == (MARKERS[body],):
        return record
    return None


def _advanced(after, before):
    # PostgreSQL epochs are nonnegative signed bigints.
    return all(v.isascii() and v.isdigit() and len(v) <= 19 for v in (after, before)) and int(after) > int(before)


def _assess(trace):
    run_id, task, refill = (trace[k] for k in ("run_id", "task", "refill"))
    for identity in (task, refill):
        if identity is not None:
            label(identity, "task ID")
    versions = trace["published"]
    published = (isinstance(versions, dict) and set(versions) == set(QUERIES)
                 and all(isinstance(v, str) and v.isascii() and v.isdigit() and len(v) <= 19
                         for v in versions.values()))
    checkpoint = _checkpoint(run_id, task)
    absent = {"run_id": run_id, "attempt": task, "receipt": None}
    held = bool(task and trace["held"] == checkpoint and trace["still_held"] == checkpoint
                and trace["receipt_checks"] == {"published": absent, "before_release": absent})
    old = _worker_input(trace["capture"], run_id, task, "old-refund")
    fresh = _worker_input(trace["refill_capture"], run_id, refill, "new-refund")
    rejected = _receipt(trace["old_receipt"], run_id, task, "rejected")
    committed = task != refill and _receipt(trace["new_receipt"], run_id, refill, "committed")
    failures, gaps, lines = [], [], []
    baseline = {}
    stats = CacheMetrics()
    def matched_late_capture():
        warm = baseline.get("refund", (None, None))[0]
        return (held and published and _saw_change(trace) and old is not None and warm is not None
                and old.access_version == warm.access_version and old.retrieval_version == warm.retrieval_version
                and _advanced(versions["refund"], old.retrieval_version))

    for index, row in enumerate(trace["reads"]):
        stage, scope = STAGES[index // 2], tuple(QUERIES)[index % 2]
        if not isinstance(row, dict) or (row.get("stage"), row.get("scope")) != (stage, scope):
            raise InputError("out-of-order selective cache observations")
        name = f"{stage} {scope}"
        body = row.get("body")
        if body not in (None, *MARKERS):
            raise InputError("invalid selective answer classification")
        expected = "delivery" if scope == "delivery" else ("old-refund" if stage == "baseline" else "new-refund")
        record = AnswerDependencies.from_dict(row["dependencies"]) if row.get("dependencies") is not None else None
        located = _located(row, run_id, scope)
        writer = row.get("cache_writer")
        if writer is not None:
            label(writer, "cache writer")
        hit = row.get("cache_hit") if located else None
        if type(hit) is not bool or hit != bool(writer):
            hit = None
        skipped = row.get("generation_skipped") if hit is not None else None
        if type(skipped) is not bool or (skipped and not hit):
            skipped = None
        prior, prior_writer = baseline.get(scope, (None, None))
        # Supersession must advance an established source scope. A 'published'
        # phase label on its own cannot establish that the old policy is stale.
        authority = stage == "baseline" or (prior is not None and published and (
            versions[scope] == prior.retrieval_version if scope == "delivery" else
            _advanced(versions[scope], prior.retrieval_version)))
        dependencies_ok = (record is not None and _bound(record, run_id, scope)
                           and record.markers == (MARKERS.get(body),) and row.get("sources") == list(record.markers))
        current = dependencies_ok and (stage == "baseline" or (authority and
                  (record.retrieval_version, record.access_version) == (versions[scope], prior.access_version)))
        visible = stage == "baseline" or matched_late_capture()
        if stage in ("released", "refilled"):
            visible = visible and rejected
        if stage == "refilled":
            visible = (visible and committed and fresh is not None and type(row.get("commit")) is int
                       and row["commit"] >= trace["new_receipt"]["commit"])
            if scope == "refund":
                visible = visible and writer == refill and record == fresh
        stale = None
        if located and authority:
            if body is not None and body != expected:
                stale = True
            elif (scope == "refund" and stage != "baseline" and isinstance(row.get("sources"), list)
                  and PRIVATE in row["sources"]):
                stale = True
            elif current and visible:
                stale = False
        stats.observe_request(hit=hit, stale=stale, generation_skipped=skipped)
        lines.append(f"{name} · HTTP {row.get('http_status')} · {body} · cache "
                     + {True: "hit", False: "miss", None: "unknown"}[hit])
        if stale:
            failures.append(f"{name} served obsolete content or sources. Expected {expected}.")
        if stale is None or not current or hit is None or skipped is None or not visible:
            gaps.append(name + " lacks confirmed response, source, scope, or write visibility")
            continue
        if stage == "baseline":
            if hit and skipped:
                baseline[scope] = (record, writer)
            else:
                gaps.append(name + " did not establish a warm cache entry that bypassed generation")
        elif scope == "delivery":
            if not hit or not skipped or record != prior or writer != prior_writer:
                failures.append(name + " did not reuse the original unaffected cached answer.")
        elif stage == "refilled":
            if not hit or not skipped or writer != refill or record != fresh:
                gaps.append("the refund answer did not witness the completed refill")
        elif hit and not stale:
            gaps.append(name + " did not exercise regeneration of the invalidated refund answer")
    late = matched_late_capture()
    if rejected and late:
        stats.rejected_fill()
    if (len(trace["reads"]) != 8 or set(baseline) != set(QUERIES)
            or not (late and rejected and committed and fresh is not None)):
        gaps.append("missing matched baseline, held input, supersession, rejection, refill or final reads")
    if failures:
        verdict = Verdict(Status.FAIL, failures[0])
    elif trace["error"] is not None:
        verdict = Verdict(Status.ERROR, "Application could not complete the selective drill.")
    elif gaps:
        verdict = Verdict(Status.NOT_TESTED, gaps[0])
    else:
        verdict = Verdict(Status.PASS, "The refund answer refreshed after supersession. Delivery kept its original cache hit.")
    return verdict, "\n".join([verdict.status.label + "  " + verdict.detail, *lines]), stats


def replay(directory):
    verdict, summary, _ = _assess(_load(directory))
    return verdict, summary


def read_metrics(directory):
    trace = _load(directory)
    return trace["run_id"], _assess(trace)[2]
