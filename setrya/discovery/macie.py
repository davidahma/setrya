"""Read one Macie S3 classification finding without exporting its object name or payload."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from setrya.offboarding.inputs import InvalidReport, keys_match, read_json, require


FORMAT = "setrya.discovery.macie"
CATALOG = "aws-macie"
IDENTITY = ("finding_id", "asset_sha256", "finding_type", "observed_at")
_TYPES = {"SensitiveData:S3Object/" + name for name in (
    "Credentials", "CustomIdentifier", "Financial", "Multiple", "Personal")}
_CATEGORIES = ("CREDENTIALS", "CUSTOM_IDENTIFIER", "FINANCIAL_INFORMATION", "PERSONAL_INFORMATION")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _utc(value):
    require(_matches(_TIME, value), "Macie finding needs an ISO 8601 timestamp with a time zone.")
    try:
        instant = datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidReport("Invalid Macie finding timestamp.") from None
    return instant.isoformat().replace("+00:00", "Z")


def _digest(value):
    try:
        data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (RecursionError, TypeError, ValueError):
        raise InvalidReport("Input must be a finite JSON value.") from None
    return sha256(data.encode("ascii")).hexdigest()


def _object_token(value):
    return isinstance(value, str) and len(value) <= 1024 and value.isprintable() and value != ""


def asset_fingerprint(partition, account, region, bucket_arn, key, version_id="", etag=""):
    """Fingerprint an exact opaque S3 object generation for a privately reviewed mapping."""
    require(isinstance(partition, str) and re.fullmatch(r"aws(?:-[a-z0-9]+)*", partition)
            and isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account)
            and isinstance(region, str) and re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d+", region),
            "Invalid Macie AWS account, region or partition.")
    require(isinstance(bucket_arn, str)
            and re.fullmatch(r"arn:" + re.escape(partition) + r":s3:::[A-Za-z0-9._-]{1,255}", bucket_arn),
            "Expected an exact S3 bucket ARN in the finding partition.")
    try:
        key_bytes = key.encode("utf-8") if isinstance(key, str) else b""
    except UnicodeError:
        key_bytes = b""
    require(0 < len(key_bytes) <= 1024, "Expected an S3 object key of up to 1024 UTF-8 bytes.")
    require((version_id == "" or _object_token(version_id)) and (etag == "" or _object_token(etag)),
            "Invalid S3 object generation.")
    require(version_id not in ("", "null") or etag, "The S3 object needs a version ID or ETag.")
    return _digest(["setrya.macie.asset.v1", partition, account, region, bucket_arn, key, version_id, etag])


def validate_identity(value):
    require(_matches(_ID, value.get("finding_id")), "Invalid Macie finding ID.")
    require(_matches(_HASH, value.get("asset_sha256")), "Invalid Macie asset fingerprint.")
    require(isinstance(value.get("finding_type"), str) and value["finding_type"] in _TYPES,
            "Expected an S3 sensitive-data finding type.")
    require(_utc(value.get("observed_at")) == value["observed_at"], "Use canonical UTC for the finding time.")


def validate_categories(values):
    require(isinstance(values, list) and all(isinstance(c, str) and c in _CATEGORIES for c in values)
            and values == sorted(set(values)), "Expected sorted, distinct Macie data categories.")


def _categories(result):
    items = result.get("sensitiveData")
    require(items is None or isinstance(items, list), "Invalid Macie classification entries.")
    found = set()
    for item in items or ():
        require(isinstance(item, dict), "Invalid Macie classification entry.")
        if item.get("category") in _CATEGORIES:
            found.add(item["category"])
    custom = result.get("customDataIdentifiers")
    if custom is not None:
        require(isinstance(custom, dict), "Invalid Macie custom identifier summary.")
        count = custom.get("totalCount")
        if type(count) is int and count > 0:
            found.add("CUSTOM_IDENTIFIER")
    return sorted(found)


def _validate_import(value):
    require(keys_match(value, ("format", "version", "catalog", "sample", *IDENTITY, "finding_sha256", "categories"))
            and value["format"] == FORMAT and value["catalog"] == CATALOG and type(value["version"]) is int and value["version"] == 1,
            "Invalid Macie import format.")
    require(value["sample"] is None or type(value["sample"]) is bool, "Invalid Macie sample indicator.")
    validate_identity(value)
    validate_categories(value["categories"])
    require(_matches(_HASH, value["finding_sha256"]), "Invalid Macie finding digest.")
    return value.copy()


def _unwrap(data):
    require(isinstance(data, dict), "Expected one Macie finding or export.")
    if "detail" in data or "detail-type" in data:
        finding = data.get("detail")
        require(isinstance(finding, dict) and data.get("source") == "aws.macie"
                and data.get("detail-type") == "Macie Finding" and data.get("version") == "0"
                and data.get("account") == finding.get("accountId") and data.get("region") == finding.get("region")
                and not {"findings", "category", "schemaVersion"}.intersection(data),
                "Macie EventBridge envelope does not match its finding.")
        return finding
    if "findings" in data:
        rows = data["findings"]
        require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
                and not {"category", "schemaVersion"}.intersection(data),
                "Export exactly one finding from GetFindings.")
        return rows[0]
    return data


def _field(row, name):
    result = row.get(name)
    require(isinstance(result, dict), "Macie finding lacks the expected classification or S3 object details.")
    return result


def parse_finding(data):
    """Normalize a raw finding, single GetFindings response, EventBridge event, or v1 import."""
    if isinstance(data, dict) and "format" in data:
        return _validate_import(data)
    row = _unwrap(data)
    require(row.get("schemaVersion") == "1.0" and row.get("category") == "CLASSIFICATION",
            "Expected a Macie 1.0 classification finding.")
    require(row.get("archived") is False, "The finding must explicitly be active.")
    result = _field(_field(row, "classificationDetails"), "result")
    require(_field(result, "status").get("code") == "COMPLETE", "Macie did not complete this object scan.")
    resources = _field(row, "resourcesAffected")
    obj = _field(resources, "s3Object")
    bucket = obj.get("bucketArn")
    if resources.get("s3Bucket") is not None:
        b = _field(resources, "s3Bucket")
        if bucket is None:
            bucket = b.get("arn")
        require(isinstance(bucket, str) and (b.get("arn") is None or b["arn"] == bucket)
                and (b.get("name") is None or b["name"] == bucket.split(":::")[-1]), "Conflicting S3 bucket identities.")
    key = obj.get("key")
    if obj.get("path") is not None:
        require(isinstance(bucket, str) and isinstance(obj["path"], str), "Invalid Macie object path.")
        prefix = bucket.split(":::")[-1] + "/"
        require(obj["path"].startswith(prefix), "Conflicting S3 object paths.")
        path_key = obj["path"][len(prefix):]
        require(key is None or key == path_key, "Conflicting S3 object paths.")
        key = path_key
    version = obj.get("versionId")
    etag = obj.get("etag")
    asset = asset_fingerprint(row.get("partition"), row.get("accountId"), row.get("region"), bucket, key,
                              "" if version is None else version, "" if etag is None else etag)
    return _validate_import({"format": FORMAT, "version": 1, "catalog": CATALOG, "finding_id": row.get("id"), "asset_sha256": asset,
                             "finding_type": row.get("type"), "observed_at": _utc(row.get("createdAt")),
                             "finding_sha256": _digest(row), "categories": _categories(result), "sample": row.get("sample")})


def load_finding(path):
    data, _ = read_json(path, "Macie finding")
    return parse_finding(data)
