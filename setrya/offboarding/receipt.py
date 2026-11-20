"""Issue requests and assess the evidence supplied for each deletion.

Adapted from Axiom v0.3.2 for Setrya's offboarding interface.
"""

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime

from . import gate
from setrya.status import Status

from .inputs import InvalidReport, keys_match, require

MAX_ITEMS = 256
ITEM_KINDS = ("provider_statement", "retention_schedule")
STATEMENT_STATUS = {
    "reported_deleted": "PROVIDER_REPORTED",
    "reported_retained": "REPORTED_RETAINED",
    "unresolved": "UNRESOLVED",
}
EXIT_CODES = {"READY_FOR_REVIEW": Status.PASS.exit_code, "RESIDUAL_FOUND": Status.FAIL.exit_code,
              "OPEN_ITEMS": Status.NOT_TESTED.exit_code}
DIGEST = re.compile(r"[0-9a-f]{64}")
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
UTC_SECONDS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def require_digest(value):
    require(
        isinstance(value, str) and DIGEST.fullmatch(value), "Invalid SHA-256 digest."
    )


def require_time(value):
    require(
        isinstance(value, str) and UTC_SECONDS.fullmatch(value),
        "Invalid UTC timestamp.",
    )
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise InvalidReport("Invalid UTC timestamp.") from None


def require_versioned(value, fields):
    require(keys_match(value, fields | {"version"}), "Unexpected or missing fields.")
    require(
        type(value["version"]) is int and value["version"] == 1, "Unsupported version."
    )


def validate_request(request):
    require_versioned(
        request,
        {
            "request_id",
            "created_at",
            "context",
            "target",
            "revision",
            "run_id",
            "policy_sha256",
            "items",
        },
    )
    identifier = request["request_id"]
    require(
        isinstance(identifier, str) and REQUEST_ID.fullmatch(identifier),
        "Invalid request identifier.",
    )
    require_time(request["created_at"])
    require(request["context"] in ("staging", "customer"), "Unknown request context.")
    require(
        all(gate.valid_token(request[key]) for key in ("target", "revision", "run_id")),
        "Invalid target, revision, or run identifier.",
    )
    require_digest(request["policy_sha256"])
    items = request["items"]
    require(
        isinstance(items, list) and len(items) <= MAX_ITEMS,
        "Invalid request item list.",
    )
    ids = set()
    for item in items:
        require(keys_match(item, {"id", "kind"}), "Invalid request item fields.")
        require(gate.valid_token(item["id"]), "Invalid item identifier.")
        require(item["kind"] in ITEM_KINDS, "Unknown item kind.")
        require(item["id"] not in ids, "Duplicate request item identifier.")
        ids.add(item["id"])
    return request


def issue(policy, policy_hash, *, revision, run_id, context, statements, schedules):
    gate.validate_document(policy, observations=False)
    require(
        isinstance(statements, (list, tuple))
        and isinstance(schedules, (list, tuple))
        and len(statements) + len(schedules) <= MAX_ITEMS,
        "Invalid requested item lists.",
    )
    items = [
        {"id": identifier, "kind": "provider_statement"} for identifier in statements
    ]
    items.extend(
        {"id": identifier, "kind": "retention_schedule"} for identifier in schedules
    )
    return validate_request(
        {
            "version": 1,
            "request_id": secrets.token_hex(16),
            "created_at": datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "context": context,
            "target": policy["target"],
            "revision": revision,
            "run_id": run_id,
            "policy_sha256": policy_hash,
            "items": items,
        }
    )


def assess_items(items, records, request_hash):
    require(
        isinstance(records, Iterator) or isinstance(records, list) and len(records) <= MAX_ITEMS, "Invalid record list."
    )
    expected = {item["id"]: item["kind"] for item in items}
    supplied = {}
    for count, record in enumerate(records, 1):
        require(count <= MAX_ITEMS, "Invalid record list.")
        require(
            isinstance(record, (tuple, list)) and len(record) == 3, "Invalid record."
        )
        descriptor, descriptor_hash, document_hash = record
        require_digest(descriptor_hash)
        require_digest(document_hash)
        require(isinstance(descriptor, dict), "Invalid descriptor.")
        kind = descriptor.get("kind")
        fields = {"id", "kind", "request_sha256", "issuer", "document_sha256"}
        if kind == "retention_schedule":
            fields.add("expires_on")
        elif kind == "provider_statement":
            fields.add("outcome")
        require_versioned(descriptor, fields)
        identifier = descriptor["id"]
        require(
            gate.valid_token(identifier) and identifier in expected, "Undeclared item."
        )
        require(kind == expected[identifier], "Item kind does not match the request.")
        require(identifier not in supplied, "Duplicate supplied item.")
        require(
            descriptor["request_sha256"] == request_hash,
            "Item belongs to a different request.",
        )
        require(
            descriptor["document_sha256"] == document_hash,
            "Document digest does not match.",
        )
        require(gate.valid_token(descriptor["issuer"]), "Invalid issuer identifier.")
        row = {
            "id": identifier,
            "kind": kind,
            "issuer": descriptor["issuer"],
            "document_sha256": document_hash,
            "descriptor_sha256": descriptor_hash,
        }
        if kind == "retention_schedule":
            expiry = descriptor["expires_on"]
            require(
                isinstance(expiry, str) and ISO_DATE.fullmatch(expiry),
                "Invalid expiry date.",
            )
            try:
                date.fromisoformat(expiry)
            except ValueError:
                raise InvalidReport("Invalid expiry date.") from None
            row.update(expires_on=expiry, status="SCHEDULED")
        else:
            outcome = descriptor["outcome"]
            require(
                isinstance(outcome, str) and outcome in STATEMENT_STATUS,
                "Unknown statement outcome.",
            )
            row.update(outcome=outcome, status=STATEMENT_STATUS[outcome])
        supplied[identifier] = row
    return [supplied.get(item["id"], {**item, "status": "MISSING"}) for item in items]


def review_status(verdict, items):
    if verdict == "FAIL":
        return "RESIDUAL_FOUND"
    if verdict == "INCONCLUSIVE" or any(item["status"] != "PROVIDER_REPORTED" for item in items):
        return "OPEN_ITEMS"
    return "READY_FOR_REVIEW"


def assess(
    request,
    request_hash,
    expected_hash,
    policy,
    policy_hash,
    evidence,
    evidence_hash,
    records,
):
    validate_request(request)
    require_digest(request_hash)
    require_digest(evidence_hash)
    require(
        request_hash == expected_hash, "Request digest does not match expected digest."
    )
    require(
        policy_hash == request["policy_sha256"],
        "Policy digest does not match the request.",
    )
    require_versioned(evidence, {"job", "observations"})
    job = evidence["job"]
    require(
        keys_match(job, {"id", "request_sha256", "started_at", "observed_at"}),
        "Invalid job fields.",
    )
    require(gate.valid_token(job["id"]), "Invalid job identifier.")
    require(
        job["request_sha256"] == request_hash, "Job belongs to a different request."
    )
    require_time(job["started_at"])
    require_time(job["observed_at"])
    require(
        request["created_at"] <= job["started_at"] <= job["observed_at"],
        "Job or observations are out of order.",
    )
    decision = gate.evaluate(
        policy,
        evidence["observations"],
        expect_run=request["run_id"],
        expect_revision=request["revision"],
    )
    require(
        decision["target"] == request["target"], "Target does not match the request."
    )
    items = assess_items(request["items"], records, request_hash)
    verdict = decision["verdict"]
    return {
        "version": 1,
        "request_id": request["request_id"],
        "context": request["context"],
        "target": request["target"],
        "revision": request["revision"],
        "run_id": request["run_id"],
        "job_id": job["id"],
        "created_at": request["created_at"],
        "started_at": job["started_at"],
        "observed_at": job["observed_at"],
        "request_sha256": request_hash,
        "policy_sha256": policy_hash,
        "evidence_sha256": evidence_hash,
        "technical_verdict": verdict,
        "probes": decision["probes"],
        "items": items,
        "review_status": review_status(verdict, items),
    }
