"""Declared erasure boundary, handling labels and owners."""
import tomllib
from pathlib import Path

from setrya.offboarding.inputs import read_regular_bytes, require
from .journal import identifier


def label(value):
    require(isinstance(value, str) and value.strip() and value.isprintable() and len(value) <= 128,
            "Expected a printable classification label.")
    return value


def systems(value):
    require(isinstance(value, list) and 0 < len(value) <= 256, "Declare one to 256 inventory systems.")
    result = {}
    for row in value:
        require(isinstance(row, dict) and set(row) == {
            "name", "owner", "role", "approved_labels", "retention_days", "adapter"
        }, "Invalid inventory system fields.")
        for key in ("name", "owner", "role", "adapter"):
            identifier(row[key], key)
        labels = row["approved_labels"]
        require(isinstance(labels, list) and labels and all(label(x) for x in labels)
                and len(set(labels)) == len(labels), "List each approved handling label once.")
        require(type(row["retention_days"]) is int and row["retention_days"] >= 0,
                "Expected nonnegative retention days.")
        require(row["name"] not in result, "Duplicate inventory system.")
        result[row["name"]] = row
    bound = [row["adapter"] for row in result.values() if row["adapter"] != "none"]
    require(len(bound) == len(set(bound)), "Give each deletion boundary a distinct adapter name.")
    return result


def load(path: Path):
    value = tomllib.loads(read_regular_bytes(path, "inventory").decode("utf-8"))
    require(set(value) == {"version", "systems"} and type(value["version"]) is int and value["version"] == 1,
            "Expected inventory version 1.")
    return systems(value["systems"])
