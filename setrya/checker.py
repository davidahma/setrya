"""Evaluate recorded observations. This module has no fixture or adapter imports."""

from dataclasses import dataclass
from enum import Enum

from .evidence import Report, establishes_baseline, matches_search

NS_PER_MS = 1_000_000


class Status(Enum):
    PASS = ("PASS", 0)
    FAIL = ("FAIL", 1)
    ERROR = ("ERROR", 2)
    NOT_TESTED = ("NOT TESTED", 3)

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def exit_code(self) -> int:
        return self.value[1]


@dataclass(frozen=True)
class Verdict:
    status: Status
    detail: str
    event: int | None = None
    probe: str | None = None
    attribution: str | None = None
    confirmed: tuple[int, ...] = ()


def _matches(event: dict | None, identity: dict) -> bool:
    return event is not None and all(event[key] == value for key, value in identity.items())


def _visible(view: dict | None, receipt: dict, probe) -> bool:
    if view is None:
        return False
    return (
        view["generation"] == receipt["generation"] and view["commit"] >= receipt["commit"]
        and view["route"] == probe.route and view["scope"] == probe.scope
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
    events = {event["kind"]: event for event in report.events if event["kind"] != "probe"}
    attempt = events.get("started", {}).get("attempt")
    identity = {"run_id": report.run_id, "attempt": attempt, "document": rules.document,
                "revision": rules.old_revision}
    held = events.get("held")
    if not _matches(held, {**identity, "name": rules.checkpoint}):
        return Verdict(Status.NOT_TESTED, f"Missing checkpoint acknowledgment for {rules.checkpoint} and the old revision")
    if report.schema >= 4 and held["ns"] - events["started"]["ns"] > rules.checkpoint_ms * NS_PER_MS:
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
    first_fresh = None
    last_pre_deadline = None
    saw_freshness_probe = False
    baseline = set()
    final = {}
    proof = set()
    final_search = set()
    missing_identity = set()
    cutoff = publication["ns"] + rules.freshness_ms * NS_PER_MS
    final_cutoff = release["ns"] + rules.settle_ms * NS_PER_MS if release else None

    for event in report.events:
        if event["kind"] != "probe":
            continue
        probe = probes[event["probe_id"]]
        if not matches_search(event, probe):
            continue
        passages = event["passages"]
        has_identity = None not in passages
        required = set(probe.require) <= set(passages)
        stage = event["stage"]
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
                return Verdict(Status.FAIL, f"Expected {', '.join(probe.require)}. Observed forbidden {marker} at rank {rank}.",
                               event["seq"], probe.id, _attribution(event, rank, release, receipt, probe))

        if first_fresh is None or stage != "final" or not release_valid:
            continue
        if event["ns"] <= final_cutoff:
            final_search.add(probe.id)
        rejected = receipt and receipt["outcome"] == "rejected" and event["started_ns"] >= receipt["ns"]
        visible = receipt and receipt["outcome"] == "committed" and _visible(event["view"], receipt, probe)
        if not (rejected or visible) or event["ns"] > final_cutoff:
            continue
        proof.add(probe.id)
        if not has_identity:
            missing_identity.add(probe.id)
            continue
        if not required:
            missing = ", ".join(marker for marker in probe.require if marker not in passages)
            return Verdict(Status.FAIL, f"Required passage {missing} was absent after the write was resolved",
                           event["seq"], probe.id)
        final[probe.id] = event

    if not saw_freshness_probe:
        return Verdict(Status.NOT_TESTED, "Missing freshness observation on the declared route and caller before release",
                       probe=primary.id)
    if first_fresh is None or first_fresh["ns"] > cutoff:
        return Verdict(Status.NOT_TESTED, "Freshness deadline could not be evaluated from the sampled search responses",
                       probe=rules.freshness_probe)
    missing_baseline = next((probe.id for probe in rules.probes if probe.before and probe.id not in baseline), None)
    if missing_baseline:
        return Verdict(Status.NOT_TESTED, "Baseline search did not establish the original passage", probe=missing_baseline)
    if not release_valid:
        return Verdict(Status.NOT_TESTED, "Missing matching release of the held worker")
    if receipt is None:
        return Verdict(Status.NOT_TESTED, "Missing terminal receipt for the exact old write", probe=primary.id)
    for probe in (primary, *(probe for probe in rules.probes if probe.id != primary.id)):
        if probe.id in final:
            continue
        if probe.id in missing_identity:
            return Verdict(Status.NOT_TESTED, "Final result lacked revision or passage identity", probe=probe.id)
        if probe.id not in final_search:
            return Verdict(Status.NOT_TESTED, "Missing final search observation on the declared route and caller", probe=probe.id)
        if probe.id not in proof:
            detail = ("Missing search visibility evidence for the old write" if receipt["outcome"] == "committed"
                      else "Missing a final search observation after the old write rejection")
            return Verdict(Status.NOT_TESTED, detail, probe=probe.id)
    decisive = final[primary.id]
    resolved = "Index rejected the old write" if receipt["outcome"] == "rejected" else "Search observed the completed write"
    return Verdict(Status.PASS, f"{resolved}. All final probes found their required passages.",
                   decisive["seq"], decisive["probe_id"], confirmed=tuple(event["seq"] for event in final.values()))
