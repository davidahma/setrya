"""Recompute the synthetic late-fill decision without loading the application."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import re
from pathlib import Path

from setrya.adapter import ReadView, SearchResult
from setrya.checker import Status, Verdict, evaluate
from setrya.drill import Drill, InputError, Probe, ProbeRules, Rules, label
from setrya.evidence import Report, bind_inputs
from setrya.offboarding.inputs import InvalidReport, read_json

from .contract import CHECKPOINT, PRIVATE, PRIVATE_TEXT, PUBLIC, PUBLIC_TEXT, QUESTION

TEST_ID = "celery-late-answer-fill"
FORMAT = "setrya.answer-cache"
VERSION = 3
QUESTION_DIGEST = sha256(QUESTION.encode()).hexdigest()
MANIFEST = "answer-cache.json"
JOURNAL = "run.json"
PRIVATE_ANSWER = "private-answer"
PUBLIC_ANSWER = "public-answer"
PROTECTED = (PRIVATE_ANSWER, PRIVATE)
CONTROL = (PUBLIC_ANSWER, PUBLIC)
BODIES = ("private-and-public", "public-only", "contains-private", "unknown")


def drill():
    probes = (
        ProbeRules("revoked-answer", "answer", "revoked", 8, PROTECTED, (), PROTECTED, True),
        ProbeRules("public-answer", "answer", "revoked", 8, (*CONTROL, "cache-hit"), CONTROL, PROTECTED),
        ProbeRules("retained-answer", "answer", "retained", 8, (*PROTECTED, "cache-hit"),
                   (*PROTECTED, "cache-hit"), ()),
    )
    rules = Rules("late-answer-fill", "policy", "granted", "revoked", CHECKPOINT, "revoked-answer",
                  10000, 60000, 30000, 100, 3000, probes)
    return Drill(rules, tuple(Probe(p, QUESTION) for p in probes), None, {})


def _label_or_none(value):
    try:
        return label(value, "answer observation")
    except InputError:
        return None


def _body(text):
    if text == PRIVATE_TEXT + " " + PUBLIC_TEXT:
        return "private-and-public"
    if text == PUBLIC_TEXT:
        return "public-only"
    if isinstance(text, str) and PRIVATE_TEXT in text:
        return "contains-private"
    return "unknown"


def _sources(value):
    if not isinstance(value, (tuple, list)) or len(value) > 2:
        return [None]
    return [s if s in (PUBLIC, PRIVATE) else None for s in value]


def worker_capture(captured):
    if not isinstance(captured, dict):
        return None
    question = captured.get("question")
    return {
        **{k: _label_or_none(captured.get(k)) for k in ("run_id", "attempt", "caller", "revision")},
        "query_sha256": sha256(question.encode()).hexdigest() if isinstance(question, str) else None,
        "body": _body(captured.get("answer")), "sources": _sources(captured.get("sources")),
    }


def verified_capture(captured, run_id, attempt):
    return captured == {"run_id": run_id, "attempt": attempt, "caller": "revoked", "revision": "granted",
                        "query_sha256": QUESTION_DIGEST, "body": "private-and-public",
                        "sources": [PRIVATE, PUBLIC]}


def capture_digest(captured):
    if captured is None:
        return None
    return sha256(json.dumps(captured, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sample(probe_id, stage, response, captured=None):
    """Retain only synthetic answer classifications and whitelisted source markers."""
    response = response if isinstance(response, dict) else {}
    hit, status, position = (response.get(k) for k in ("cache_hit", "http_status", "commit"))
    query = response.get("query_sha256")
    sources = response.get("sources")
    # Preserve explicit exposure even when a response exceeds the two-source
    # fixture shape. Record its one-based position without copying a long list.
    private_rank = None
    if isinstance(sources, (tuple, list)) and len(sources) > 2 and PRIVATE in sources:
        private_rank = sources.index(PRIVATE) + 1
    return {
        "overflow_private_rank": private_rank,
        "query_sha256": query if isinstance(query, str) and re.fullmatch("[0-9a-f]{64}", query) else None,
        "probe": probe_id, "stage": stage, "route": _label_or_none(response.get("route")),
        "caller": _label_or_none(response.get("caller")), "status": status if type(status) is int else None,
        "capture_sha256": capture_digest(captured),
        "body": _body(response.get("answer")), "sources": _sources(response.get("sources")), "cache_hit": hit if type(hit) is bool else None,
        "generation_skipped": (response["generation_skipped"]
                               if type(response.get("generation_skipped")) is bool else None),
        "cache_writer": _label_or_none(response.get("cache_writer")),
        "generation": _label_or_none(response.get("generation")),
        "commit": position if type(position) is int and 0 <= position < 2**64 else None,
    }


def matches_question(row):
    # v1 and v2 are replayed under their original request contract.
    return "query_sha256" not in row or row["query_sha256"] == QUESTION_DIGEST


def observation(row):
    """Encode the observed answer and its ranked sources into one Recorder read."""
    expected = {"capture_sha256", "probe", "stage", "route", "caller", "status", "body", "sources",
                 "cache_hit", "cache_writer", "generation", "commit"}
    if isinstance(row, dict) and "query_sha256" in row:
        expected.update(("query_sha256", "overflow_private_rank"))
        rank = row.get("overflow_private_rank")
        if rank is not None and (type(rank) is not int or rank < 1 or row.get("sources") != [None]):
            raise InputError("invalid forbidden source position")
        query = row["query_sha256"]
        if query is not None and (not isinstance(query, str) or not re.fullmatch("[0-9a-f]{64}", query)):
            raise InputError("invalid answer query fingerprint")
    if isinstance(row, dict) and "generation_skipped" in row:
        expected.add("generation_skipped")
        if row["generation_skipped"] is not None and type(row["generation_skipped"]) is not bool:
            raise InputError("invalid generation observation")
    if (not isinstance(row, dict) or set(row) != expected or row["body"] not in BODIES
            or not isinstance(row["sources"], list) or len(row["sources"]) > 2
            or any(s not in (None, PRIVATE, PUBLIC) for s in row["sources"])
            or (row["status"] is not None and type(row["status"]) is not int)
            or (row["commit"] is not None and (type(row["commit"]) is not int or not 0 <= row["commit"] < 2**64))
            or (row["cache_hit"] is not None and type(row["cache_hit"]) is not bool)
            or any(row[k] is not None and _label_or_none(row[k]) is None
                   for k in ("route", "caller", "cache_writer", "generation"))):
        raise InputError("invalid answer endpoint observation")
    digest = sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if not matches_question(row):
        return SearchResult((None,), origins=("sample-" + digest,), route=row["route"], scope=row["caller"])
    markers = []
    view = None
    if row["status"] == 200:
        markers.extend(row["sources"])
        if row.get("overflow_private_rank") is not None:
            markers.append(PRIVATE)
        if row["body"] in ("private-and-public", "contains-private"):
            markers.append(PRIVATE_ANSWER)
        if row["body"] in ("private-and-public", "public-only"):
            markers.append(PUBLIC_ANSWER)
        sources = {"private-and-public": [PRIVATE, PUBLIC], "public-only": [PUBLIC]}
        if (row["sources"] != sources.get(row["body"]) or row["cache_hit"] is None
                or row["cache_hit"] != (row["cache_writer"] is not None)):
            markers.append(None)
        if all(row[k] is not None for k in ("generation", "commit", "route", "caller")):
            view = ReadView(row["generation"], row["commit"], row["route"], row["caller"])
    else:
        markers.append(None)
    # The cache field's origin binds the lossless replay of the redacted HTTP row
    # to this event. The other origins identify the actual cache writer, if any.
    origins = [row["cache_writer"]] * len(markers)
    markers.append({True: "cache-hit", False: "cache-miss"}.get(row["cache_hit"]))
    origins.append("sample-" + digest)
    return SearchResult(tuple(markers), view=view, origins=tuple(origins), route=row["route"], scope=row["caller"])


def pending_before_release(manifest):
    # A v1 bundle is replayed under its published contract.
    return manifest["version"] == 1 or manifest["receipt_before_release"] is False


def _read_bundle(directory):
    directory = Path(directory)
    try:
        manifest, _ = read_json(directory / MANIFEST, "answer cache manifest")
    except InvalidReport as error:
        raise InputError(str(error)) from None
    keys = {"format", "version", "mode", "run_id", "report_sha256", "held_after_reads", "worker_capture", "observations"}
    if isinstance(manifest, dict) and manifest.get("version") in (2, VERSION):
        keys.add("receipt_before_release")
        before = manifest.get("receipt_before_release")
        if before is not None and type(before) is not bool:
            raise InputError("invalid pre-release terminal observation")
    if (not isinstance(manifest, dict) or set(manifest) != keys or manifest["format"] != FORMAT
            or type(manifest["version"]) is not int or manifest["version"] not in (1, 2, VERSION)
            or manifest["mode"] not in ("broken", "guarded") or not isinstance(manifest["observations"], list)
            or not isinstance(manifest["report_sha256"], str)):
        raise InputError("invalid answer cache manifest")
    report = Report.load(directory / JOURNAL, expected_sha256=manifest["report_sha256"])
    expected = drill()
    if (report.run_id != manifest["run_id"] or report.rules != expected.rules
            or report.inputs != bind_inputs(expected, test_id=TEST_ID)):
        raise InputError("answer cache trace does not match the declared run and drill")
    rows = manifest["observations"]
    probes = [e for e in report.events if e["kind"] == "probe"]
    if len(probes) != len(rows):
        raise InputError("answer trace count differs from the recording")
    started = next((e for e in report.events if e["kind"] == "started"), {})
    receipt = next((e for e in report.events if e["kind"] == "receipt"), {})
    attempt = started.get("attempt")
    captured = manifest["worker_capture"]
    verified = verified_capture(captured, report.run_id, attempt)
    lines = []
    private_from_task = set()
    if verified:
        lines.append(f"Task {attempt} captured the private answer for the revoked user before release.")
    for event, row in zip(probes, rows):
        if (not isinstance(row, dict) or ("generation_skipped" in row) != (manifest["version"] >= 2)
                or ("query_sha256" in row) != (manifest["version"] >= 3)):
            raise InputError("answer response fields differ from the manifest version")
        observed = observation(row)
        expected_capture = None if event["stage"] == "baseline" else capture_digest(captured)
        if (expected_capture != row["capture_sha256"] or (event["stage"], event["probe_id"], event["route"], event["scope"]) !=
                (row["stage"], row["probe"], observed.route, observed.scope)
                or list(event["passages"]) != list(observed.passages) or list(event["origins"]) != list(observed.origins)
                or event["view"] != (asdict(observed.view) if observed.view else None)):
            raise InputError("answer endpoint observation does not match its recorded event")
        cache = {True: "hit", False: "miss"}.get(row["cache_hit"], "unknown")
        origin = "" if matches_question(row) else " · answer question unverified"
        if (row["stage"] == "final" and row["cache_hit"] and attempt and row["cache_writer"] == attempt
                and pending_before_release(manifest) and receipt.get("outcome") == "committed" and receipt["attempt"] == attempt
                and receipt["run_id"] == report.run_id and event["seq"] > receipt["seq"] and observed.view
                and observed.view.generation == receipt["generation"] and receipt["commit"] is not None
                and observed.view.commit >= receipt["commit"]):
            origin = " · served the resumed task’s cached answer"
            if row["caller"] == "revoked" and row["body"] in ("private-and-public", "contains-private"):
                private_from_task.add(event["seq"])
        lines.append(f"Event {event['seq']} · {row['stage']} · {row['caller']} · "
                     f"HTTP {row['status']} · cache {cache} · {row['body']}{origin}")
    verdict = evaluate(report)
    if verdict.status is Status.FAIL:
        for event, row in zip(probes, rows):
            if event["seq"] == verdict.event and row.get("overflow_private_rank") is not None and row["caller"] == "revoked":
                verdict = replace(verdict, detail="The revoked caller's answer listed the forbidden source at position "
                                  f"{row['overflow_private_rank']}.")
                break
    if verdict.status is Status.PASS:
        held = next(e for e in report.events if e["kind"] == "held")
        if manifest["held_after_reads"] != {k: held[k] for k in ("run_id", "attempt", "document", "revision", "name")}:
            verdict = Verdict(Status.NOT_TESTED, "Missing confirmation that the captured task stayed held across both callers' reads.")
        if not verified:
            verdict = Verdict(Status.NOT_TESTED, "The old task did not demonstrate a private answer captured for the revoked caller.")
        required = {p.rules.id: p.rules for p in expected.probes}
        for stage in ("baseline", "freshness", "final"):
            reads = {r["probe"]: r for r in rows if r["stage"] == stage}
            if (reads.keys() != required.keys() or any(
                    r["status"] != 200 or None in observation(r).passages or
                    (r["route"], r["caller"]) != (required[k].route, required[k].scope) for k, r in reads.items())):
                verdict = Verdict(Status.NOT_TESTED, "Missing an identified HTTP answer for both callers at " + stage)
                break
    if verdict.status in (Status.PASS, Status.NOT_TESTED) and not pending_before_release(manifest):
        if manifest["receipt_before_release"] is True:
            verdict = Verdict(Status.NOT_TESTED, "The selected task already had a terminal receipt before release.")
        elif verdict.status is Status.PASS:
            verdict = Verdict(Status.NOT_TESTED, "No observation confirmed the selected task was still pending before release.")
    if verdict.status is Status.PASS:
        worker = "rejected the late fill" if receipt["outcome"] == "rejected" else "committed its late fill"
        verdict = replace(verdict, detail=f"The worker {worker}. The revoked user received the public answer "
                          "and the retained user received a private cache hit.")
    elif verdict.status is Status.FAIL and verdict.event in private_from_task:
        verdict = replace(verdict, detail=f"At event {verdict.event} the revoked user received the private answer "
                          f"from the resumed task's cache write ({attempt}).")
    summary = "\n".join([verdict.status.label + "  " + verdict.detail, *lines])
    return verdict, summary, report, manifest


def replay(directory):
    verdict, summary, _, _ = _read_bundle(directory)
    return verdict, summary
