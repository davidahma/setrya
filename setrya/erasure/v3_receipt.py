"""Compute technical and case-review decisions from one frozen evidence format."""
from collections import defaultdict, deque
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

from setrya.offboarding.inputs import keys_match, read_regular_bytes, require, unique_fields
from .judges import check_observation, check_policy
from .journal import identifier, validate_details
from .model import check_callers, route_id
from .receipt import EVIDENCE, EXIT, MAX_BUNDLE_FILE_BYTES, RECEIPT, _hex, _timestamp
from setrya.receipt_format import FORMAT_V3 as RECEIPT_FORMAT
from .subject import Subject

EVIDENCE_FORMAT = "setrya.erasure.evidence.v3"


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
    judged = isinstance(spec, dict) and "judges" in spec
    _fields(spec, "conversation route callers questions artifacts scan_stores require_lineage" + (" judges" if judged else ""))
    if judged:
        check_policy(spec["judges"])
    identifier(spec["conversation"], "conversation")
    route_id(spec["route"])
    check_callers(spec["callers"])
    questions = spec["questions"]
    require(isinstance(questions, list) and len(questions) == 2 and all(_hex(q, 64) for q in questions)
            and questions[0] != questions[1], "Supply distinct target and control questions.")
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


def check_stage(stage, spec, baseline_scans=None):
    _fields(stage, "stored answers scans discovered")
    require(isinstance(stage["stored"], list) and len(stage["stored"]) == len(spec["artifacts"]), "Missing store slots.")
    for row, artifact in zip(stage["stored"], spec["artifacts"]):
        if row is not None:
            check_stored(row, artifact["store"], artifact["key"])
    require(keys_match(stage["scans"], spec["scan_stores"]), "Wrong scanner slots.")
    for values in stage["scans"].values():
        if values is not None:
            _unique_ids(values, 512, "scanner key")
    discovered = discovered_keys(spec, stage["scans"] if baseline_scans is None else baseline_scans)
    require(isinstance(stage["discovered"], list) and len(stage["discovered"]) == len(discovered), "Wrong discovered key slots.")
    for row, (store, key) in zip(stage["discovered"], discovered):
        if row is not None:
            check_stored(row, store, key)
    count = len(spec["questions"])
    require(isinstance(stage["answers"], list) and len(stage["answers"]) == len(spec["callers"]) * count,
            "Missing endpoint slots.")
    for i, row in enumerate(stage["answers"]):
        if row is not None:
            caller, question = spec["callers"][i // count], spec["questions"][i % count]
            fields = "caller conversation route question target_present control_present"
            _fields(row, fields + (" judgments" if "judges" in spec else ""))
            if "judges" in spec:
                check_observation(row, spec["judges"])
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


def baseline_ready(spec, stage):
    if any(_unconfirmed_deletions(spec, stage)):
        return False
    for a, row in zip(spec["artifacts"], stage["stored"]):
        if row is None or not row["exists"]:
            return False
        if a["action"] == "delete":
            if row["target_present"] is not True and a["key"] not in (stage["scans"].get(a["store"]) or []):
                return False
        elif (row["control_present"] is not True
              or row["target_present"] is not (a["action"] == "rewrite")):
            return False
    for i, row in enumerate(stage["answers"]):
        field = "target_present" if i % 2 == 0 else "control_present"
        if row is None or row[field] is not True:
            return False
    if any(r is None or not r["exists"] for r in stage["discovered"]):
        return False
    return all(stage["scans"].values())  # Each declared scanner needs a positive synthetic baseline.


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
    events = e["journal"]
    if events is None:
        if expected:
            gaps.add("Missing subject journal observations.")
        return
    _list(events, 16384, "journal observation")
    for event in events:
        _fields(event, "store key source action classification observed_at details")
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


def stored_outcome(r, a, before, name, failures, gaps):
    """Apply one declared store rule to an observation after erasure."""
    loc = f"{a['store']}/{a['key']}"
    if r is None:
        gaps.add(f"Missing direct read of {loc} at {name}.")
        return
    if a["action"] == "delete":
        if r["exists"]:
            failures.add(f"{loc} still exists at {name}.")
        return
    if not r["exists"] or r["control_present"] is False:
        failures.add(f"{loc} lost the retained finding at {name}.")
    elif r["control_present"] is None:
        gaps.add(f"Unobserved retained finding in {loc} at {name}.")
    if r["target_present"] is True:
        failures.add(f"{loc} contains the erased finding at {name}.")
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
        failures.add(f"{caller} recovered the erased finding at {name}.")
    if control_question:
        if r is None or r["control_present"] is None:
            gaps.add(f"Missing {caller}'s retained-fact observation at {name}.")
        elif not r["control_present"]:
            failures.add(f"{caller} lost the retained finding at {name}.")


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
            failures.add(f"Discovered copy {store}/{key} still exists at {name}.")
    for store, hits in stage["scans"].items():
        if hits is None:
            gaps.add(f"Missing canary scan of {store} at {name}.")
        else:
            for key in hits:
                failures.add(f"Canary found in {store}/{key} at {name}.")
    for i, r in enumerate(stage["answers"]):
        answer_outcome(r, e["case"]["callers"][i // 2], i % 2, name, failures, gaps)


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
        needs_derivation = any(a["action"] == "rewrite" for a in spec["artifacts"])
        if spec["require_lineage"] and (not affected or (needs_derivation and not e["lineage"]["derivations"])):
            gaps.add("Missing source-to-artifact provenance.")
        if spec["require_lineage"]:
            for a, before in zip(spec["artifacts"], e["baseline"]["stored"]):
                if before and before["exists"]:
                    ref = (a["store"], a["key"], before["revision"])
                    if a["action"] == "rewrite" and ref not in affected:
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


def derive(e, digest):
    _fields(e, "format run scope observed_at subject mode case worker external lineage baseline erased immediate final "
               "expected_producers coverage journal errors drill_duration_ms")
    require(e["format"] == EVIDENCE_FORMAT and e["scope"] == "person" and _hex(digest, 64),
            "Invalid v3 evidence header.")
    identifier(e["run"], "run")
    _timestamp(e["observed_at"])
    _fields(e["subject"], "ref classification")
    Subject(**e["subject"])
    spec = e["case"]
    check_spec(spec)
    check_external(e["external"], spec, e["subject"]["ref"])
    require(type(e["drill_duration_ms"]) is int and e["drill_duration_ms"] >= 0, "Invalid drill duration.")
    require(e["mode"] in ("late-write", "deletion"), "Invalid drill mode.")
    check_stage(e["baseline"], spec)
    check_stage(e["final"], spec, e["baseline"]["scans"])
    if e["immediate"] is not None:
        check_stage(e["immediate"], spec, e["baseline"]["scans"])
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
    failures, gaps = set(), set()
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
    if e["erased"] is not None:
        _after(e["immediate"], "after erasure", e, failures, gaps)
        _after(e["final"], "final read", e, failures, gaps)
    errors = e["errors"]
    _list(errors, 8192, "adapter errors")
    for err in errors:
        _fields(err, "operation error")
        identifier(err["operation"], "operation")
        identifier(err["error"], "error")
    tech = "FAIL" if failures else "ERROR" if errors else "NOT TESTED" if gaps else "PASS"
    review = "RESIDUAL_FOUND" if tech == "FAIL" else "OPEN_ITEMS" if external or tech != "PASS" else "READY_FOR_REVIEW"
    return {"format": RECEIPT_FORMAT, "version": 3, "scope": "person", "run": e["run"], "subject": e["subject"],
            "observed_at": e["observed_at"], "case": spec, "mode": e["mode"], "race": race,
            "technical_verdict": tech, "review_status": review, "open_items": e["external"],
            "artifact_checks": _artifact_index(e),
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


def summary(receipt):
    lines = [f"Technical verdict {receipt['technical_verdict']} · Review status {receipt['review_status']}",
             f"{receipt['case']['conversation']} · {receipt['mode']} · worker race {receipt['race']}",
             f"Drill duration {receipt['drill_duration_ms']} ms"]
    if receipt["technical_verdict"] == "PASS":
        callers = ", ".join(receipt["case"]["callers"])
        lines.append(f"Reopened {receipt['case']['conversation']} as {callers}. Each caller lost the erased fact and kept the control fact.")
    for row in receipt["artifact_checks"]:
        if row["discovered_by"] == "canary":
            lines.append(f"Canary located {row['store']}/{row['key']}.")
    lines.extend(receipt["failures"] + receipt["gaps"])
    for err in receipt["errors"]:
        lines.append(f"{err['operation']} failed ({err['error']}).")
    for item in receipt["open_items"]:
        follow = item["follow_up"] or "unscheduled"
        lines.append(f"Open {item['store']}/{item['key']} · {item['kind']} · owner {item['owner']} · "
                     f"evidence {item['evidence']} · follow-up {follow}")
    return "\n".join(lines)
