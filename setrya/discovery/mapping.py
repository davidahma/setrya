"""Bind one Macie finding to the exact fixture, approved actions and services in a reviewed manifest."""
from copy import deepcopy
from datetime import date
import re

from setrya.erasure.journal import identifier
from setrya.erasure.model import route_id
from setrya.erasure.subject import Subject
from setrya.offboarding.inputs import keys_match, read_json, require

from .macie import CATALOG, IDENTITY, parse_finding, validate_categories, validate_identity


MAPPING = "setrya.discovery.mapping"
BINDING = "setrya.discovery.binding"
DIRECT_ROLES = ("source", "summary", "checkpoint", "cache", "memory", "vector", "store")
EXTERNAL_ROLES = ("provider", "external")
_FIELDS = {"format", "version", "catalog", "sample", *IDENTITY, "finding_sha256", "categories", "case_id", "subject_ref", "classification", "environment",
           "revision", "conversation", "route", "callers", "questions", "source", "services"}


def _service(row, source):
    require(isinstance(row, dict), "Expected a service declaration.")
    for field in ("id", "owner"):
        identifier(row.get(field), field)
    role = row.get("role")
    if role == "endpoint":
        require(keys_match(row, ("id", "role", "owner", "route")), "Invalid endpoint service fields.")
        route_id(row["route"])
        return
    require(role in (*DIRECT_ROLES, *EXTERNAL_ROLES), "Unknown discovery service role.")
    fields = {"id", "role", "owner", "store", "key", "action"}
    action = row.get("action")
    for field in ("store", "key"):
        identifier(row.get(field), field)
    if role in EXTERNAL_ROLES:
        require(action == "external", "External services need an external review item.")
    elif action == "retain":
        require(role == "source" and source["action"] == "retain" and
                (row["store"], row["key"]) == (source["store"], source["key"]),
                "Retention must refer to the mapped source.")
        fields.update(("follow_up", "evidence"))
        identifier(row.get("evidence"), "retention policy exhibit")
        try:
            valid_date = date.fromisoformat(row.get("follow_up")).isoformat() == row.get("follow_up")
        except (ValueError, TypeError):
            valid_date = False
        require(valid_date, "Retention needs a YYYY-MM-DD review date.")
    else:
        require(action in ("delete", "rewrite", "preserve"), "Unsupported direct service action.")
    require(keys_match(row, fields), "Invalid discovery service fields.")


def _check(value, expected_format):
    require(isinstance(value, dict), "Expected a discovery manifest.")
    require(keys_match(value, _FIELDS) and value["format"] == expected_format
            and value["catalog"] == CATALOG and type(value["version"]) is int and value["version"] == 1,
            "Invalid discovery manifest format or fields.")
    require(value["sample"] is None or type(value["sample"]) is bool, "Invalid Macie sample indicator.")
    validate_identity(value)
    validate_categories(value["categories"])
    digest = value["finding_sha256"]
    require(isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest), "Invalid Macie finding digest.")
    for field in ("case_id", "conversation", "environment"):
        identifier(value[field], field)
    Subject(value["subject_ref"], value["classification"])
    require(isinstance(value["revision"], str) and re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}", value["revision"]),
            "Expected a full Git commit ID for the application revision.")
    route_id(value["route"])
    callers = value["callers"]
    require(isinstance(callers, list) and len(callers) == 2, "Declare both authenticated callers.")
    for caller in callers:
        identifier(caller, "caller")
    require(callers[0] != callers[1], "Expected distinct authenticated callers.")
    questions = value["questions"]
    require(isinstance(questions, list) and len(questions) == 2
            and all(isinstance(q, str) and re.fullmatch(r"[0-9a-f]{64}", q) for q in questions)
            and questions[0] != questions[1], "Expected distinct target and control question digests.")
    source = value["source"]
    require(keys_match(source, ("store", "key", "revision", "action")), "Invalid mapped source fields.")
    for field in ("store", "key", "revision"):
        identifier(source[field], field)
    require(source["action"] in ("delete", "rewrite", "retain"), "Expected an approved source handling action.")
    services = value["services"]
    require(isinstance(services, list) and 2 <= len(services) <= 256, "Expected two to 256 services.")
    for row in services:
        _service(row, source)
    require(len({row["id"] for row in services}) == len(services), "Duplicate service ID.")
    endpoints = [row["route"] for row in services if row["role"] == "endpoint"]
    require(endpoints == [value["route"]], "Declare the mapped answer route exactly once.")
    located = [row for row in services if row["role"] != "endpoint"]
    require(len({(r["store"], r["key"]) for r in located}) == len(located), "Duplicate service location.")
    external_stores = {row["store"] for row in located if row["role"] in EXTERNAL_ROLES}
    require(all(row["store"] not in external_stores for row in located if row["role"] in DIRECT_ROLES),
            "An application store cannot also be an external exception.")
    require(any(row["role"] == "source" and row["store"] == source["store"] and row["key"] == source["key"] and
                row["action"] == source["action"] for row in located), "Missing exact service for the approved source.")


def load_mapping(path):
    value, _ = read_json(path, "discovery mapping")
    _check(value, MAPPING)
    return value


def bind_finding(finding, manifest):
    """Validate a privately reviewed mapping before dispatching any application callback."""
    finding = parse_finding(finding)
    _check(manifest, MAPPING)
    for field in IDENTITY:
        require(finding[field] == manifest[field], f"Macie finding does not match the approved {field}.")
    require(manifest["finding_sha256"] == finding["finding_sha256"],
            "Macie finding does not match the approved finding digest.")
    require(manifest["categories"] == finding["categories"],
            "Macie finding does not match the approved data categories.")
    require(manifest["sample"] is finding["sample"], "Macie finding does not match the approved sample indicator.")
    return {**deepcopy(manifest), "format": BINDING}


def validate_binding(value):
    """Check a public v1 binding. Trust comes from the reviewed mapping or separately verified receipt."""
    _check(value, BINDING)
    return value
