"""Evaluate recorded observations. This module has no fixture or adapter imports."""

from dataclasses import dataclass
from .status import Status

from .evidence import Report, establishes_baseline, matches_search, observed_change

NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class Verdict:
    status: Status
    detail: str
    event: int | None = None
    probe: str | None = None
    attribution: str | None = None
    confirmed: tuple[int, ...] = ()
    evidence_tier: str | None = None


def _matches(event: dict | None, identity: dict) -> bool:
    return event is not None and all(event[key] == value for key, value in identity.items())


def _visible(view: dict | None, receipt: dict, probe) -> bool:
    if view is None or receipt["commit"] is None:
        return False
    return (
        view["generation"] == receipt["generation"] and view["commit"] >= receipt["commit"]
        and view["route"] == probe.route and view["scope"] == probe.scope
    )


def _write_resolved(event: dict, receipt: dict | None, probe, refreshes) -> bool:
    if receipt is None:
        return False
    if receipt["outcome"] == "rejected":
        return event["seq"] > receipt["seq"] and event["started_ns"] >= receipt["ns"]
    refresh = refreshes.get((probe.route, probe.scope))
    return _visible(event["view"], receipt, probe) or (
        event["view"] is None and refresh is not None
        and event["seq"] > refresh["seq"] and event["started_ns"] >= refresh["ns"]
    )


def _attribution(event: dict, rank: int, release: dict | None, receipt: dict | None, probe) -> str:
    if release is None or event["seq"] < release["seq"]:
        return "Observed while the old worker was held"
    if (receipt and receipt["outcome"] == "committed" and event["origins"][rank - 1] == receipt["attempt"]
            and _visible(event["view"], receipt, probe)):
        return f"Returned passage traces to old attempt {receipt['attempt']}"
    return "The recorded search result establishes the violation. Its write origin is unverified."


def evaluate(report: Report) -> Verdict:
    report.validate()
    if report.schema == 1:
        return Verdict(Status.NOT_TESTED, "Report schema 1 lacks current publication and caller evidence. Rerun the drill.")
    verdict = _evaluate(report)
    if verdict.status is not Status.FAIL:
        error = next((event for event in report.events if event["kind"] == "adapter_error"), None)
        if error:
            detail = "The recorded test did not complete" if error["operation"] == "test" else "The adapter could not complete the drill"
            return Verdict(Status.ERROR, detail, error["seq"])
    return verdict


def _evaluate(report: Report) -> Verdict:
    rules = report.rules
    primary = next(probe for probe in rules.probes if probe.id == rules.freshness_probe)
    if not any(event.get("stage") == "baseline" and event.get("probe_id") == primary.id
               and establishes_baseline(event, primary, freshness=True) for event in report.events):
        return Verdict(Status.NOT_TESTED, "Baseline search did not distinguish the original revision from its replacement",
                       probe=primary.id)
    events = {event["kind"]: event for event in report.events if event["kind"] in {"started", "held", "published", "released", "receipt"}}
    attempt = events.get("started", {}).get("attempt")
    identity = {"run_id": report.run_id, "attempt": attempt, "document": rules.document,
                "revision": rules.old_revision}
    held = events.get("held")
    if not _matches(held, {**identity, "name": rules.checkpoint}):
        return Verdict(Status.NOT_TESTED, f"Missing checkpoint acknowledgment for {rules.checkpoint} and the old revision")
    if held["ns"] - events["started"]["ns"] > rules.checkpoint_ms * NS_PER_MS:
        return Verdict(Status.NOT_TESTED, "The checkpoint acknowledgment arrived after the declared deadline", held["seq"])
    release = events.get("released")
    release_valid = release is not None and release["attempt"] == attempt
    receipt = events.get("receipt")
    if not _matches(receipt, identity):
        receipt = None
    publication = events.get("published")
    expected_publication = {"run_id": report.run_id, "document": rules.document, "revision": rules.new_revision}
    if not publication or not _matches(publication["publication"], expected_publication):
        return Verdict(Status.NOT_TESTED, f"Missing matching publication of revision {rules.new_revision} for {rules.document}",
                       probe=primary.id)

    probes = {probe.id: probe for probe in rules.probes}
    refreshes = {(event["route"], event["scope"]): event for event in report.events
                 if event["kind"] == "refreshed" and rules.visibility == "observed-after-refresh"}
    first_fresh = None
    last_pre_deadline = None
    saw_freshness_probe = False
    baseline = set()
    latest_final = {}
    cutoff = publication["ns"] + rules.freshness_ms * NS_PER_MS
    final_cutoff = release["ns"] + rules.settle_ms * NS_PER_MS if release else None

    timely_receipt = receipt if release_valid and receipt and receipt["ns"] <= final_cutoff else None

    for event in report.events:
        if event["kind"] != "probe":
            continue
        probe = probes[event["probe_id"]]
        stage = event["stage"]
        in_final_window = stage == "final" and release_valid and event["ns"] <= final_cutoff
        if in_final_window and (event["route"], event["scope"]) == (probe.route, probe.scope):
            latest_final[probe.id] = event
        if not matches_search(event, probe):
            continue
        passages = event["passages"]
        has_identity = None not in passages
        required = observed_change(event, probe)
        if stage == "baseline":
            if establishes_baseline(event, probe, freshness=probe.id == primary.id):
                baseline.add(probe.id)
            continue

        if probe.id == primary.id and stage == "freshness" and first_fresh is None:
            saw_freshness_probe = True
            covered_boundary = last_pre_deadline is not None and (
                event["ns"] - last_pre_deadline <= rules.max_gap_ms * NS_PER_MS
            )
            observed_late = (required and event["ns"] > cutoff) or (
                not required and has_identity and event["started_ns"] >= cutoff
            )
            if observed_late and covered_boundary:
                return Verdict(Status.FAIL, f"New revision missed the {rules.freshness_ms} ms freshness deadline",
                               event["seq"], probe.id)
            if not required and has_identity and event["ns"] < cutoff:
                last_pre_deadline = event["ns"]
            if required:
                first_fresh = event

        if first_fresh and (event is first_fresh or event["started_ns"] >= first_fresh["ns"]):
            forbidden = next(((rank, marker) for rank, marker in enumerate(passages, 1)
                              if marker in probe.forbid), None)
            if forbidden:
                rank, marker = forbidden
                return Verdict(Status.FAIL, f"Expected {', '.join(probe.require) or 'absence'}. Observed forbidden {marker} at rank {rank}.",
                               event["seq"], probe.id, _attribution(event, rank, release, receipt, probe))

        if first_fresh and in_final_window and has_identity and not required and _write_resolved(event, timely_receipt, probe, refreshes):
            missing = ", ".join(marker for marker in probe.require if marker not in passages)
            return Verdict(Status.FAIL, f"Required passage {missing} was absent after the write was resolved",
                           event["seq"], probe.id)

    if not saw_freshness_probe:
        return Verdict(Status.NOT_TESTED, "Missing freshness observation on the declared route and caller before release",
                       probe=primary.id)
    if first_fresh is None or first_fresh["ns"] > cutoff:
        return Verdict(Status.NOT_TESTED, "Freshness deadline could not be evaluated from the sampled search responses",
                       probe=rules.freshness_probe)
    missing_baseline = next((probe.id for probe in rules.probes if probe.before and probe.id not in baseline), None)
    if missing_baseline:
        return Verdict(Status.NOT_TESTED, "Baseline search did not establish the original passage", probe=missing_baseline)
    if primary.absence:
        for probe in rules.probes:
            if probe == primary:
                continue
            checks = [event for event in report.events if event.get("stage") == "freshness"
                      and event.get("probe_id") == probe.id and event["seq"] > first_fresh["seq"]]
            if not checks or not matches_search(checks[-1], probe) or None in checks[-1]["passages"] or not observed_change(checks[-1], probe):
                return Verdict(Status.NOT_TESTED, "Missing retained control before releasing the old worker", probe=probe.id)
    if not release_valid:
        return Verdict(Status.NOT_TESTED, "Missing matching release of the held worker")
    if receipt is None:
        return Verdict(Status.NOT_TESTED, "Missing terminal receipt for the exact old write", probe=primary.id)
    if timely_receipt is None:
        return Verdict(Status.NOT_TESTED, "The terminal write receipt arrived after the settle deadline",
                       receipt["seq"], primary.id)
    for probe in (primary, *(probe for probe in rules.probes if probe.id != primary.id)):
        latest = latest_final.get(probe.id)
        if latest is None:
            return Verdict(Status.NOT_TESTED, "Missing final search observation on the declared route and caller", probe=probe.id)
        if not matches_search(latest, probe):
            return Verdict(Status.NOT_TESTED, "Final search view did not match the declared route and caller",
                           latest["seq"], probe.id)
        if not _write_resolved(latest, receipt, probe, refreshes):
            detail = ("Missing search visibility evidence for the old write" if receipt["outcome"] == "committed"
                      else "Missing a final search observation after the old write rejection")
            return Verdict(Status.NOT_TESTED, detail, latest["seq"], probe.id)
        if None in latest["passages"]:
            return Verdict(Status.NOT_TESTED, "Final result lacked revision or passage identity", latest["seq"], probe.id)
    decisive = latest_final[primary.id]
    resolved = "Index rejected the old write" if receipt["outcome"] == "rejected" else "Search observed the completed write"
    tier = "write-confirmed"
    if receipt["outcome"] == "committed" and any(
            not _visible(latest_final[p.id]["view"], receipt, p) for p in rules.probes):
        tier = "observed-after-refresh"
        resolved = "Observed after refresh (write visibility unverified)"
    return Verdict(Status.PASS, f"{resolved}. All final probes found their required passages.",
                   decisive["seq"], decisive["probe_id"], confirmed=tuple(event["seq"] for event in latest_final.values()),
                   evidence_tier=tier)
