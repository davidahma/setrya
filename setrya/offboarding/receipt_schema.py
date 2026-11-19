"""Validate the closed receipt v1 schema without dependencies in the portable CLI."""

from datetime import date, datetime
from importlib.resources import files
import json
import re

from .gate import technical_verdict
from .inputs import InvalidReport, require
from .receipt import review_status


def validate_receipt(value):
    schema = json.loads(files(__package__).joinpath("receipt-v1.schema.json").read_bytes())
    if not _matches(value, schema, schema["$defs"]):
        raise InvalidReport("Receipt does not match the published v1 schema.")

    probes, items = value["probes"], value["items"]
    for rows in (probes, items):
        require(len({row["id"] for row in rows}) == len(rows), "Duplicate receipt identifiers.")
    require(value["created_at"] <= value["started_at"] <= value["observed_at"],
            "Receipt timestamps contradict the request and job order.")
    require(all((row["verdict"] == "PASS") == (not row["reasons"]) for row in probes),
            "Receipt probe verdict disagrees with its reasons.")
    verdict = technical_verdict(probes)
    require(value["technical_verdict"] == verdict and value["review_status"] == review_status(verdict, items),
            "Receipt summary disagrees with its probes or exhibits.")


def _matches(value, rule, definitions):
    # Evaluate only the vocabulary in our packaged, immutable schema. The caller
    # supplies receipt data, never a schema or a reference URI.
    if "$ref" in rule:
        return _matches(value, definitions[rule["$ref"].removeprefix("#/$defs/")], definitions)
    types = {"object": dict, "array": list, "integer": int, "string": str}
    if "type" in rule and type(value) is not types[rule["type"]]:
        return False
    if "const" in rule and value != rule["const"]:
        return False
    if "enum" in rule and value not in rule["enum"]:
        return False
    if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
        return False
    if "format" in rule:
        parse = {"date": date.fromisoformat, "date-time": datetime.fromisoformat}[rule["format"]]
        try:
            parse(value)
        except ValueError:
            return False
    if "properties" in rule:
        fields = rule["properties"]
        if not isinstance(value, dict) or not value.keys() >= set(rule.get("required", ())):
            return False
        if rule.get("additionalProperties") is False and value.keys() - fields.keys():
            return False
        if any(not _matches(value[key], fields[key], definitions) for key in value.keys() & fields.keys()):
            return False
    if "items" in rule:
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", len(value)):
            return False
        if any(not _matches(item, rule["items"], definitions) for item in value):
            return False
    if "oneOf" in rule:
        return sum(_matches(value, choice, definitions) for choice in rule["oneOf"]) == 1
    return True
