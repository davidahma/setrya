"""Read files, create deletion receipts, and verify their contents offline.

Adapted from Axiom v0.3.2 for Setrya's offboarding interface.
"""

import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import receipt
from .inputs import InvalidReport, publish_new, read_json, regular_file_opener

COMMANDS = ("request", "receipt", "verify-receipt")
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_CLOCK_SKEW = timedelta(minutes=5)


def add_commands(jobs):
    request = jobs.add_parser("request", help="issue a request before offboarding")
    request.add_argument("--policy", type=Path, required=True)
    request.add_argument("--revision", required=True)
    request.add_argument("--run-id", required=True)
    request.add_argument("--context", choices=("staging", "customer"), required=True)
    request.add_argument("--statement", metavar="ID", action="append", default=[])
    request.add_argument("--schedule", metavar="ID", action="append", default=[])
    request.add_argument("--output", type=Path, required=True)
    for name, help_text in (
        ("receipt", "assess the evidence and write a new receipt"),
        ("verify-receipt", "recompute an existing receipt from its original evidence"),
    ):
        command = jobs.add_parser(name, help=help_text)
        command.add_argument("--request", type=Path, required=True)
        command.add_argument(
            "--expect-request",
            metavar="SHA256",
            required=True,
            help="request digest kept in the original offboarding ticket",
        )
        command.add_argument(
            "--expect-job",
            metavar="ID",
            help="require the job ID from the application dispatch log",
        )
        command.add_argument("--policy", type=Path, required=True)
        command.add_argument("--evidence", type=Path, required=True)
        command.add_argument(
            "--record",
            nargs=2,
            type=Path,
            metavar=("DESCRIPTOR", "DOCUMENT"),
            action="append",
            default=[],
        )
        command.add_argument(
            "--output" if name == "receipt" else "--receipt", required=True, type=Path
        )


def document_digest(path, budget):
    digest = hashlib.sha256()
    with open(path, "rb", opener=regular_file_opener) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_DOCUMENT_BYTES:
            raise InvalidReport("A record document exceeds the 32 MiB limit.")
        if os.fstat(stream.fileno()).st_size > budget:
            raise InvalidReport("Record documents exceed the 64 MiB aggregate limit.")
        limit = min(MAX_DOCUMENT_BYTES, budget)
        remaining = limit
        while chunk := stream.read(min(128 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise InvalidReport("A record document exceeds the 32 MiB limit.")
            digest.update(chunk)
        if remaining == limit:
            raise InvalidReport("A record document is empty.")
    return digest.hexdigest(), limit - remaining


def print_decision(report, *, verified):
    if verified:
        print("Receipt verified. Replay matches the supplied receipt.")
    print(
        f"Request {report['request_id']}   Context {report['context']}   Target {report['target']}"
    )
    print(
        f"Job {report['job_id']}   Run {report['run_id']}   Revision {report['revision']}"
    )
    print(f"Started {report['started_at']}   Observed {report['observed_at']}")
    print(f"Technical verdict {report['technical_verdict']}")
    for probe in report["probes"]:
        if probe["verdict"] != "PASS":
            print(
                probe["verdict"],
                probe["id"],
                probe["scope"],
                probe["checkpoint"],
                *probe["reasons"],
            )
    for item in report["items"]:
        details = [
            f"{key}={item[key]}" for key in ("issuer", "expires_on") if key in item
        ]
        print(item["status"], item["id"], item["kind"], *details)
    print(f"Review status {report['review_status']}")


def run(args):
    try:
        policy, policy_hash = read_json(args.policy, "policy")
        if args.offboarding_action == "request":
            request = receipt.issue(
                policy,
                policy_hash,
                revision=args.revision,
                run_id=args.run_id,
                context=args.context,
                statements=args.statement,
                schedules=args.schedule,
            )
            digest = publish_new(request, args.output)
            print(f"Request SHA-256 {digest}")
            return 0

        if len(args.record) > receipt.MAX_ITEMS:
            raise InvalidReport("Too many record attachments (maximum 256).")
        request, request_hash = read_json(args.request, "request")
        if request_hash != args.expect_request:
            raise InvalidReport("Request digest does not match expected digest.")
        evidence, evidence_hash = read_json(args.evidence, "evidence")
        documents = []

        def descriptors():
            # assess_items validates each descriptor before opening the next.
            # Only a validated digest and its exhibit path survive iteration.
            for descriptor_path, document_path in args.record:
                descriptor, digest = read_json(descriptor_path, "descriptor")
                expected = descriptor.get("document_sha256") if isinstance(descriptor, dict) else None
                yield descriptor, digest, expected
                documents.append((document_path, expected))

        report = receipt.assess(
            request,
            request_hash,
            args.expect_request,
            policy,
            policy_hash,
            evidence,
            evidence_hash,
            descriptors(),
        )
        if (
            datetime.fromisoformat(report["observed_at"])
            > datetime.now(UTC) + MAX_CLOCK_SKEW
        ):
            raise InvalidReport(
                "Observation time is more than five minutes ahead of the verifier clock."
            )
        if args.expect_job is not None and args.expect_job != report["job_id"]:
            raise InvalidReport("Job identifier does not match the expected job.")
        budget = MAX_TOTAL_DOCUMENT_BYTES
        for path, expected in documents:
            actual, size = document_digest(path, budget)
            if actual != expected:
                raise InvalidReport("Document digest does not match.")
            budget -= size
        verified = args.offboarding_action == "verify-receipt"
        if verified:
            supplied, _ = read_json(args.receipt, "receipt")
            # JSON distinguishes true, 1, and 1.0. Native Python equality does not.
            if json.dumps(supplied, sort_keys=True) != json.dumps(
                report, sort_keys=True
            ):
                raise InvalidReport("Receipt does not match the replayed evidence.")
        else:
            publish_new(report, args.output)
        print_decision(report, verified=verified)
        return receipt.EXIT_CODES[report["review_status"]]
    except InvalidReport as error:
        print(f"Invalid receipt input or output. {error}", file=sys.stderr)
    except (OSError, RecursionError):
        print("Unable to read inputs or publish the requested file.", file=sys.stderr)
    return 2
