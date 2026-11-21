"""Run and replay a discovery finding against its approved application scope."""
from collections import defaultdict, deque
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
from secrets import token_hex

from setrya.evidence import bundle_manifest
from setrya.erasure import v3_receipt
from setrya.erasure.receipt import EVIDENCE, MAX_BUNDLE_FILE_BYTES, RECEIPT
from setrya.erasure.subject import Subject
from setrya.erasure.v3 import SharedCase, Stored, run_shared
from setrya.offboarding.inputs import keys_match, publish_new, read_regular_bytes, require, unique_fields

from .mapping import EXTERNAL_ROLES, validate_binding

INPUT = "case-input.json"
RESULT = "case-result.json"
DRILL = "erasure"
STAGES = ("baseline", "immediate", "final")
FORMAT = "setrya.discovery.result"


def _canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _load(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)


def _location(ref):
    return ref["store"], ref["key"]


def _version(ref):
    return ref["store"], ref["key"], ref["revision"]


def _descendants(source, lineage):
    """Follow only this source version. A subject may have other unrelated roots."""
    root = _version(source)
    if not lineage or root not in {_version(r) for r in lineage["roots"]}:
        return set()
    children = defaultdict(list)
    for edge in lineage["derivations"]:
        for ref in edge["inputs"]:
            children[_version(ref)].append(_version(edge["artifact"]))
    found = {root}
    visit = deque([root])
    while visit:
        for child in children[visit.popleft()]:
            if child not in found:
                found.add(child)
                visit.append(child)
    return found


def _permission_gaps(binding, spec, baseline, lineage):
    source = binding["source"]
    affected = _descendants(source, lineage)
    gaps = set()
    if not affected:
        gaps.add("The mapped source version is missing from the test subject's lineage roots.")
    inspected = {_location(a) for a in spec["artifacts"]}
    for service in binding["services"]:
        if service.get("action") == "delete" and _location(service) not in inspected:
            gaps.add(f"Cannot delete {service['store']}/{service['key']} without inspecting its retained content.")
    for artifact, row in zip(spec["artifacts"], baseline["stored"]):
        if _location(artifact) == _location(source):
            if row is None or row["revision"] != source["revision"]:
                gaps.add("The source read does not match the revision approved in the mapping.")
        if artifact["action"] in ("delete", "rewrite"):
            if not row or _version(row) not in affected:
                gaps.add(f"No source-version path to {artifact['store']}/{artifact['key']} at its baseline revision.")
        elif row and _version(row) in affected:
            gaps.add(f"Preserved record {artifact['store']}/{artifact['key']} depends on the mapped source.")
    return gaps


def _can_dispatch(binding, case, subject, application):
    # Releasing an already dispatched worker can commit a write even when erase
    # is withheld. Establish the approved source and destination versions first.
    spec = case.spec()
    lineage = application.lineage(subject)
    v3_receipt.check_lineage(lineage)
    stored = []
    for a in case.artifacts:
        read = application.inspect(subject, a.store, a.key)
        if not isinstance(read, Stored) or read.key != a.key:
            return False
        row = {"store": a.store, **asdict(read)}
        v3_receipt.check_stored(row, a.store, a.key)
        if not row["exists"] or row["control_present"] is not (a.action != "delete"):
            return False
        if row["target_present"] is not (a.action != "preserve"):
            return False
        stored.append(row)
    return not _permission_gaps(binding, spec, {"stored": stored}, lineage)


def _scope(binding, subject, spec, external):
    require(subject == {"ref": binding["subject_ref"], "classification": binding["classification"]},
            "Fixture subject or classification differs from the approved mapping.")
    require(all(spec[k] == binding[k] for k in ("conversation", "route", "callers", "questions")),
            "Fixture conversation, caller, route or question differs from the approved mapping.")
    require(spec["require_lineage"] and not spec["scan_stores"],
            "A discovery drill requires version lineage and explicitly approved artifact keys.")
    approvals = {_location(s): s for s in binding["services"] if s["role"] != "endpoint"}
    tested = set()
    for entry in spec["artifacts"]:
        loc = _location(entry)
        require(loc in approvals and approvals[loc]["action"] == entry["action"],
                "Fixture requested an artifact action outside the approved mapping.")
        tested.add(loc)
    require(_location(binding["source"]) in tested, "The fixture must inspect the mapped source key.")
    for item in external:
        approval = approvals.get(_location(item))
        require(approval and approval["role"] in EXTERNAL_ROLES and approval["owner"] == item["owner"],
                "External evidence differs from the approved service or owner.")
        require(approval["role"] != "provider" or item["kind"] == "configuration",
                "A model provider may supply configuration evidence only.")


def _prepare(binding, mode, output):
    # Freeze the validated scope before calling application code.
    packet = {"format": "setrya.discovery.input", "version": 1,
              "binding": binding, "nonce": token_hex(32), "mode": mode}
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=False)
    publish_new(packet, folder / INPUT)
    return folder, packet


def run_case(binding, *, subject, case, application, environment, revision, output, mode="late-write",
             external_items=(), journal=None, expected_producers=()):
    """Run trusted test-fixture callbacks. The fixture independently identifies its deployed revision and environment."""
    binding = validate_binding(_load(_canonical(binding)))
    require(binding["source"]["action"] != "retain", "Use record_retention for a retained source.")
    require(mode in ("deletion", "late-write"), "Choose deletion or late-write mode.")
    require(environment == binding["environment"] and revision == binding["revision"],
            "Fixture revision or environment differs from the approved mapping.")
    require(isinstance(subject, Subject) and isinstance(case, SharedCase), "Expected a subject and shared fixture case.")
    erase_approved = getattr(application, "erase_approved", None)
    require(callable(erase_approved), "A discovery fixture requires an erase_approved(subject, source, actions) callback.")
    cancel = getattr(application, "cancel_and_drain", None)
    require(mode == "deletion" or callable(cancel), "A discovery worker needs a safe cancel_and_drain hook.")
    external_items = tuple(external_items)
    _scope(binding, asdict(subject), case.spec(), [asdict(x) for x in external_items])
    folder, packet = _prepare(binding, mode, output)
    run = sha256(_canonical(packet)).hexdigest()
    source = dict(packet["binding"]["source"])
    # Catalog-only services remain coverage gaps. A verified fixture may operate
    # on just the subset whose current baseline was inspected and authorized.
    actions = tuple(asdict(artifact) for artifact in case.artifacts)
    run_shared(subject, case=case, application=application, output=folder / DRILL, mode=mode,
               external_items=external_items, journal=journal, run_id=run, expected_producers=expected_producers, cancel_hold=cancel,
               erase_action=lambda test_subject: erase_approved(test_subject, source, actions),
               authorize_hold=lambda: _can_dispatch(packet["binding"], case, subject, application),
               authorize_erase=lambda spec, baseline, lineage:
               not _permission_gaps(packet["binding"], spec, baseline, lineage))
    return _publish(folder)


def record_retention(binding, *, output):
    """Return the approved owner and review date without opening the application or dispatching a worker."""
    binding = validate_binding(_load(_canonical(binding)))
    require(binding["source"]["action"] == "retain", "Retention requires an explicitly retained source.")
    folder, _ = _prepare(binding, "retention", output)
    return _publish(folder)


def _publish(folder):
    result = _derive(folder, expected_binding=None)
    publish_new(result, folder / RESULT, max_bytes=MAX_BUNDLE_FILE_BYTES)
    return result


def _pointer(stage, slot, i):
    return f"{DRILL}/{EVIDENCE}#/{stage}/{slot}/{i}"


def _direct(service, index, e):
    row = {**service, "status": "NOT TESTED", "evidence": [], "reasons": []}
    if index is None:
        row["reasons"].append("The catalog declares this key, but the fixture supplied no direct read.")
        return row
    before = e["baseline"]["stored"][index]
    failures, gaps = set(), set()
    for stage in STAGES:
        sample = e[stage]
        stored = sample["stored"][index] if sample else None
        if stored is not None:
            row["evidence"].append(_pointer(stage, "stored", index))
        if stage != "baseline" and e["erased"] is not None:
            v3_receipt.stored_outcome(stored, service, before, stage, failures, gaps)
    if e["erased"] is not True:
        gaps.add("The drill did not complete the application's erasure operation.")
    row["status"] = "FAIL" if failures else "NOT TESTED" if gaps else "PASS"
    row["reasons"] = sorted(failures) + sorted(gaps)
    return row


def _endpoint(service, e):
    failures, gaps, pointers = set(), set(), []
    for stage in STAGES:
        sample = e[stage]
        for i, row in enumerate(sample["answers"] if sample else [None] * 4):
            if row is not None:
                pointers.append(_pointer(stage, "answers", i))
            if stage != "baseline" and e["erased"] is not None:
                v3_receipt.answer_outcome(row, e["case"]["callers"][i // 2], i % 2, stage, failures, gaps)
    if e["erased"] is not True:
        gaps.add("The drill did not complete the application's erasure operation.")
    return {**service, "status": "FAIL" if failures else "NOT TESTED" if gaps else "PASS",
            "evidence": pointers, "reasons": sorted(failures) + sorted(gaps)}


def _provider_invocations(e, binding):
    """Join an outbound SDK observation to one source-linked invocation key."""
    lineage = e["lineage"]
    affected = _descendants(binding["source"], lineage)
    providers = {_location(s) for s in binding["services"] if s["role"] == "provider"}
    edges = defaultdict(list)
    for i, edge in enumerate(lineage["derivations"] if lineage else []):
        loc = _location(edge["artifact"])
        if loc in providers:
            edges[loc].append((i, edge))
    journal = defaultdict(list)
    for i, event in enumerate(e["journal"] or []):
        loc = _location(event)
        if loc in providers and event["action"] == "sent":
            journal[loc].append((i, event))
    proof = {}
    for loc in providers:
        status, pointers = "UNOBSERVED", []
        # A reused invocation key cannot bind a journal event to a particular generation.
        if len(edges[loc]) == 1:
            index, edge = edges[loc][0]
            versions = {(r["key"], f"{r['key']}@{r['revision']}") for r in edge["inputs"] if _version(r) in affected}
            if versions:
                pointers.append(f"{DRILL}/{EVIDENCE}#/lineage/derivations/{index}")
                status, event_links = _sdk_exchange(journal[loc], binding["classification"], versions, edge)
                pointers.extend(event_links)
        proof[loc] = {"status": status, "evidence": pointers}
    return proof


def _sdk_exchange(events, classification, versions, edge):
    """Attribute one client call. Reused keys and framework start callbacks are ambiguous."""
    stages = [event["details"].get("stage") for _, event in events]
    if stages not in (["attempted"], ["attempted", "returned"], ["returned", "attempted"]):
        return "UNOBSERVED", []
    first = events[0][1]
    details = first["details"]
    sdk_operations = {"anthropic": ("messages.create",),
                      "bedrock": ("converse", "invoke_model", "converse_stream", "invoke_model_with_response_stream")}
    if (details.get("evidence_level") != "configuration"
            or details.get("operation") not in sdk_operations.get(details.get("provider"), ())):
        return "UNOBSERVED", []
    shared = {k: v for k, v in details.items() if k != "stage"}
    for _, event in events:
        if (event["classification"] != classification or event["source"] != first["source"]
                or {k: v for k, v in event["details"].items() if k != "stage"} != shared):
            return "UNOBSERVED", []
    # The SDK carries key@revision without a store, so a second input bearing
    # the same marker cannot be distinguished at this boundary.
    matching = [(r["key"], f"{r['key']}@{r['revision']}") for r in edge["inputs"]
                if r["key"] == first["source"] and f"{r['key']}@{r['revision']}" in details.get("sources", ())]
    if not any(pair in versions and matching.count(pair) == 1 for pair in matching):
        return "UNOBSERVED", []
    return ("SDK_RETURNED" if "returned" in stages else "SDK_ATTEMPTED",
            [f"{DRILL}/{EVIDENCE}#/journal/{i}" for i, _ in events])


def _external(service, located):
    if located is None:
        return {**service, "status": "OPEN", "evidence": [], "reasons": ["No external exhibit was supplied."],
                "follow_up": None}
    index, item = located
    return {**service, "status": item["kind"].upper(), "evidence": [f"{DRILL}/{EVIDENCE}#/external/{index}"],
            "exhibit": item["evidence"], "follow_up": item["follow_up"], "reasons": []}


def _base(packet):
    binding = packet["binding"]
    return {"format": FORMAT, "version": 1, "run": sha256(_canonical(packet)).hexdigest(),
            **{key: binding[key] for key in ("catalog", "sample", "case_id", "finding_id", "finding_sha256", "asset_sha256", "finding_type", "categories",
                                             "observed_at", "environment", "revision", "classification", "subject_ref",
                                             "source", "conversation", "route", "callers", "questions")},
            "mode": packet["mode"]}


def _derive(folder, expected_binding):
    packet = _load(read_regular_bytes(folder / INPUT, "discovery input"))
    require(keys_match(packet, ("format", "version", "binding", "nonce", "mode"))
            and packet["format"] == "setrya.discovery.input" and type(packet["version"]) is int
            and packet["version"] == 1 and isinstance(packet["nonce"], str)
            and re.fullmatch("[0-9a-f]{64}", packet["nonce"]), "Invalid discovery input packet.")
    binding = validate_binding(packet["binding"])
    if expected_binding is not None:
        require(_canonical(validate_binding(expected_binding)) == _canonical(binding),
                "Case scope differs from the independently supplied binding.")
    result = _base(packet)
    if binding["source"]["action"] == "retain":
        require(packet["mode"] == "retention" and not (folder / DRILL).exists(), "Invalid retained case bundle.")
        rows = []
        for s in binding["services"]:
            row = {**s, "status": "NOT TESTED", "reasons": ["The approved policy deferred erasure."],
                   "evidence": []}
            if s.get("action") == "retain":
                row.update(status="RETAINED", exhibit=s["evidence"], evidence=[f"{INPUT}#/binding/services/{len(rows)}"])
            rows.append(row)
        return {**result, "technical_verdict": "NOT TESTED", "review_status": "OPEN_ITEMS", "race": "not exercised",
                "services": rows, "erasure_receipt": None, "lineage": None, "failures": [], "errors": [],
                "gaps": ["Deletion was deferred under the approved retention policy."]}
    require(packet["mode"] in ("deletion", "late-write"), "Invalid discovery drill mode.")
    require(bundle_manifest(folder / DRILL) == RECEIPT, "Expected one nested erasure receipt.")
    # Derive each decision from a single bounded snapshot of each file. A separately
    # reopened evidence file could differ from the one that passed native replay.
    raw = read_regular_bytes(folder / DRILL / RECEIPT, "erasure receipt", MAX_BUNDLE_FILE_BYTES)
    raw_evidence = read_regular_bytes(folder / DRILL / EVIDENCE, "erasure evidence", MAX_BUNDLE_FILE_BYTES)
    e = _load(raw_evidence)
    receipt = v3_receipt.derive(e, sha256(raw_evidence).hexdigest())
    require(_canonical(_load(raw)) == _canonical(receipt), "Erasure receipt differs from the replayed evidence.")
    require(receipt["run"] == result["run"] and receipt["mode"] == packet["mode"],
            "Erasure receipt was not produced for this discovery finding, scope and run.")
    _scope(binding, receipt["subject"], e["case"], e["external"])
    permission_gaps = _permission_gaps(binding, e["case"], e["baseline"], e["lineage"])
    require(not permission_gaps or e["erased"] is None, "Erase was attempted outside the approved source lineage.")
    gaps = set(receipt["gaps"]) | permission_gaps
    direct = {_location(a): i for i, a in enumerate(e["case"]["artifacts"])}
    external = {_location(item): (i, item) for i, item in enumerate(e["external"])}
    services = []
    invocations = _provider_invocations(e, binding)
    external_open = False
    for service in binding["services"]:
        if service["role"] == "endpoint":
            row = _endpoint(service, e)
        elif service["role"] in EXTERNAL_ROLES:
            row = _external(service, external.get(_location(service)))
            if service["role"] == "provider":
                row["invocation"] = invocations[_location(service)]
            external_open = True
        else:
            row = _direct(service, direct.get(_location(service)), e)
            if _location(service) not in direct:
                gaps.add(f"No direct check for {service['id']} ({service['store']}/{service['key']}, owner {service['owner']}).")
        services.append(row)
    verdict = receipt["technical_verdict"]
    if verdict == "PASS" and gaps:
        verdict = "NOT TESTED"
    review = receipt["review_status"]
    if review == "READY_FOR_REVIEW" and (external_open or verdict != "PASS"):
        review = "OPEN_ITEMS"
    return {**result, "technical_verdict": verdict, "review_status": review, "race": receipt["race"],
            "drill_observed_at": receipt["observed_at"], "services": services, "lineage": e["lineage"],
            "erasure_receipt": {"file": f"{DRILL}/{RECEIPT}", "sha256": sha256(raw).hexdigest()},
            "failures": receipt["failures"], "gaps": sorted(gaps), "errors": receipt["errors"]}


def replay_bytes(directory, raw, *, expected_binding=None):
    folder = Path(directory)
    require(bundle_manifest(folder) == RESULT, "Expected one discovery case result.")
    result = _derive(folder, expected_binding)
    require(_canonical(_load(raw)) == _canonical(result), "Discovery result differs from the replayed case.")
    return result


def replay(directory, *, expected_binding=None):
    return replay_bytes(directory, read_regular_bytes(Path(directory) / RESULT, "discovery result", MAX_BUNDLE_FILE_BYTES),
                        expected_binding=expected_binding)


def exit_code(result, *, case_review=False):
    return v3_receipt.exit_code(result, case_review=case_review)


def summary(result):
    sample = {True: "sample finding", False: "ordinary finding", None: "sample status unknown"}[result["sample"]]
    lines = [f"Technical verdict {result['technical_verdict']} · Review status {result['review_status']}",
             f"Case {result['case_id']} · finding {result['finding_id']} · {result['catalog']} · {sample}",
             f"{result['environment']} · application {result['revision']} · worker race {result['race']}"]
    for service in result["services"]:
        follow = f" · due {service['follow_up']}" if service.get("follow_up") else ""
        lines.append(f"{service['status']}  {service['id']} · {service['role']} · owner {service['owner']}{follow}")
    lines.extend(result["failures"] + result["gaps"])
    lines.extend(f"{error['operation']} failed ({error['error']})." for error in result["errors"])
    return "\n".join(lines)
