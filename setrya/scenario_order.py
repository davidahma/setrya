"""Check caller-visible search history across deliveries in one scenario."""

from .checker import Status, Verdict
from .evidence import Report, establishes_baseline, matches_search, observed_change


def _post_publication_reads(report, primary):
    """Yield scoped reads from a lane that witnessed the declared held-worker race."""
    rules = report.rules
    events = {e["kind"]: e for e in report.events if e["kind"] in {"started", "held", "published"}}
    if events.keys() != {"started", "held", "published"}:
        return
    start, hold, publication = events["started"], events["held"], events["published"]
    if (hold["run_id"], hold["attempt"], hold["document"], hold["revision"], hold["name"]) != (
        report.run_id, start["attempt"], rules.document, rules.old_revision, rules.checkpoint
    ):
        return
    if (hold["ns"] - start["ns"] > rules.checkpoint_ms * 1_000_000 or
            publication["publication"] != {"run_id": report.run_id, "document": rules.document,
                                           "revision": rules.new_revision}):
        return
    if not any(e.get("stage") == "baseline" and e["probe_id"] == primary.id
               and establishes_baseline(e, primary, freshness=True) for e in report.events):
        return
    for event in report.events:
        if event.get("stage") in ("freshness", "final"):
            yield event


def shared_transition_failure(reports: dict[int, Report]) -> Verdict | None:
    """Find a forbidden read in another delivery after the shared transition.

    Call after validating member digests, run identities, common rules and the
    schedule's publication and release order. Each lane uses the same clock.
    """
    if len(reports) < 2:
        return None
    rules = next(iter(reports.values())).rules
    probes = {p.id: p for p in rules.probes}
    primary = probes[rules.freshness_probe]
    reads = [(lane, e) for lane, report in reports.items() for e in _post_publication_reads(report, primary)
             if matches_search(e, probes[e["probe_id"]])]
    first_change = {}
    for lane, event in reads:
        if (lane not in first_change and event["stage"] == "freshness" and
                event["probe_id"] == primary.id and observed_change(event, primary)):
            first_change[lane] = event
    if not first_change:
        return None
    source, change = min(first_change.items(), key=lambda item: (item[1]["ns"], item[0]))
    for lane, event in sorted(reads, key=lambda item: (item[1]["ns"], item[0])):
        # Separate journals give no ordering for a query starting on the exact
        # clock tick at which the other delivery returned its response.
        if lane == source or event["started_ns"] <= change["ns"]:
            continue
        if lane in first_change and event["seq"] >= first_change[lane]["seq"]:
            continue  # The individual checker already considers these observations.
        probe = probes[event["probe_id"]]
        for rank, marker in enumerate(event["passages"], 1):
            if marker in probe.forbid:
                return Verdict(Status.FAIL,
                               f"Delivery {lane} event {event['seq']} returned forbidden {marker} at rank {rank} "
                               f"after delivery {source} event {change['seq']} observed the change. "
                               f"Route {probe.route}, caller {probe.scope}.",
                               event["seq"], probe.id)
    return None
