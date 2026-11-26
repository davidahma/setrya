"""Compute technical and case-review decisions from one frozen evidence format."""
from collections import defaultdict, deque
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

from . import deployment, scan_scope
from .inputs import keys_match, read_regular_bytes, require, unique_fields
from .judges import check_observation, check_policy, coverage
from .journal_chain import check_summary as check_chain
from .journal import identifier, validate_details
from .model import check_callers, route_id
from .provenance import validate as validate_provenance
from .receipt_format import EVIDENCE, EXIT, MAX_BUNDLE_FILE_BYTES, RECEIPT, _hex, _timestamp
from .receipt_format import FORMAT_V3 as RECEIPT_FORMAT
from .subject import Subject, Turn

LEGACY_EVIDENCE_FORMAT = "setrya.erasure.evidence.v3"
FLAT_EVIDENCE_FORMAT = "setrya.erasure.evidence.v3.sampled"
GROUPED_V31_FORMAT = "setrya.erasure.evidence.v3.1"
EVIDENCE_FORMAT = "setrya.erasure.evidence.v3.2"
DEPLOYMENT_EVIDENCE_FORMAT = "setrya.erasure.evidence.v3.3"
GROUPED_EVIDENCE_FORMATS = (GROUPED_V31_FORMAT, EVIDENCE_FORMAT, DEPLOYMENT_EVIDENCE_FORMAT)
MAX_JOURNAL_OBSERVATIONS = 16384
MAX_MAINTENANCE_STEPS = 8


def _fields(row, fields):
    require(keys_match(row, fields.split()), "Unexpected shared-erasure fields.")


def _list(items, limit, name):
    require(isinstance(items, list) and len(items) <= limit, f"Invalid {name} list.")


def _unique_ids(items, limit, name):
    _list(items, limit, name)
    for item in items:
        identifier(item, name)
    require(len(items) == len(set(items)), f"Duplicate {name}.")


def check_spec(spec):
    from .control_references import validate_controls
    controls = isinstance(spec, dict) and "controls" in spec
    judged = isinstance(spec, dict) and "judges" in spec
    sampled = isinstance(spec, dict) and "sampling" in spec
    maintenance = isinstance(spec, dict) and "maintenance_steps" in spec
    _fields(spec, "conversation route callers questions artifacts scan_stores require_lineage" +
            (" judges" if judged else "") + (" sampling" if sampled else "") + (" controls" if controls else "") +
            (" maintenance_steps" if maintenance else ""))
    if controls:
        require(sampled, "Historical single-read cases do not carry control references.")
        validate_controls(spec["controls"])
    if judged:
        check_policy(spec["judges"], sampled)
    identifier(spec["conversation"], "conversation")
    route_id(spec["route"])
    check_callers(spec["callers"])
    questions = spec["questions"]
    require(isinstance(questions, list) and 2 <= len(questions) <= (16 if sampled else 2)
            and all(_hex(q, 64) for q in questions) and len(set(questions)) == len(questions), "Supply distinct target and control questions.")
    if sampled:
        plan = spec["sampling"]
        _fields(plan, "samples question_roles source")
        roles = plan["question_roles"]
        require(isinstance(roles, list) and len(roles) == len(questions) and roles[:2] == ["target", "control"]
                and all(role in ("target", "control") for role in roles)
                and max(roles.count("target"), roles.count("control")) <= 8, "Invalid question roles.")
        require(type(plan["samples"]) is int and 1 <= plan["samples"] <= 32, "Use one to 32 samples per question.")
        require(plan["source"] == ("answer-text" if judged else "application-signals"), "Wrong answer source.")
        reads = len(questions) * len(spec["callers"]) * plan["samples"]
        checks = sum(len(spec["judges"][role]) for role in ("target", "control")) if judged else 1
        require(reads <= 1024 and reads * checks <= 1536, "Sampling exceeds the per-stage evidence budget.")
    _unique_ids(spec["scan_stores"], 128, "scanner")
    require(type(spec["require_lineage"]) is bool, "Invalid lineage requirement.")
    seen = set()
    _list(spec["artifacts"], 512, "artifact")
    for a in spec["artifacts"]:
        _fields(a, "store key action")
        loc = identifier(a["store"], "store"), identifier(a["key"], "key")
        require(loc not in seen and a["action"] in ("delete", "rewrite", "preserve"), "Invalid artifact rule.")
        seen.add(loc)
    require(seen, "Declare at least one artifact to inspect.")
    if maintenance:
        steps = spec["maintenance_steps"]
        _unique_ids(steps, MAX_MAINTENANCE_STEPS, "maintenance step")
        require(sampled and 0 < len(steps) * len(seen) <= 256 and all(len(step) <= 96 for step in steps),
                "Use short maintenance step names and at most 256 maintenance reads.")


def check_external(items, spec, subject_ref):
    required = set(spec["scan_stores"]) | {a["store"] for a in spec["artifacts"]}
    seen = set()
    _list(items, 512, "external item")
    for item in items:
        _fields(item, "store key owner kind evidence follow_up subject_ref")
        require(item["subject_ref"] == subject_ref, "Wrong subject on external evidence.")
        require(item["kind"] in ("configuration", "attested", "retained"), "Invalid external disposition.")
        for field in ("store", "key", "owner", "evidence"):
            identifier(item[field], field)
        key = item["store"], item["key"]
        require(key not in seen and item["store"] not in required, "Required stores cannot be external exceptions.")
        seen.add(key)
        if item["follow_up"] is not None:
            value = item["follow_up"]
            require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value, "Invalid follow-up date.")
        require(item["kind"] != "retained" or item["follow_up"] is not None, "Retention needs a review date.")


def fact_flags(row):
    require(all(type(row[f]) in (bool, type(None)) for f in ("target_present", "control_present")),
            "Invalid observed fact flags.")


def check_stored(row, store, key):
    _fields(row, "store key exists revision target_present control_present")
    require(row["store"] == store and row["key"] == key and type(row["exists"]) is bool, "Wrong store observation.")
    fact_flags(row)
    if row["exists"]:
        identifier(row["revision"], "artifact revision")
    else:
        require(row["revision"] is None and row["target_present"] is not True and row["control_present"] is not True,
                "Absent artifact has content or a revision.")


def discovered_keys(spec, scans):
    known = {(a["store"], a["key"]) for a in spec["artifacts"]}
    return sorted((store, key) for store, hits in scans.items() for key in (hits or []) if (store, key) not in known)


def new_residual_keys(spec, baseline_scans, final_scans):
    return sorted(set(discovered_keys(spec, final_scans)) - set(discovered_keys(spec, baseline_scans)))


def answer_slots(spec):
    """Yield the declared caller, question digest, fact role and one-based sample ordinal."""
    plan = spec.get("sampling", {})
    roles = plan.get("question_roles", ("target", "control"))
    for caller in spec["callers"]:
        for ordinal in range(1, plan.get("samples", 1) + 1):
            for question, role in zip(spec["questions"], roles):
                yield caller, question, role, ordinal


def sample_positions(spec, *, grouped):
    """Index samples in execution order, including the durable evidence pointer."""
    questions = len(spec["questions"])
    count = spec.get("sampling", {}).get("samples", 1)
    for index, (_, _, _, ordinal) in enumerate(answer_slots(spec)):
        if grouped:
            slot = (index // (count * questions)) * questions + index % questions
            yield slot, ordinal - 1
        else:
            yield (index,)


def group_samples(stage, spec):
    if stage is None:
        return None
    count = spec["sampling"]["samples"]
    grouped = [[None] * count for _ in range(len(spec["callers"]) * len(spec["questions"]))]
    for (slot, index), row in zip(sample_positions(spec, grouped=True), stage["answers"], strict=True):
        grouped[slot][index] = row
    return {**stage, "answers": grouped}


def sample_rows(stage, spec, *, grouped):
    """Yield the evidence coordinates and observation in the original read order."""
    if grouped and stage is not None:
        answers = stage["answers"]
        count = spec["sampling"]["samples"]
        slots = len(spec["callers"]) * len(spec["questions"])
        require(isinstance(answers, list) and len(answers) == slots
                and all(isinstance(values, list) and len(values) == count for values in answers),
                "Missing grouped endpoint samples.")
    for coordinates in sample_positions(spec, grouped=grouped):
        row = stage["answers"] if stage is not None else None
        if stage is not None:
            for index in coordinates:
                row = row[index]
        yield coordinates, row


def answer_records(evidence, stage):
    return [row for _, row in sample_rows(evidence[stage], evidence["case"],
                                         grouped=evidence["format"] in GROUPED_EVIDENCE_FORMATS)]


def check_stage(stage, spec, baseline_scans=None, *, scoped=True):
    has_details = scoped and isinstance(stage, dict) and "scan_details" in stage
    _fields(stage, "stored answers scans discovered" + (" scan_details" if has_details else ""))
    require(isinstance(stage["stored"], list) and len(stage["stored"]) == len(spec["artifacts"]), "Missing store slots.")
    for row, artifact in zip(stage["stored"], spec["artifacts"]):
        if row is not None:
            check_stored(row, artifact["store"], artifact["key"])
    require(keys_match(stage["scans"], spec["scan_stores"]), "Wrong scanner slots.")
    if has_details:
        scan_scope.check_stage(stage["scan_details"], stage["scans"])
    for values in stage["scans"].values():
        if values is not None:
            _unique_ids(values, 512, "scanner key")
    discovered = discovered_keys(spec, stage["scans"] if baseline_scans is None else baseline_scans)
    require(isinstance(stage["discovered"], list) and len(stage["discovered"]) == len(discovered), "Wrong discovered key slots.")
    for row, (store, key) in zip(stage["discovered"], discovered):
        if row is not None:
            check_stored(row, store, key)
    slots = list(answer_slots(spec))
    require(isinstance(stage["answers"], list) and len(stage["answers"]) == len(slots),
            "Missing endpoint slots.")
    sampled = "sampling" in spec
    for (caller, question, _, ordinal), row in zip(slots, stage["answers"]):
        if row is not None:
            fields = "caller conversation route question target_present control_present"
            _fields(row, fields + (" judgments" if "judges" in spec else "") + (" sample" if sampled else ""))
            if sampled:
                require(type(row["sample"]) is int and row["sample"] == ordinal, "Wrong endpoint sample ordinal.")
            if "judges" in spec:
                check_observation(row, spec["judges"], sampled)
            require((row["caller"], row["conversation"], row["route"], row["question"]) ==
                    (caller, spec["conversation"], spec["route"], question), "Wrong endpoint observation.")
            fact_flags(row)


def ready_for_mode(mode, worker):
    return mode == "deletion" or bool(worker["held"] and worker["held"] == worker["checkpoint"])


def _unconfirmed_deletions(spec, stage):
    """A matching target can share a physical row with retained data."""
    for artifact, row in zip(spec["artifacts"], stage["stored"]):
        if artifact["action"] == "delete" and row is not None and row["control_present"] is not False:
            yield artifact["store"], artifact["key"]
    for loc, row in zip(discovered_keys(spec, stage["scans"]), stage["discovered"]):
        if row is not None and row["control_present"] is not False:
            yield loc


def baseline_issues(spec, stage):
    """Explain each missing precondition before any deletion or worker action."""
    for store, key in _unconfirmed_deletions(spec, stage):
        yield "inspect", store, key, f"{store}/{key}: confirm the retained fact is absent before deleting this key."
    for a, row in zip(spec["artifacts"], stage["stored"]):
        store, key = a["store"], a["key"]
        if row is None or not row["exists"]:
            yield "inspect", store, key, f"{store}/{key}: required key was missing or unreadable."
        elif a["action"] == "delete":
            if row["target_present"] is not True and key not in (stage["scans"].get(store) or []):
                yield "inspect", store, key, f"{store}/{key}: the target fact was not observed before deletion."
        elif (row["control_present"] is not True
              or row["target_present"] is not (a["action"] == "rewrite")):
            yield "inspect", store, key, f"{store}/{key}: initial facts do not match the {a['action']} expectation."
    for (caller, question, role, _), row in zip(answer_slots(spec), stage["answers"]):
        if row is None or row[role + "_present"] is not True:
            yield "read", caller, role, f"{caller}: missing a positive {role} answer for question {question[:12]}."
    for (store, key), row in zip(discovered_keys(spec, stage["scans"]), stage["discovered"]):
        if row is None or not row["exists"]:
            yield "inspect", store, key, f"{store}/{key}: scanner match was not confirmed by a direct read."
    for store, hits in stage["scans"].items():
        if not hits:
            yield "scan", store, None, f"{store}: scanner needs a positive marker baseline in the declared scope."


def baseline_ready(spec, stage):
    return next(baseline_issues(spec, stage), None) is None


def _ref(row):
    _fields(row, "store key revision")
    return tuple(identifier(row[f], f) for f in ("store", "key", "revision"))


def check_lineage(lineage):
    _fields(lineage, "roots derivations")
    _list(lineage["roots"], 512, "root")
    _list(lineage["derivations"], 2048, "derivation")
    roots = {_ref(ref) for ref in lineage["roots"]}
    require(len(roots) == len(lineage["roots"]), "Duplicate subject source version.")
    inputs = {}
    edge_budget = 4096
    children = defaultdict(set)
    for edge in lineage["derivations"]:
        _fields(edge, "artifact inputs")
        dest = _ref(edge["artifact"])
        _list(edge["inputs"], 256, "input")
        edge_budget -= len(edge["inputs"])
        require(edge_budget >= 0, "Provenance exceeds the edge budget.")
        parents = {_ref(row) for row in edge["inputs"]}
        require(parents and len(parents) == len(edge["inputs"]) and dest not in inputs,
                "Duplicate or empty versioned derivation.")
        inputs[dest] = parents
        for parent in parents:
            children[parent].add(dest)
    # A versioned derivation cannot have itself as an ancestor. Retained
    # co-parent versions need not descend from the erased subject.
    degree = {ref: sum(p in inputs for p in parents) for ref, parents in inputs.items()}
    queue = deque(ref for ref, count in degree.items() if count == 0)
    processed = 0
    while queue:
        ref = queue.popleft()
        processed += 1
        for child in children[ref]:
            degree[child] -= 1
            if degree[child] == 0:
                queue.append(child)
    require(processed == len(inputs), "Cycle in versioned provenance.")
    reached = set(roots)
    queue = deque(roots)
    while queue:
        for child in children[queue.popleft()]:
            if child not in reached:
                reached.add(child)
                queue.append(child)
    require(set(inputs) <= reached, "Unrelated derivation in subject lineage.")
    return reached


def _journal(e, declared, external, gaps):
    expected = e["expected_producers"]
    _unique_ids(expected, 256, "expected producer")
    coverage = e["coverage"]
    require(expected or coverage is None, "Unexpected distributed coverage.")
    if expected:
        if coverage is None:
            gaps.add("Missing durable producer coverage.")
        else:
            _fields(coverage, "run_id expected received missing unsealed")
            for field in ("expected", "received", "missing", "unsealed"):
                _unique_ids(coverage[field], 256, "producer")
            received = set(coverage["received"])
            unsealed = set(coverage["unsealed"])
            require(coverage["run_id"] == e["run"] and set(coverage["expected"]) == set(expected)
                    and set(coverage["missing"]) == set(expected) - received
                    and not (received & unsealed), "Invalid durable producer coverage.")
            for p in set(expected) - received:
                gaps.add(f"Missing durable journal for {p}.")
            for p in received - set(expected):
                gaps.add(f"Unexpected durable journal from {p}.")
            for p in unsealed - set(expected):
                gaps.add(f"Unexpected producer {p} has observations but no durable journal.")
    modern = e["format"] in GROUPED_EVIDENCE_FORMATS
    events = e["journal"]
    if modern:
        chain = e["journal_chain"]
        if chain is not None:
            require(events is not None, "Journal chain has no journal observations.")
            if expected:
                require(isinstance(chain, dict) and coverage is not None
                        and chain.keys() == set(coverage["received"]), "Wrong journal chain producers.")
                for item in chain.values():
                    if item is not None:
                        check_chain(item)
            else:
                check_chain(chain)
        require(not expected or events is None or chain is not None, "Missing producer chain evidence.")
    if events is None:
        if expected:
            gaps.add("Missing subject journal observations.")
        return
    _list(events, MAX_JOURNAL_OBSERVATIONS, "journal observation")
    for event in events:
        fields = {"store", "key", "source", "action", "classification", "observed_at", "details"}
        require(isinstance(event, dict) and fields <= event.keys(), "Invalid journal observation fields.")
        extra = {field: value for field, value in event.items() if field not in fields}
        if extra:
            turn = Turn.from_copy_fields(extra)
            require(modern and turn is not None and extra == turn.copy_fields(), "Invalid journal turn context.")
        for f in ("store", "key", "source"):
            identifier(event[f], f)
        _timestamp(event["observed_at"])
        Subject(e["subject"]["ref"], event["classification"])
        require(event["action"] in ("retrieved", "sent", "stored", "derived") and isinstance(event["details"], dict),
                "Invalid journal observation.")
        details = validate_details(event["details"])
        location = (event["store"], event["key"])
        if location not in declared:
            gaps.add(f"Journal location outside required and external checks: {location[0]}/{location[1]}.")
        if details.get("evidence_level") == "configuration" and location not in external:
            gaps.add(f"Provider observation needs its own external item: {location[0]}/{location[1]}.")


class Findings(set):
    """Collect original receipt messages and their audience-facing classifications."""

    def __init__(self):
        super().__init__()
        self.kinds = set()

    def add(self, message, *, kind="other"):
        super().add(message)
        self.kinds.add(kind)


def stored_outcome(r, a, before, name, failures, gaps):
    """Apply one declared store rule to an observation after erasure."""
    loc = f"{a['store']}/{a['key']}"
    if r is None:
        gaps.add(f"Missing direct read of {loc} at {name}.")
        return
    if a["action"] == "delete":
        if r["exists"]:
            failures.add(f"{loc} still exists at {name}.", kind="residual")
        return
    if not r["exists"] or r["control_present"] is False:
        failures.add(f"{loc} lost the retained finding at {name}.", kind="control-loss")
    elif r["control_present"] is None:
        gaps.add(f"Unobserved retained finding in {loc} at {name}.")
    if r["target_present"] is True:
        failures.add(f"{loc} contains the erased finding at {name}.", kind="residual")
    elif r["exists"] and r["target_present"] is None:
        gaps.add(f"Unobserved subject finding in {loc} at {name}.")
    if r["exists"] and a["action"] == "rewrite" and r["revision"] == before["revision"]:
        gaps.add(f"No distinct rewritten version of {loc} at {name}.")
    if r["exists"] and a["action"] == "preserve" and r["revision"] != before["revision"]:
        failures.add(f"Unrelated artifact {loc} changed at {name}.")


def answer_outcome(r, caller, control_question, name, failures, gaps):
    if r is None or r["target_present"] is None:
        gaps.add(f"Missing {caller}'s subject-fact observation at {name}.")
    elif r["target_present"]:
        failures.add(f"{caller} recovered the erased finding at {name}.", kind="residual")
    if control_question:
        if r is None or r["control_present"] is None:
            gaps.add(f"Missing {caller}'s retained-fact observation at {name}.")
        elif not r["control_present"]:
            failures.add(f"{caller} lost the retained finding at {name}.", kind="control-loss")


def _after(stage, name, e, failures, gaps):
    if stage is None:
        gaps.add(f"No {name} observations.")
        return
    for r, a, before in zip(stage["stored"], e["case"]["artifacts"], e["baseline"]["stored"]):
        stored_outcome(r, a, before, name, failures, gaps)
    for (store, key), row in zip(discovered_keys(e["case"], e["baseline"]["scans"]), stage["discovered"]):
        if row is None:
            gaps.add(f"Missing direct read of discovered {store}/{key} at {name}.")
        elif row["exists"]:
            failures.add(f"Discovered copy {store}/{key} still exists at {name}.", kind="residual")
    for store, hits in stage["scans"].items():
        if hits is None:
            gaps.add(f"Missing canary scan of {store} at {name}.")
        else:
            for key in hits:
                failures.add(f"Canary found in {store}/{key} at {name}.", kind="residual")
    for (caller, _, role, _), row in zip(answer_slots(e["case"]), stage["answers"]):
        answer_outcome(row, caller, role == "control", name, failures, gaps)


def _write_attribution(e, race):
    if "late_write_window" not in e:
        return []
    window, events = e["late_write_window"], e["journal"]
    _fields(window, "start stop")
    start, stop = window["start"], window["stop"]
    require(race == "exercised" and e["erased"] is True and not e["expected_producers"]
            and e["journal_chain"] is not None and e["journal_chain"]["status"] in ("verified", "partial")
            and type(start) is int and type(stop) is int and 0 <= start < stop <= len(events),
            "Invalid journal window for the observed release and drain.")
    immediately = e["immediate"]
    final = e["final"]
    before = {(r["store"], r["key"]): r for r in immediately["stored"] + immediately["discovered"] if r}
    late = e.get("late_residuals", [])
    if "late_residuals" in e:
        late_keys = new_residual_keys(e["case"], e["baseline"]["scans"], final["scans"])
        require(isinstance(late, list) and len(late) == len(late_keys) and late, "Invalid late residual read slots.")
        for (store, key), row in zip(late_keys, late, strict=True):
            if row is not None:
                check_stored(row, store, key)
    after = {(r["store"], r["key"]): r for r in final["stored"] + final["discovered"] + late if r}
    writes = defaultdict(list)
    for i in range(start, len(events)):
        event = events[i]
        if event["action"] in ("stored", "derived"):
            writes[event["store"], event["key"]].append((i, event))
    result = []
    for (store, key), row in sorted(after.items()):
        earlier = before.get((store, key))
        canary_match = key in (final["scans"].get(store) or ())
        if not row["exists"] or not (row["target_present"] is True or canary_match):
            continue
        if store in final["scans"] and (immediately["scans"][store] is None or key in immediately["scans"][store]):
            continue
        if earlier is not None:
            if earlier["exists"] and earlier["target_present"] is not False:
                continue
        elif not canary_match or not scan_scope.same_scope(immediately, final, store):
            continue
        candidates = writes[store, key]
        completed = [(i, event) for i, event in candidates
                     if event["details"].get("stage") in (None, "returned", "committed")]
        if len(completed) != 1:
            continue
        i, event = completed[0]
        if (i >= stop or "turn" not in event or event["details"].get("revision") != row["revision"]
                or any(j >= i or other.get("turn") != event["turn"] or other["details"].get("stage") != "attempted"
                       for j, other in candidates if j != i)):
            continue
        result.append({"store": store, "key": key, "revision": row["revision"], "turn": event["turn"],
                       "agent": event["agent"], "initiator": event["initiator"], "journal_entry": i})
    return result


def _sampling_summary(e):
    spec = e["case"]
    policy = spec.get("judges")
    result = {"samples_per_question": spec["sampling"]["samples"], "stages": {}}
    for role in ("target", "control"):
        result[role + "_coverage"] = coverage(policy[role]) if policy else "application-signals"
    planned = len(spec["questions"]) * len(spec["callers"]) * spec["sampling"]["samples"]
    for stage in ("baseline", "immediate", "final"):
        observed = [row for row in e[stage]["answers"] if row is not None] if e[stage] else []
        counts = {"planned": planned, "observed": len(observed)}
        for role in ("target", "control"):
            counts[role + "_positive"] = sum(row[role + "_present"] is True for row in observed)
            counts[role + "_uncertain"] = sum(row[role + "_present"] is None for row in observed)
        result["stages"][stage] = counts
    return result


def _artifact_index(e):
    """Portable links to direct observations for each declared and discovered copy."""
    spec = e["case"]
    discovered = [dict(store=store, key=key, action="delete")
                  for store, key in discovered_keys(spec, e["baseline"]["scans"])]
    rows = []
    for slot, targets in (("stored", spec["artifacts"]), ("discovered", discovered)):
        for i, target in enumerate(targets):
            evidence = [f"{EVIDENCE}#/{stage}/{slot}/{i}" for stage in ("baseline", "immediate", "final")
                        if e[stage] is not None and e[stage][slot][i] is not None]
            rows.append({**target, "discovered_by": "canary" if slot == "discovered" else "case", "evidence": evidence})
    return rows


def lineage_gaps(e, external):
    spec = e["case"]
    gaps = set()
    checked_versions = {(row["store"], row["key"], row["revision"]) for row in e["baseline"]["stored"] + e["baseline"]["discovered"]
                        if row and row["exists"]}
    if e["lineage"] is not None:
        affected = check_lineage(e["lineage"])
        derived_versions = {_ref(row["artifact"]) for row in e["lineage"]["derivations"]}
        needs_derivation = any(a["action"] == "rewrite" for a in spec["artifacts"])
        if spec["require_lineage"] and (not affected or (needs_derivation and not e["lineage"]["derivations"])):
            gaps.add("Missing source-to-artifact provenance.")
        if spec["require_lineage"]:
            for a, before in zip(spec["artifacts"], e["baseline"]["stored"]):
                if before and before["exists"]:
                    ref = (a["store"], a["key"], before["revision"])
                    if a["action"] == "rewrite" and (ref not in affected or (
                            e["format"] in GROUPED_EVIDENCE_FORMATS and ref not in derived_versions)):
                        gaps.add(f"Missing provenance for required rewrite {ref[0]}/{ref[1]}@{ref[2]}.")
                    elif a["action"] == "delete" and not needs_derivation and ref not in affected:
                        gaps.add(f"Missing provenance for required deletion {ref[0]}/{ref[1]}@{ref[2]}.")
                    elif a["action"] == "preserve" and ref in affected:
                        gaps.add(f"Preserved artifact {ref[0]}/{ref[1]} is in the affected lineage.")
        for store, key, version in affected - checked_versions:
            if (store, key) not in external:
                gaps.add(f"Affected version {store}/{key}@{version} lacks a matching baseline inspection.")
    elif spec["require_lineage"]:
        gaps.add("Missing source-to-artifact provenance.")
    return gaps


def _check_maintenance(e):
    steps = e["case"].get("maintenance_steps", ())
    require(bool(steps) == ("maintenance" in e), "Maintenance steps need corresponding evidence.")
    if not steps:
        return
    require(e["format"] in (EVIDENCE_FORMAT, DEPLOYMENT_EVIDENCE_FORMAT), "Historical evidence cannot declare maintenance.")
    rows = e["maintenance"]
    _list(rows, MAX_MAINTENANCE_STEPS, "maintenance")
    require(len(rows) == len(steps), "Wrong number of maintenance stages.")
    artifacts = e["case"]["artifacts"]
    can_continue = e["erased"] is True
    for step, row in zip(steps, rows):
        if row is None:
            can_continue = False
            continue
        _fields(row, "step stored")
        require(can_continue and row["step"] == step, "Maintenance observations are out of order.")
        stored = row["stored"]
        require(isinstance(stored, list) and len(stored) == len(artifacts), "Wrong maintenance artifact readings.")
        for reading, artifact in zip(stored, artifacts):
            if reading is not None:
                check_stored(reading, artifact["store"], artifact["key"])


def _maintenance_checks(e, failures, gaps):
    results = []
    for index, (step, row) in enumerate(zip(e["case"].get("maintenance_steps", ()), e.get("maintenance", ()))):
        if row is None:
            gaps.add(f"Missing completed maintenance step {step}.")
            results.append({"step": step, "checks": []})
            continue
        checks = []
        observations = zip(e["case"]["artifacts"], e["baseline"]["stored"], row["stored"])
        for slot, (artifact, before, reading) in enumerate(observations):
            found, missing = Findings(), set()
            stored_outcome(reading, artifact, before, f"maintenance step {step}", found, missing)
            failures.update(found)
            failures.kinds.update(found.kinds)
            gaps.update(missing)
            status = "FAIL" if found else "NOT TESTED" if missing else "PASS"
            evidence = None if reading is None else f"{EVIDENCE}#/maintenance/{index}/stored/{slot}"
            checks.append({**artifact, "status": status, "evidence": evidence})
        results.append({"step": step, "checks": checks})
    return results


def _post_erasure_findings(e, *, grouped=False):
    failures, gaps = Findings(), set()
    if e["erased"] is not None:
        for phase, name in (("immediate", "after erasure"), ("final", "final read")):
            stage = e[phase]
            if grouped and stage is not None:
                stage = {**stage, "answers": answer_records(e, phase)}
            _after(stage, name, e, failures, gaps)
    checks = _maintenance_checks(e, failures, gaps)
    return failures, gaps, checks


def post_erasure_findings(e, *, grouped=False):
    failures, gaps, _ = _post_erasure_findings(e, grouped=grouped)
    return failures, gaps


def derive(e, digest):
    evidence_format = e.get("format") if isinstance(e, dict) else None
    modern = evidence_format in GROUPED_EVIDENCE_FORMATS
    deployed = evidence_format == DEPLOYMENT_EVIDENCE_FORMAT
    provenanced = evidence_format in (EVIDENCE_FORMAT, DEPLOYMENT_EVIDENCE_FORMAT)
    _fields(e, "format run scope observed_at subject mode case worker external lineage baseline erased immediate final "
               "expected_producers coverage journal errors drill_duration_ms" + (" journal_chain" if modern else "")
               + (" late_write_window" if modern and "late_write_window" in e else "")
               + (" late_residuals" if modern and "late_write_window" in e and "late_residuals" in e else "")
               + (" maintenance" if modern and "maintenance" in e else "")
               + (" provenance" if provenanced else "") + (" deployment" if deployed else ""))
    require(evidence_format in (*GROUPED_EVIDENCE_FORMATS, FLAT_EVIDENCE_FORMAT, LEGACY_EVIDENCE_FORMAT) and e["scope"] == "person" and _hex(digest, 64),
            "Invalid v3 evidence header.")
    producer = validate_provenance(e["provenance"]) if provenanced else None
    identifier(e["run"], "run")
    _timestamp(e["observed_at"])
    _fields(e["subject"], "ref classification")
    Subject(**e["subject"])
    spec = e["case"]
    check_spec(spec)
    require(("sampling" in spec) == (e["format"] != LEGACY_EVIDENCE_FORMAT), "Wrong evidence format for the answer policy.")
    check_external(e["external"], spec, e["subject"]["ref"])
    require(type(e["drill_duration_ms"]) is int and e["drill_duration_ms"] >= 0, "Invalid drill duration.")
    require(e["mode"] in ("late-write", "deletion"), "Invalid drill mode.")
    if modern:
        # Keep observation evaluation identical while retaining the real per-question
        # sample coordinates in the evidence on disk.
        e = dict(e)
        for phase in ("baseline", "immediate", "final"):
            if e[phase] is not None:
                _fields(e[phase], "stored answers scans discovered" +
                        (" scan_details" if isinstance(e[phase], dict) and "scan_details" in e[phase] else ""))
                e[phase] = {**e[phase], "answers": answer_records(e, phase)}
    check_stage(e["baseline"], spec, scoped=modern)
    check_stage(e["final"], spec, e["baseline"]["scans"], scoped=modern)
    if e["immediate"] is not None:
        check_stage(e["immediate"], spec, e["baseline"]["scans"], scoped=modern)
    scan_coverage, uninspected_remotes, scope_gaps = scan_scope.summarize(e)
    open_items = e["external"] + uninspected_remotes
    _fields(e["worker"], "held checkpoint pre_release drained")
    worker = e["worker"]
    for attempt in worker.values():
        if attempt is not None:
            identifier(attempt, "worker attempt")
            require(e["mode"] == "late-write", "A deletion-only run cannot claim worker observations.")
    require(type(e["erased"]) in (bool, type(None)), "Invalid erase outcome.")
    armed = baseline_ready(spec, e["baseline"]) and ready_for_mode(e["mode"], worker)
    if e["erased"] is not None:
        require(armed, "Erase was attempted without the declared baseline and hold.")
    require((e["immediate"] is not None) == (e["erased"] is not None), "Inconsistent erasure stages.")
    _check_maintenance(e)
    gaps = set(scope_gaps)
    declared = {(a["store"], a["key"]) for a in spec["artifacts"]}
    declared.update(discovered_keys(spec, e["baseline"]["scans"]))
    external = {(x["store"], x["key"]) for x in e["external"]}
    _journal(e, declared | external, external, gaps)
    gaps.update(lineage_gaps(e, external))
    for store, key in _unconfirmed_deletions(spec, e["baseline"]):
        gaps.add(f"Deletion of {store}/{key} requires an observed absence of the retained finding. "
                 "Declare a rewrite for a shared artifact.")
    if not baseline_ready(spec, e["baseline"]):
        gaps.add("Missing positive baseline for a required artifact, scanner, caller or question.")
    if e["erased"] is not True:
        gaps.add("No completed erasure.")
    race = "not exercised"
    if e["mode"] == "late-write":
        completed = armed and worker["held"] == worker["pre_release"] == worker["drained"] and e["immediate"] is not None
        race = "exercised" if completed else "incomplete"
        if not completed:
            gaps.add("Missing the same live pre-write attempt through erasure, release and drain.")
    failures, observation_gaps, maintenance_checks = _post_erasure_findings(e)
    gaps.update(observation_gaps)
    errors = e["errors"]
    _list(errors, 8192, "adapter errors")
    for err in errors:
        _fields(err, "operation error")
        identifier(err["operation"], "operation")
        identifier(err["error"], "error")
    adapter_errors = errors
    if "sampling" in spec:
        adapter_errors = [err for err in errors if err["operation"] not in ("target_judge", "control_judge")]
        if len(adapter_errors) != len(errors):
            gaps.add("An answer judge could not evaluate a required reply.")
    observed_deployment = deployment.derive(e["deployment"], e, producer) if deployed else None
    if observed_deployment is not None and observed_deployment["status"] != "verified":
        gaps.add("One or more HTTP observations did not match the configured deployment identity.")
    tech = "FAIL" if failures else "ERROR" if adapter_errors else "NOT TESTED" if gaps else "PASS"

    review = "RESIDUAL_FOUND" if tech == "FAIL" else "OPEN_ITEMS" if open_items or tech != "PASS" else "READY_FOR_REVIEW"
    metadata = {"evidence_format": evidence_format, "provenance": producer} if provenanced else {}
    if observed_deployment is not None:
        metadata["deployment"] = observed_deployment
    attribution = _write_attribution(e, race)
    if attribution:
        metadata["write_attribution"] = attribution
    if modern:
        metadata["journal_chain"] = e["journal_chain"]
        if "judges" in spec:
            metadata["lexical_only"] = not any(
                rule["kind"] == "model" for role in ("target", "control") for rule in spec["judges"][role]
            )
    return {**metadata, **({"controls": spec["controls"]} if "controls" in spec else {}),
            **({"answer_sampling": _sampling_summary(e)} if "sampling" in spec else {}),
            "format": RECEIPT_FORMAT, "version": 3, "scope": "person", "run": e["run"], "subject": e["subject"],
            "observed_at": e["observed_at"], "case": spec, "mode": e["mode"], "race": race,
            **({"scan_coverage": scan_coverage} if scan_coverage else {}),
            "technical_verdict": tech, "review_status": review, "open_items": open_items,
            "artifact_checks": _artifact_index(e),
            **({"maintenance_checks": maintenance_checks} if maintenance_checks else {}),
            "failures": sorted(failures), "gaps": sorted(gaps), "errors": errors, "drill_duration_ms": e["drill_duration_ms"],
            "evidence": {"file": EVIDENCE, "sha256": digest}}


def replay_bytes(directory, raw):
    receipt = json.loads(raw.decode(), object_pairs_hook=unique_fields)
    evidence = read_regular_bytes(Path(directory) / EVIDENCE, "shared evidence", MAX_BUNDLE_FILE_BYTES)
    result = derive(json.loads(evidence.decode(), object_pairs_hook=unique_fields), sha256(evidence).hexdigest())
    require(json.dumps(receipt, sort_keys=True) == json.dumps(result, sort_keys=True),
            "Receipt differs from the replayed v3 evidence.")
    return result


def replay(directory):
    return replay_bytes(directory, read_regular_bytes(Path(directory) / RECEIPT, "erasure receipt", MAX_BUNDLE_FILE_BYTES))


def exit_code(receipt, *, case_review=False):
    native = EXIT[receipt["technical_verdict"]]
    if case_review and native == 0 and receipt["review_status"] != "READY_FOR_REVIEW":
        return 3
    return native


def human_issue(text):
    return text.replace(" at after erasure.", " after erasure.")


def attribution_lines(receipt, *, prefix=""):
    return [
        f'{row["store"]}/{row["key"]} · turn {row["turn"]} · agent {row["agent"]} · initiator {row["initiator"]}. '
        f'The journaled write matches the residual revision ({prefix}{EVIDENCE}#/journal/{row["journal_entry"]}).'
        for row in receipt.get("write_attribution", ())
    ]


def maintenance_lines(receipt):
    confirmed = {"delete": "erased", "preserve": "same revision retained", "rewrite": "rewritten"}
    for result in receipt.get("maintenance_checks", ()):
        step = result["step"]
        if not result["checks"]:
            yield f"{step} · NOT TESTED · Completion unconfirmed."
        for check in result["checks"]:
            loc = f"{check['store']}/{check['key']}"
            status, action = check["status"], check["action"]
            detail = confirmed[action] if status == "PASS" else f"{action} requirement"
            pointer = f" · {check['evidence']}" if check["evidence"] else ""
            yield f"{step} · {status} · {loc} · {detail}{pointer}"


def summary(receipt):
    mode = "write-back" if receipt["mode"] == "late-write" else receipt["mode"]
    lines = [f"Technical verdict {receipt['technical_verdict']} · Review status {receipt['review_status']}",
             f"{receipt['case']['conversation']} · {mode} · write-back check {receipt['race']}",
             f"Drill duration {receipt['drill_duration_ms']} ms"]
    samples = receipt.get("answer_sampling")
    if samples:
        lines.append(f"Reads per question per caller: {samples['samples_per_question']}. "
                     f"Erased fact coverage: {samples['target_coverage']}.")
    if receipt["technical_verdict"] == "PASS":
        callers = ", ".join(receipt["case"]["callers"])
        lines.append(f"Reopened {receipt['case']['conversation']} as {callers}. Each caller lost the erased fact and kept the control fact.")
    for row in receipt["artifact_checks"]:
        if row["discovered_by"] == "canary":
            lines.append(f"Canary located {row['store']}/{row['key']}.")
    lines.extend(human_issue(text) for text in receipt["failures"] + receipt["gaps"])
    lines.extend(maintenance_lines(receipt))
    lines.extend(attribution_lines(receipt))
    for err in receipt["errors"]:
        lines.append(f"{err['operation']} failed ({err['error']}).")
    for item in receipt["open_items"]:
        follow = item["follow_up"] or "unscheduled"
        lines.append(f"Open {item['store']}/{item['key']} · {item['kind']} · owner {item['owner']} · "
                     f"evidence {item['evidence']} · follow-up {follow}")
    return "\n".join(lines)
