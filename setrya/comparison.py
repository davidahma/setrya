"""Compare two saved drills without contacting either application."""

from .checker import Status, Verdict, evaluate
from .drill import ProbeRules
from .evidence import Report, matches_search, observed_change


def _difference(before: Report, after: Report) -> str | None:
    if before.inputs is None or after.inputs is None:
        return "Both runs need schema 3 drill input fingerprints. Run both checks again."
    if before.run_id == after.run_id:
        return "Supply reports from two distinct runs."
    if before.inputs["drill_version"] != after.inputs["drill_version"]:
        return "Drill input versions differ."
    if before.inputs["adapter"] != after.inputs["adapter"]:
        return "The adapter factory differs."
    earlier = {probe.id: probe for probe in before.rules.probes}
    later = {probe.id: probe for probe in after.rules.probes}
    if earlier.keys() != later.keys():
        return "The reports cover different probes."
    for probe_id, probe in earlier.items():
        changed = later[probe_id]
        if before.inputs["questions"][probe_id] != after.inputs["questions"][probe_id]:
            return f"Question changed for {probe_id}."
        if probe.top_k != changed.top_k:
            return f"Search depth changed for {probe_id} from {probe.top_k} to {changed.top_k}."
        if (probe.route, probe.scope) != (changed.route, changed.scope):
            return f"Route or caller changed for {probe_id}."
        if probe != changed:
            return f"Passage expectations changed for {probe_id}."
    if before.rules != after.rules:
        return "Revision, schedule, drill identity, or probe order changed."
    return None


def _event(report: Report, verdict: Verdict) -> dict | None:
    if verdict.event is not None:
        return report.events[verdict.event - 1]
    return None


def _matching_proof(report: Report, verdict: Verdict, probe: ProbeRules, failed: dict) -> dict | None:
    if verdict.status is not Status.PASS:
        return None
    if failed["stage"] == "final":
        return next((report.events[seq - 1] for seq in verdict.confirmed
                     if report.events[seq - 1]["probe_id"] == probe.id), None)

    primary = next(item for item in report.rules.probes if item.id == report.rules.freshness_probe)
    fresh = next(event for event in report.events if event.get("stage") == "freshness"
                 and event["probe_id"] == primary.id and matches_search(event, primary)
                 and observed_change(event, primary))
    return next((event for event in report.events
                 if event.get("stage") == "freshness" and event["probe_id"] == probe.id
                 and event["seq"] >= fresh["seq"] and matches_search(event, probe)
                 and None not in event["passages"] and set(probe.require) <= set(event["passages"])
                 and not set(probe.forbid).intersection(event["passages"])), None)


def _describe(label: str, verdict: Verdict, event: dict | None) -> str:
    if event is None or event["kind"] != "probe":
        return f"{label} {verdict.status.label} · {verdict.detail}"
    passages = ", ".join(marker or "?" for marker in event["passages"]) or "(empty)"
    return (f"{label} {verdict.status.label} · event {event['seq']} · {event['probe_id']} · {event['stage']} · {passages}\n"
            f"  {verdict.detail}")


def compare(before: Report, after: Report, *, allow_passing_pair: bool = False) -> tuple[int, str]:
    old, new = evaluate(before), evaluate(after)
    difference = _difference(before, after)
    if difference:
        return 3, f"INCOMPARABLE  {difference}"
    old_event, new_event = _event(before, old), _event(after, new)
    decisive = old if old.status is Status.FAIL and new.status is not Status.FAIL else new
    probe = next(item for item in before.rules.probes if item.id == (decisive.probe or before.rules.freshness_probe))
    if old.status is Status.FAIL and new.status is Status.PASS:
        new_event = _matching_proof(after, new, probe, old_event)
        headline = ("PASS  The affected probe passed at the matching phase after refresh. Write visibility is unverified."
                    if new.evidence_tier == "observed-after-refresh" else
                    "FIX VERIFIED  The affected probe passed at the matching phase.")
        state = (0, headline) if new_event else (
            3, "INCONCLUSIVE  The after report lacks a confirmed observation at the affected phase."
        )
    elif new.status is Status.FAIL:
        state = 1, "FAILURE OBSERVED  The after report records a failure."
        if old.status is Status.PASS:
            old_event = _matching_proof(before, old, probe, new_event)
            if old_event is None:
                state = 1, "FAILURE OBSERVED  The before report lacks a confirmed observation at the affected phase."
            elif (old.evidence_tier == "observed-after-refresh" and old_event["stage"] == "final"
                  and old_event["view"] is None):
                state = 1, ("FAILURE OBSERVED  The before probe passed after refresh. "
                            "Its visibility of the completed write is unverified.")
            else:
                state = 1, "REGRESSION  The affected probe passed before and failed after at the matching phase."
    elif new.status is Status.ERROR or old.status is Status.ERROR:
        state = 2, "ERROR  An execution error leaves the comparison unresolved."
    elif new.status is Status.NOT_TESTED or old.status is Status.NOT_TESTED:
        state = 3, "INCONCLUSIVE  An observation required to establish the fix is missing."
    elif allow_passing_pair:
        state = 0, "PASS  Both revisions met the same recorded search expectations."
    else:
        state = 3, "INCONCLUSIVE  Both reports passed. A failing before run is needed to establish a fix."

    code, headline = state
    query = before.inputs["questions"][probe.id][:12]
    return code, "\n".join([
        headline,
        f"Probe {probe.id} · query sha256 {query} · route {probe.route} · caller {probe.scope} · top {probe.top_k}",
        _describe("Before", old, old_event),
        _describe("After", new, new_event),
        f"Runs {before.run_id} → {after.run_id}",
    ])
