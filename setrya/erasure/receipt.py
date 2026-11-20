"""Replay v2 subject erasure receipts from bounded, local observations."""
from collections import defaultdict
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from setrya.offboarding.inputs import keys_match, read_regular_bytes, require, unique_fields
from .inventory import label, systems
from .journal import identifier, validate_details
from .subject import Subject
from .model import route_id

EVIDENCE = "erasure-evidence.json"
RECEIPT = "erasure-receipt.json"
EXIT = {"PASS": 0, "FAIL": 1, "ERROR": 2, "NOT TESTED": 3}
MAX_BUNDLE_FILE_BYTES = 16 * 1024 * 1024
STAGES = ("before", "immediate", "final")


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value)


def _timestamp(value):
    require(isinstance(value, str), "Missing evidence timestamp.")
    stamp = datetime.fromisoformat(value)
    require(stamp.utcoffset() is not None, "Evidence timestamps need an offset.")
    return stamp


def validate_claims(items, inventory, subject_ref):
    require(isinstance(items, list), "Invalid owner dispositions.")
    claims = {}
    for item in items:
        require(keys_match(item, ("store", "key", "owner", "evidence", "subject_ref", "until")), "Invalid owner disposition.")
        for field in ("store", "key", "owner", "evidence"):
            identifier(item[field], field)
        require(item["subject_ref"] == subject_ref, "Disposition belongs to a different subject.")
        store, key = item["store"], item["key"]
        require(store in inventory and inventory[store]["owner"] == item["owner"], "Disposition owner mismatch.")
        require((store, key) not in claims, "Duplicate owner disposition.")
        if item["until"] is not None:
            require(isinstance(item["until"], str) and date.fromisoformat(item["until"]).isoformat() == item["until"],
                    "Use an ISO retention date.")
        claims[store, key] = item
    return claims


def _observations(evidence):
    case, reads = evidence["case"], evidence["reads"]
    require(keys_match(case, ("route", "callers", "question_sha256")) and _hex(case["question_sha256"], 64),
            "Invalid erasure case.")
    route_id(case["route"])
    callers = case["callers"]
    require(isinstance(callers, list) and len(callers) == 2 and callers[0] != callers[1], "Expected two callers.")
    for caller in callers:
        identifier(caller, "caller")
    require(keys_match(reads, STAGES), "Invalid application read stages.")
    for stage in STAGES:
        require(isinstance(reads[stage], list) and len(reads[stage]) == len(callers), "Missing caller slots.")
        for caller, row in zip(callers, reads[stage]):
            if row is None:
                continue
            require(keys_match(row, ("caller", "route", "question_sha256", "subject_present", "control_present"))
                    and row["caller"] == caller and row["route"] == case["route"]
                    and row["question_sha256"] == case["question_sha256"], "Application read identity mismatch.")
            require(all(type(row[f]) in (type(None), bool) for f in ("subject_present", "control_present")),
                    "Invalid application read outcome.")


def _stores(evidence, inventory):
    stores = evidence["stores"]
    require(keys_match(stores, inventory), "Expected readings for every inventory system.")
    for name, row in stores.items():
        require(keys_match(row, ("proof", "before", "deleted", "immediate", "final")), "Invalid store checks.")
        proof = row["proof"]
        require(proof in ("direct", "configuration", "none") and type(row["deleted"]) is bool, "Invalid store proof.")
        if inventory[name]["adapter"] == "none" or inventory[name]["role"] == "provider":
            require(proof != "direct", "A direct proof requires a checkable local adapter.")
        for stage in STAGES:
            keys = row[stage]
            if keys is not None:
                require(proof == "direct" and isinstance(keys, list) and len(keys) <= 4096
                        and all(identifier(k, "copy key") for k in keys) and keys == sorted(set(keys)),
                        "Invalid or duplicate scoped keys.")
        require(not row["deleted"] or row["before"] is not None, "Deletion requires scoped discovery.")
    return stores


def _locations(evidence, stores):
    rows = defaultdict(list)
    for store, checks in stores.items():
        for stage in STAGES:
            for key in checks[stage] or []:
                rows[store, key]
    history = evidence["journal"]
    require(history is None or isinstance(history, list), "Invalid copy journal.")
    for i, record in enumerate(history or []):
        require(keys_match(record, ("store", "key", "source", "action", "classification", "observed_at", "details")),
                "Invalid journal fields.")
        for field in ("store", "key", "source"):
            identifier(record[field], field)
        label(record["classification"])
        _timestamp(record["observed_at"])
        require(record["action"] in ("retrieved", "sent", "stored", "derived")
                and isinstance(record["details"], dict), "Invalid journal observation.")
        validate_details(record["details"])
        rows[record["store"], record["key"]].append(i)
    return rows


def derive(evidence, digest):
    require(keys_match(evidence, ("format", "run", "subject", "observed_at", "case", "inventory", "worker",
                                  "reads", "stores", "journal", "dispositions", "errors", "time_to_first_verdict_ms"))
            and evidence["format"] == "setrya.erasure.evidence.v2" and _hex(evidence["run"], 32)
            and _hex(digest, 64), "Invalid erasure evidence header.")
    require(keys_match(evidence["subject"], ("ref", "classification")), "Invalid subject.")
    subject = Subject(**evidence["subject"])
    observed = _timestamp(evidence["observed_at"])
    elapsed = evidence["time_to_first_verdict_ms"]
    require(type(elapsed) is int and elapsed >= 0, "Invalid verdict duration.")
    inventory = systems(evidence["inventory"])
    claims = validate_claims(evidence["dispositions"], inventory, subject.ref)
    _observations(evidence)
    stores = _stores(evidence, inventory)
    locations = _locations(evidence, stores)
    worker, reads = evidence["worker"], evidence["reads"]
    require(keys_match(worker, ("held", "checkpoint", "pre_release", "drained")), "Missing worker evidence.")
    for field in ("held", "checkpoint", "pre_release"):
        if worker[field] is not None:
            identifier(worker[field], "attempt")
    require(type(worker["drained"]) in (type(None), bool), "Invalid drain outcome.")
    armed = bool(worker["held"] and worker["held"] == worker["checkpoint"]
                 and all(r and r["subject_present"] is True and r["control_present"] is True for r in reads["before"]))
    drained = armed and worker["pre_release"] == worker["held"] and worker["drained"] is True
    gaps, failures = [], []
    for name, key in sorted(set(claims) - set(locations)):
        gaps.append(f"Owner disposition for {name}/{key} has no observed copy.")
    if not drained:
        gaps.append("Missing a held pre-write attempt through deletion and its terminal drain.")
    for stage in ("immediate", "final"):
        for caller, r in zip(evidence["case"]["callers"], reads[stage]):
            if armed and r and r["subject_present"] is True:
                failures.append(f"{caller} saw the subject at {stage}.")
            if armed and r and r["control_present"] is False:
                failures.append(f"{caller} lost the retained control at {stage}.")
            if r is None or r["subject_present"] is None or r["control_present"] is None:
                gaps.append(f"Missing {stage} serving evidence for {caller}.")
    if not any(r["before"] for r in stores.values()):
        gaps.append("No live subject copy discovered before deletion.")
    if evidence["journal"] is None:
        gaps.append("Missing copy journal.")
    errors = evidence["errors"]
    require(isinstance(errors, list) and all(keys_match(e, ("operation", "error"))
            and identifier(e["operation"], "operation") and identifier(e["error"], "error") for e in errors),
            "Invalid adapter errors.")
    for name, store in stores.items():
        if store["proof"] != "direct" or not store["deleted"] or any(store[t] is None for t in STAGES):
            gaps.append(f"{name} has incomplete direct deletion evidence.")
    # A receipt may contain thousands of locations. Build membership indexes once
    # so replay does not scan the same store or claims list for every copy.
    discovered = {name: set(row["before"] or []) for name, row in stores.items()}
    residuals = {name: set(row["immediate"] or []) | set(row["final"] or []) for name, row in stores.items()}
    approved = {name: set(row["approved_labels"]) for name, row in inventory.items()}
    claim_indexes = {location: i for i, location in enumerate(claims)}
    copies = []
    for (name, key), indexes in sorted(locations.items()):
        system, store = inventory.get(name), stores.get(name)
        records = [evidence["journal"][i] for i in indexes]
        labels = sorted({r["classification"] for r in records} | {subject.classification})
        if not system:
            gaps.append(f"{name} is missing from inventory.")
        elif set(labels) - approved[name]:
            failures.append(f"{name} holds a classification outside its approved labels.")
        external = any(r["details"].get("evidence_level") == "configuration" or r["details"].get("provider")
                       for r in records)
        claim = claims.get((name, key))
        status = "unknown"
        if armed and store and key in residuals[name]:
            status = "still present"
            failures.append(f"{name}/{key} remained or returned.")
        elif (not external and drained and store and store["deleted"] and store["proof"] == "direct"
              and all(store[t] is not None for t in STAGES) and key in discovered[name]):
            status = "verified gone"
        elif claim:
            if claim["until"] is None:
                status = "attested"
            elif date.fromisoformat(claim["until"]) >= observed.date():
                status = "retained until"
        links = [f"{EVIDENCE}#/journal/{i}" for i in indexes]
        if store:
            token = name.replace("~", "~0").replace("/", "~1")
            links.append(f"{EVIDENCE}#/stores/{token}")
        if claim:
            links.append(f"{EVIDENCE}#/dispositions/{claim_indexes[name, key]}")
        copies.append({"store": name, "key": key, "owner": system["owner"] if system else None,
                       "classifications": labels, "status": status, "evidence": links,
                       "disposition": claim})
    incomplete = any(c["status"] != "verified gone" for c in copies)
    verdict = "FAIL" if failures else "ERROR" if errors else "NOT TESTED" if gaps or incomplete else "PASS"
    return {"format": "setrya.erasure.receipt.v2", "version": 2, "run": evidence["run"], "subject": evidence["subject"],
            "observed_at": evidence["observed_at"], "case": evidence["case"], "verdict": verdict, "copies": copies,
            "unknown_copies": sum(c["status"] == "unknown" for c in copies), "time_to_first_verdict_ms": elapsed,
            "failures": sorted(set(failures)), "gaps": sorted(set(gaps)), "errors": errors,
            "evidence": {"file": EVIDENCE, "sha256": digest}}


def load_evidence(directory):
    raw = read_regular_bytes(Path(directory) / EVIDENCE, "subject evidence", MAX_BUNDLE_FILE_BYTES)
    return json.loads(raw.decode(), object_pairs_hook=unique_fields), sha256(raw).hexdigest()


def replay_bytes(directory, raw):
    receipt = json.loads(raw.decode(), object_pairs_hook=unique_fields)
    evidence, digest = load_evidence(directory)
    result = derive(evidence, digest)
    require(json.dumps(receipt, sort_keys=True) == json.dumps(result, sort_keys=True),
            "Receipt differs from the replayed erasure evidence.")
    return result


def replay(directory):
    return replay_bytes(directory, read_regular_bytes(Path(directory) / RECEIPT, "erasure receipt", MAX_BUNDLE_FILE_BYTES))
