"""An export locates one approved fixture source and cannot authorize its own policy."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from urllib.parse import quote

from setrya.discovery.macie import asset_fingerprint, load_finding, parse_finding
from setrya.discovery.mapping import bind_finding, load_mapping, validate_binding


def raw_finding():
    bucket = "arn:aws:s3:::synthetic-internal"
    return {
        "schemaVersion": "1.0", "category": "CLASSIFICATION", "id": "macie-test-203",
        "accountId": "111122223333", "region": "us-east-1", "partition": "aws",
        "type": "SensitiveData:S3Object/Personal", "createdAt": "2026-11-01T01:00:00+01:00",
        "archived": False, "sample": True, "description": "never-export-this-description",
        "classificationDetails": {"result": {"status": {"code": "COMPLETE"}, "sensitiveData": [
            {"occurrences": {"records": [{"jsonPath": "never-export-this-column"}]}}]}},
        "resourcesAffected": {"s3Bucket": {"arn": bucket, "name": "synthetic-internal"},
                              "s3Object": {"bucketArn": bucket, "key": "records/private name + 4.json",
                                           "versionId": "synthetic-version", "etag": "synthetic-etag"}},
    }


def trusted_mapping():
    fingerprint = asset_fingerprint("aws", "111122223333", "us-east-1", "arn:aws:s3:::synthetic-internal",
                                    "records/private name + 4.json", "synthetic-version", "synthetic-etag")
    imported = parse_finding(raw_finding())
    return {
        "catalog": "aws-macie", "sample": True, "finding_sha256": imported["finding_sha256"], "categories": [],
        "format": "setrya.discovery.mapping", "version": 1, "finding_id": "macie-test-203",
        "finding_type": "SensitiveData:S3Object/Personal", "observed_at": "2026-11-01T00:00:00Z",
        "asset_sha256": fingerprint, "case_id": "case-21", "environment": "tenant-fixture", "revision": "1" * 40,
        "subject_ref": "a" * 64, "classification": "restricted", "conversation": "case-21",
        "callers": ["analyst-1", "analyst-2"], "route": "/investigations/21", "questions": ["e" * 64, "f" * 64],
        "source": {"store": "findings", "key": "finding-174", "revision": "v3", "action": "delete"},
        "services": [
            {"id": "writer", "owner": "security", "role": "source", "store": "findings", "key": "finding-174",
             "action": "delete"},
            {"id": "assistant", "owner": "eng", "role": "endpoint", "route": "/investigations/21"},
        ],
    }


class MacieImportTests(unittest.TestCase):
    def test_three_aws_shapes_keep_only_exportable_metadata(self):
        f = raw_finding()
        event = {"version": "0", "source": "aws.macie", "detail-type": "Macie Finding", "account": f["accountId"],
                 "region": f["region"], "detail": f, "time": "2030-01-01T00:00:00Z", "id": "envelope-only"}
        out = parse_finding(f)
        self.assertEqual(out, parse_finding(event))
        self.assertEqual(out, parse_finding({"findings": [f]}))
        self.assertEqual(out, parse_finding(out))
        self.assertEqual(out["observed_at"], "2026-11-01T00:00:00Z")
        self.assertEqual(out["finding_id"], f["id"])
        self.assertIs(out["sample"], True)
        self.assertIs(parse_finding({**f, "sample": False})["sample"], False)
        self.assertIsNone(parse_finding({k: v for k, v in f.items() if k != "sample"})["sample"])
        with self.assertRaises(ValueError):
            parse_finding({**f, "sample": 1})
        self.assertEqual(out["finding_sha256"], sha256(json.dumps(f, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        for private in ("private name", "synthetic-internal", "never-export", "111122223333", "synthetic-version"):
            self.assertNotIn(private, json.dumps(out))

    def test_multiple_category_event_forwards_only_documented_types(self):
        f = raw_finding()
        f["type"] = "SensitiveData:S3Object/Multiple"
        result = f["classificationDetails"]["result"]
        result["sensitiveData"] = [
            {"category": "PERSONAL_INFORMATION", "totalCount": 65, "detections": [
                {"type": "NAME", "count": 35, "occurrences": {"cells": [{"row": 2, "columnName": "Name"}]}}]},
            {"category": "FINANCIAL_INFORMATION", "totalCount": 30},
            {"category": "NEVER_EXPORT_CUSTOM_NAMES", "totalCount": 1},
        ]
        result["customDataIdentifiers"] = {"totalCount": 0, "detections": []}
        normalized = parse_finding(f)
        self.assertEqual(normalized["categories"], ["FINANCIAL_INFORMATION", "PERSONAL_INFORMATION"])
        result["customDataIdentifiers"] = {"totalCount": 1, "detections": [{"name": "private-rule"}]}
        self.assertEqual(parse_finding(f)["categories"], ["CUSTOM_IDENTIFIER", *normalized["categories"]])
        supplied = {**trusted_mapping(), "finding_type": f["type"], "categories": ["CREDENTIALS"],
                    "finding_sha256": normalized["finding_sha256"]}
        with self.assertRaisesRegex(ValueError, "data categories"):
            bind_finding(normalized, supplied)
        self.assertEqual(parse_finding(raw_finding())["categories"], [])

    def test_normalized_handoff_must_match_the_privately_reviewed_digest_and_categories(self):
        normalized = parse_finding(raw_finding())
        mapping = trusted_mapping()
        for field, altered in (("catalog", "another-provider"), ("sample", False), ("finding_sha256", "2" * 64),
                               ("categories", ["PERSONAL_INFORMATION"])):
            with self.subTest(field=field):
                changed = {**normalized, field: altered}
                with self.assertRaises(ValueError):
                    bind_finding(changed, mapping)
                incomplete = {k: v for k, v in mapping.items() if k != field}
                with self.assertRaises(ValueError):
                    bind_finding(normalized, incomplete)
        self.assertEqual(bind_finding(normalized, mapping)["catalog"], "aws-macie")

    def test_exact_object_identity_requires_revision_and_no_decoding(self):
        f = raw_finding()
        obj = f["resourcesAffected"]["s3Object"]
        right = parse_finding(f)["asset_sha256"]
        for key, value in (("key", quote(obj["key"], safe="/+")), ("key", obj["key"].upper()),
                           ("versionId", "other"), ("etag", "new")):
            with self.subTest(key=key, value=value):
                changed = deepcopy(f)
                changed["resourcesAffected"]["s3Object"][key] = value
                self.assertNotEqual(parse_finding(changed)["asset_sha256"], right)
                with self.assertRaisesRegex(ValueError, "asset_sha256"):
                    bind_finding(changed, trusted_mapping())
        obj["path"] = "synthetic-internal/" + obj.pop("key")
        del obj["bucketArn"]
        self.assertEqual(parse_finding(f)["asset_sha256"], right)
        obj["versionId"] = "null"
        del obj["etag"]
        with self.assertRaisesRegex(ValueError, "version ID or ETag"):
            parse_finding(f)

    def test_missing_partial_or_ambiguous_finding_rejected(self):
        for k, val in (("archived", True), ("category", "POLICY"), ("type", "Policy:IAM/foo"),
                       ("createdAt", "2026-11-01"), ("createdAt", "2026-99-01T00:00:00Z")):
            with self.subTest(k=k, val=val), self.assertRaises(ValueError):
                parse_finding({**raw_finding(), k: val})
        partial = raw_finding()
        partial["classificationDetails"]["result"]["status"]["code"] = "PARTIAL"
        for data in (partial, {"findings": [raw_finding(), raw_finding()]}, {"findings": []},
                     {"source": "aws.macie", "detail-type": "Macie Finding", "detail": raw_finding()},
                     {"findings": [raw_finding()], "category": "POLICY"},
                     {**parse_finding(raw_finding()), "description": "untrusted content"}):
            with self.subTest(keys=list(data)), self.assertRaises(ValueError):
                parse_finding(data)
        conflict = raw_finding()
        conflict["resourcesAffected"]["s3Object"]["path"] = "synthetic-internal/another-key"
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            parse_finding(conflict)

    def test_bounded_file_reads_and_roundtrip(self):
        with TemporaryDirectory() as folder:
            src = Path(folder) / "source.json"
            src.write_text('{"id":"a","id":"b"}')
            with self.assertRaisesRegex(ValueError, "unique fields"):
                load_finding(src)
            src.write_text(" " * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, "exceeds"):
                load_finding(src)
            src.write_text(json.dumps(raw_finding()))
            imported = load_finding(src)
            src.write_text(json.dumps(imported))
            self.assertEqual(load_finding(src), imported)
            src.write_text(json.dumps(trusted_mapping()))
            self.assertEqual(load_mapping(src), trusted_mapping())


class TrustedMappingTests(unittest.TestCase):
    def test_binds_original_finding_digest_and_stale_or_other_assets_fail(self):
        normal = parse_finding(raw_finding())
        mapping = trusted_mapping()
        bound = bind_finding(normal, mapping)
        self.assertEqual(bound["finding_sha256"], normal["finding_sha256"])
        self.assertEqual(validate_binding(bound), bound)
        self.assertEqual(mapping, trusted_mapping())
        for field, other in (("finding_id", "other"), ("finding_type", "SensitiveData:S3Object/Multiple"),
                             ("observed_at", "2026-11-02T00:00:00Z"), ("asset_sha256", "0" * 64),
                             ("finding_sha256", "0" * 64)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "does not match"):
                bind_finding(normal, {**mapping, field: other})
        bound["services"][0]["owner"] = "another"
        self.assertEqual(mapping["services"][0]["owner"], "security")

    def test_non_mutating_retention_requires_scope_owner_exhibit_date(self):
        mapping = trusted_mapping()
        mapping["source"]["action"] = "retain"
        row = mapping["services"][0]
        row.update(action="retain", evidence="policy-item-5", follow_up="2027-01-01")
        bound = bind_finding(raw_finding(), mapping)
        self.assertEqual(bound["source"]["action"], "retain")
        for key in ("owner", "evidence", "follow_up"):
            invalid = deepcopy(mapping)
            invalid["services"][0].pop(key)
            with self.subTest(field=key), self.assertRaises(ValueError):
                bind_finding(raw_finding(), invalid)

    def test_ambiguous_services_and_public_binding_are_checked(self):
        mapping = trusted_mapping()
        for contaminant in (
            {**mapping["services"][0], "id": "different"},
            {"id": "provider", "role": "provider", "store": "findings", "key": "unrelated", "owner": "team",
             "action": "external"},
            {"id": "unknown", "role": "endpoint", "route": "/different", "owner": "team"},
        ):
            with self.subTest(id=contaminant["id"]), self.assertRaises(ValueError):
                bind_finding(raw_finding(), {**mapping, "services": [*mapping["services"], contaminant]})
        bound = bind_finding(raw_finding(), mapping)
        for key, value in (("format", "setrya.discovery.mapping"), ("version", True), ("revision", "main"),
                           ("services", []), ("callers", ["one", "one"]), ("questions", ["e" * 64] * 2),
                           ("source", {**bound["source"], "action": "retain"})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_binding({**bound, key: value})
