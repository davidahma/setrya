"""Check caller-visible search history across deliveries in one scenario."""

from dataclasses import dataclass

from .checker import NS_PER_MS, Status, Verdict
from .drill import InputError, integer, label, mapping
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


@dataclass(frozen=True)
class OrderedRead:
    writer: int
    reader: int
    phase: str
    probe: str
    ordinal: int
    change: int
    event: int
    failure: Verdict | None
    proof: str | None

    @property
    def key(self):
        return self.writer, self.reader, self.phase, self.probe, self.ordinal


def _event(report, kind):
    return next((e for e in report.events if e["kind"] == kind), None)


def terminal_receipt(report):
    receipt = _event(report, "receipt")
    release = _event(report, "released")
    if receipt and release and (
        (receipt["run_id"], receipt["attempt"], receipt["document"], receipt["revision"])
        == (report.run_id, release["attempt"], report.rules.document, report.rules.old_revision)
        and receipt["ns"] - release["ns"] <= report.rules.settle_ms * NS_PER_MS
    ):
        return receipt
    return None


def _visible(event, write):
    view = event["view"]
    return (view is not None and write is not None and write["commit"] is not None
            and view["generation"] == write["generation"] and view["commit"] >= write["commit"])


def _missing_after_write(event, probe, receipt):
    if (not matches_search(event, probe) or None in event["passages"] or receipt is None
            or (receipt["outcome"] != "rejected" and not _visible(event, receipt))):
        return ()
    return tuple(marker for marker in probe.require if marker not in event["passages"])


def _clean(event, probe):
    return (matches_search(event, probe) and None not in event["passages"] and observed_change(event, probe)
            and not set(probe.forbid).intersection(event["passages"]))


def validate_publication_write(position, run_id, rules):
    if position is None:
        return
    mapping(position, "publication position", {"run_id", "document", "revision", "generation", "commit"})
    if (position["run_id"], position["document"], position["revision"]) != (run_id, rules.document, rules.new_revision):
        raise InputError("publication position belongs to a different mutation")
    label(position["generation"], "publication generation")
    integer(position["commit"], "publication position", 0, 2**64 - 1)


def validate_windows(reports, order, starts, publication_write):
    """Validate the shared clock against release *invocation*, including synchronous hooks."""
    first = reports[order[0]]
    validate_publication_write(publication_write, first.run_id, first.rules)
    if (not isinstance(starts, list) or len(starts) > len(order)
            or any(type(t) is not int or not 0 <= t < 2**63 for t in starts)
            or any(a >= b for a, b in zip(starts, starts[1:]))):
        raise InputError("invalid release start times")
    for turn, lane in enumerate(order):
        report = reports[lane]
        release = _event(report, "released")
        if release and (turn >= len(starts) or release["ns"] < starts[turn]):
            raise InputError("release completion precedes its invocation")
        if turn < len(starts):
            published = _event(report, "published")
            if not published or published["ns"] > starts[0]:
                raise InputError("release started before publication")
        if turn and turn < len(starts):
            prior = _event(reports[order[turn - 1]], "receipt")
            if not prior or prior["ns"] > starts[turn]:
                return Verdict(Status.NOT_TESTED, "Missing terminal receipt before the next release")
    if not starts or len(order) < 2:
        return None
    reader = order[1]
    rules = first.rules
    primary = next(p for p in rules.probes if p.id == rules.freshness_probe)
    receipt = terminal_receipt(first)
    interval = []
    for lane, report in reports.items():
        initial = []
        for event in report.events:
            if event.get("stage") != "freshness":
                continue
            if event["ns"] <= starts[0]:
                initial.append(event)
                continue
            if (lane != reader or event["started_ns"] < starts[0]
                    or (len(starts) > 1 and event["ns"] > starts[1])):
                raise InputError("search overlaps an unexpected release boundary")
            interval.append(event)
        change = next((e for e in initial if e["probe_id"] == primary.id and matches_search(e, primary)
                       and observed_change(e, primary)), None)
        if change is None:
            return Verdict(Status.NOT_TESTED, "Missing change observation on a held delivery before the first release")
        if primary.absence and any(not any(
            e["seq"] > change["seq"] and e["probe_id"] == p.id and _clean(e, p) for e in initial
        ) for p in rules.probes if p.id != primary.id):
            return Verdict(Status.NOT_TESTED, "Missing authorized control before the first release")
    if len(starts) < 2:
        return None
    if (receipt is None or [e["probe_id"] for e in interval] != [p.id for p in rules.probes]
            or any(not receipt["ns"] < e["started_ns"] <= e["ns"] < starts[1] for e in interval)):
        return Verdict(Status.NOT_TESTED, "Missing same-window reads after the first receipt and before the next release")
    for e, p in zip(interval, rules.probes):
        forbidden = matches_search(e, p) and set(p.forbid).intersection(e["passages"])
        if not (forbidden or _clean(e, p) or _missing_after_write(e, p, receipt)):
            return Verdict(Status.NOT_TESTED, "Missing caller or control observation between worker releases")
    return None


def ordered_reads(reports, order, starts, publication_write):
    """Identify the exact query slot, worker positions, and evidence available in that slot."""
    if len(reports) < 2:
        return ()
    rules = reports[0].rules
    probes = {p.id: p for p in rules.probes}
    primary = probes[rules.freshness_probe]
    held = {i: [e for e in _post_publication_reads(r, primary) if e["stage"] == "freshness"]
            for i, r in reports.items()}
    changes = {}
    for lane, reads in held.items():
        for e in reads:
            if (e["probe_id"] == primary.id and matches_search(e, primary) and observed_change(e, primary)
                    and (not starts or e["ns"] <= starts[0])):
                changes[lane] = e
                break
    if not changes:
        return ()
    source, change = min(changes.items(), key=lambda pair: (pair[1]["ns"], pair[0]))
    observations = []
    for reader, events in held.items():
        own_transition = changes.get(reader)
        counts = {}
        for event in events:
            probe = probes[event["probe_id"]]
            between = starts and event["started_ns"] > starts[0]
            phase = "between releases" if between else "both held"
            key = (phase, probe.id)
            counts[key] = counts.get(key, 0) + 1
            writer, anchor = source, change
            proof = None
            if between:
                if reader != order[1] or len(starts) < 2 or event["ns"] >= starts[1]:
                    continue
                writer = order[0]
                anchor = changes.get(writer)
                receipt = terminal_receipt(reports[writer])
                if not anchor or not receipt or receipt["ns"] >= event["started_ns"]:
                    continue
                if receipt["outcome"] == "rejected":
                    proof = f"delivery {writer} rejected its old write at event {receipt['seq']}"
                elif _visible(event, receipt):
                    proof = f"view covers delivery {writer}'s committed write at event {receipt['seq']}"
            else:
                if (reader == writer or event["started_ns"] <= anchor["ns"]
                        or (reader in changes and event["seq"] > changes[reader]["seq"])):
                    continue
                if starts and event["ns"] < starts[0]:
                    if probe == primary and not probe.absence and set(probe.require) - set(probe.before):
                        proof = "the named replacement was visible while both workers were held"
                    elif _visible(event, publication_write):
                        proof = "view covers the publication while both workers were held"
            if not matches_search(event, probe):
                continue
            violation = None
            for rank, marker in enumerate(event["passages"], 1):
                if (marker in probe.forbid and (between or own_transition is None
                                                or event["seq"] < own_transition["seq"])):
                    violation = Verdict(Status.FAIL,
                                        f"Delivery {reader} event {event['seq']} returned forbidden {marker} at rank {rank} "
                                        f"after delivery {writer} event {anchor['seq']} observed the change. "
                                        f"Route {probe.route}, caller {probe.scope}.",
                                        event["seq"], probe.id)
                    break
            if between and violation is None:
                missing = _missing_after_write(event, probe, receipt)
                if missing:
                    violation = Verdict(Status.FAIL,
                                        f"Delivery {reader} event {event['seq']} lacked required {', '.join(missing)} "
                                        f"after delivery {writer}'s old write resolved and before delivery {reader}'s release. "
                                        f"Route {probe.route}, caller {probe.scope}.", event["seq"], probe.id)
            if not _clean(event, probe):
                proof = None
            observations.append(OrderedRead(writer, reader, phase, probe.id, counts[key], anchor["seq"],
                                            event["seq"], violation, proof))
    return tuple(observations)


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
