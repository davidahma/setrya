"""Axiom v0.3.2 receipt contract tests, adapted to Setrya’s offboarding module."""

import hashlib
import json
import unittest
from copy import deepcopy

from setrya.offboarding import receipt
from setrya.offboarding.inputs import InvalidReport


def digest(value):
    return hashlib.sha256(json.dumps(value).encode("utf-8")).hexdigest()


def packet(*items):
    probe = {"id": "summary", "scope": "agent.read", "checkpoint": "after_offboarding"}
    policy = {"version": 1, "target": "desk", "probes": [probe]}
    request = {
        "version": 1,
        "request_id": "a" * 32,
        "created_at": "2025-01-20T09:00:00Z",
        "context": "customer",
        "target": "desk",
        "revision": "v3",
        "run_id": "r7",
        "policy_sha256": digest(policy),
        "items": [{"id": identifier, "kind": kind} for identifier, kind in items],
    }
    request_hash = digest(request)
    evidence = {
        "version": 1,
        "job": {
            "id": "offboard-82",
            "request_sha256": request_hash,
            "started_at": "2025-01-20T09:00:01Z",
            "observed_at": "2025-01-20T09:00:02Z",
        },
        "observations": {
            "version": 1,
            "target": "desk",
            "run_id": "r7",
            "revision": "v3",
            "offboarding": "completed",
            "probes": [
                {
                    **probe,
                    "before": {"subject": "present", "control": "present"},
                    "after": {"subject": "absent", "control": "present"},
                }
            ],
        },
    }
    return {
        "request": request,
        "request_hash": request_hash,
        "expected_hash": request_hash,
        "policy": policy,
        "policy_hash": digest(policy),
        "evidence": evidence,
        "evidence_hash": digest(evidence),
        "records": [],
    }


def record(item, request_hash):
    document_hash = hashlib.sha256(item["id"].encode("ascii")).hexdigest()
    descriptor = {
        **item,
        "version": 1,
        "issuer": "storage-provider",
        "request_sha256": request_hash,
        "document_sha256": document_hash,
    }
    if item["kind"] == "retention_schedule":
        descriptor["expires_on"] = "2024-02-29"
    else:
        descriptor["outcome"] = "reported_deleted"
    return descriptor, digest(descriptor), document_hash


class ReceiptTests(unittest.TestCase):
    def test_issuance_validates_policy_and_mints_a_new_id_for_every_request(self):
        data = packet()
        args = {
            "revision": "v3",
            "run_id": "r7",
            "context": "staging",
            "statements": ["vault"],
            "schedules": ["backups"],
        }
        first = receipt.issue(data["policy"], data["policy_hash"], **args)
        second = receipt.issue(data["policy"], data["policy_hash"], **args)
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["policy_sha256"], data["policy_hash"])
        self.assertRegex(first["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(first["context"], "staging")
        self.assertEqual(
            first["items"],
            [
                {"id": "vault", "kind": "provider_statement"},
                {"id": "backups", "kind": "retention_schedule"},
            ],
        )
        for changes in (
            {"context": "certified"},
            {"statements": "vault"},
            {"schedules": ["vault"]},
            {"run_id": False},
            {"statements": ["a"] * 257},
        ):
            with self.subTest(changes=list(changes)), self.assertRaises(InvalidReport):
                receipt.issue(data["policy"], data["policy_hash"], **(args | changes))
        with self.assertRaises(InvalidReport):
            receipt.issue({**data["policy"], "probes": []}, data["policy_hash"], **args)

    def test_replay_preserves_order_and_never_treats_elapsed_retention_as_deletion(
        self,
    ):
        data = packet(
            ("archive", "retention_schedule"), ("provider", "provider_statement")
        )
        archive, provider = data["request"]["items"]
        records = [
            record(provider, data["request_hash"]),
            record(archive, data["request_hash"]),
        ]
        data["records"] = records
        initial = deepcopy(data)
        decision = receipt.assess(**data)
        self.assertEqual(data, initial)
        self.assertEqual(decision, receipt.assess(**data))
        self.assertEqual(
            decision, receipt.assess(**(data | {"records": records[::-1]}))
        )
        self.assertEqual(decision["technical_verdict"], "PASS")
        self.assertEqual(decision["review_status"], "OPEN_ITEMS")
        self.assertEqual(
            decision["items"],
            [
                {
                    **archive,
                    "status": "SCHEDULED",
                    "expires_on": "2024-02-29",
                    "issuer": "storage-provider",
                    "descriptor_sha256": records[1][1],
                    "document_sha256": records[1][2],
                },
                {
                    **provider,
                    "status": "PROVIDER_REPORTED",
                    "outcome": "reported_deleted",
                    "issuer": "storage-provider",
                    "descriptor_sha256": records[0][1],
                    "document_sha256": records[0][2],
                },
            ],
        )

    def test_missing_and_reported_statements_remain_visible(self):
        data = packet(("records", "provider_statement"))
        item = data["request"]["items"][0]
        missing = receipt.assess(**data)
        self.assertEqual(missing["items"], [{**item, "status": "MISSING"}])
        self.assertEqual(missing["review_status"], "OPEN_ITEMS")
        descriptor, _, document_hash = record(item, data["request_hash"])
        for outcome, status, review in (
            ("reported_deleted", "PROVIDER_REPORTED", "READY_FOR_REVIEW"),
            ("reported_retained", "REPORTED_RETAINED", "OPEN_ITEMS"),
            ("unresolved", "UNRESOLVED", "OPEN_ITEMS"),
        ):
            with self.subTest(outcome=outcome):
                supplied = descriptor | {"outcome": outcome}
                data["records"] = [(supplied, digest(supplied), document_hash)]
                decision = receipt.assess(**data)
                self.assertEqual(decision["technical_verdict"], "PASS")
                self.assertEqual(decision["items"][0]["status"], status)
                self.assertEqual(decision["items"][0]["outcome"], outcome)
                self.assertEqual(decision["review_status"], review)
        for changes in (
            {"outcome": "verified_deleted"},
            {"outcome": []},
            {"expires_on": "2024-01-01"},
        ):
            with self.subTest(changes=changes), self.assertRaises(InvalidReport):
                supplied = descriptor | changes
                receipt.assess(
                    **(
                        data
                        | {"records": [(supplied, digest(supplied), document_hash)]}
                    )
                )
        del descriptor["outcome"]
        with self.assertRaises(InvalidReport):
            receipt.assess(
                **(
                    data
                    | {"records": [(descriptor, digest(descriptor), document_hash)]}
                )
            )
        self.assertEqual(
            receipt.assess(**packet())["review_status"], "READY_FOR_REVIEW"
        )

    def test_control_and_completion_gaps_cannot_be_cleared_by_provider_records(self):
        for status, subject, control, verdict, review in (
            ("completed", "absent", "absent", "INCONCLUSIVE", "OPEN_ITEMS"),
            ("unconfirmed", "present", "present", "INCONCLUSIVE", "OPEN_ITEMS"),
            ("completed", "present", "unavailable", "FAIL", "RESIDUAL_FOUND"),
        ):
            with self.subTest(status=status, subject=subject, control=control):
                obligations = [("provider", "provider_statement")]
                if verdict == "FAIL":
                    obligations.append(("archive", "retention_schedule"))
                data = packet(*obligations)
                data["records"] = [
                    record(data["request"]["items"][0], data["request_hash"])
                ]
                observation = data["evidence"]["observations"]
                observation["offboarding"] = status
                observation["probes"][0]["after"].update(
                    subject=subject, control=control
                )
                data["evidence_hash"] = digest(data["evidence"])
                decision = receipt.assess(**data)
                self.assertEqual(decision["technical_verdict"], verdict)
                self.assertEqual(decision["review_status"], review)
                self.assertEqual(decision["probes"][0]["verdict"], verdict)

    def test_a_new_request_on_the_same_build_cannot_reuse_an_old_job(self):
        data = packet()
        data["request"]["request_id"] = "b" * 32
        data["request_hash"] = digest(data["request"])
        with self.assertRaisesRegex(InvalidReport, "expected digest"):
            receipt.assess(**data)
        data["expected_hash"] = data["request_hash"]
        with self.assertRaisesRegex(InvalidReport, "Job belongs"):
            receipt.assess(**data)

    def test_evidence_requires_consistent_job_and_application_identity(self):
        for key, value in (
            ("revision", "other"),
            ("target", "other"),
            ("run_id", "other"),
        ):
            with self.subTest(key=key), self.assertRaises(InvalidReport):
                data = packet()
                data["evidence"]["observations"][key] = value
                receipt.assess(**data)
        data = packet()
        data["request"]["target"] = "different"
        data["request_hash"] = data["expected_hash"] = digest(data["request"])
        data["evidence"]["job"]["request_sha256"] = data["request_hash"]
        with self.assertRaisesRegex(InvalidReport, "Target"):
            receipt.assess(**data)
        data = packet()
        for overrides in (
            {"policy_hash": digest("another policy")},
            {"expected_hash": None},
            {"request_hash": []},
            {"evidence_hash": "short"},
            {"evidence": data["evidence"]["observations"]},
        ):
            with self.subTest(keys=list(overrides)), self.assertRaises(InvalidReport):
                receipt.assess(**(data | overrides))

    def test_job_times_are_real_utc_seconds_in_request_start_observation_order(self):
        for field, value in (
            ("started_at", "2025-01-19T09:00:00Z"),
            ("observed_at", "2025-01-20T09:00:00Z"),
            ("observed_at", "2025-02-30T09:00:02Z"),
            ("observed_at", "2025-01-20T09:00:02+00:00"),
            ("started_at", False),
            ("observed_at", "2025-01-20T09:00:02.005Z"),
        ):
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(InvalidReport),
            ):
                data = packet()
                data["evidence"]["job"][field] = value
                receipt.assess(**data)
        data = packet()
        for field in ("started_at", "observed_at"):
            data["evidence"]["job"][field] = data["request"]["created_at"]
        self.assertEqual(receipt.assess(**data)["review_status"], "READY_FOR_REVIEW")

    def test_request_and_evidence_reject_extra_claims_and_malformed_types(self):
        invalid_requests = (
            {"version": True},
            {"request_id": "A" * 32},
            {"policy_sha256": "X" * 64},
            {"context": ["staging"]},
            {"target": 1},
            {"items": {}},
            {"certified": True},
            {"items": [{"id": "vault", "kind": "verified_deletion"}]},
            {"items": [{"id": [], "kind": "provider_statement"}]},
        )
        for changes in invalid_requests:
            with self.subTest(changes=changes), self.assertRaises(InvalidReport):
                receipt.validate_request(packet()["request"] | changes)
        for key, value in (
            ("job", []),
            ("signature_verified", True),
            ("version", False),
            ("observations", []),
        ):
            with self.subTest(key=key), self.assertRaises(InvalidReport):
                data = packet()
                data["evidence"][key] = value
                receipt.assess(**data)

    def test_records_require_bound_descriptors_unique_ids_and_matching_document_bytes(
        self,
    ):
        data = packet(("archive", "retention_schedule"))
        descriptor, descriptor_hash, document_hash = record(
            data["request"]["items"][0], data["request_hash"]
        )
        invalid = [
            None,
            [None],
            [(descriptor, descriptor_hash)],
            [(None, descriptor_hash, document_hash)],
            [(descriptor, "bad", document_hash)],
            [(descriptor, descriptor_hash, digest("wrong bytes"))],
            [(descriptor, descriptor_hash, document_hash)] * 2,
        ]
        for changes in (
            {"id": "undeclared"},
            {"id": []},
            {"issuer": "raw issuer name"},
            {"certified": True},
            {"version": True},
            {"kind": "physical_erasure"},
            {"request_sha256": digest("other request")},
            {"expires_on": "2025-02-29"},
            {"outcome": "reported_deleted"},
            {"expires_on": 20250101},
            {"expires_on": "2024-02-29T00:00:00Z"},
        ):
            changed = descriptor | changes
            invalid.append([(changed, digest(changed), document_hash)])
        for index, records in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(InvalidReport):
                receipt.assess(**(data | {"records": records}))
        statement = {
            **descriptor,
            "kind": "provider_statement",
            "outcome": "reported_deleted",
        }
        del statement["expires_on"]
        with self.assertRaisesRegex(InvalidReport, "kind"):
            receipt.assess(
                **(data | {"records": [(statement, digest(statement), document_hash)]})
            )
