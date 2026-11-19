"""Replay a receipt produced by Axiom v0.3.2 through the Setrya CLI."""

import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from setrya.cli import main
from setrya.offboarding.inputs import InvalidReport
from setrya.offboarding.receipt_cli import publish_new
from test_offboarding_receipt import packet, record


LEGACY_RECEIPT = Path(__file__).with_name("fixtures") / "offboarding-receipt-axiom-v0.3.2.json"


class OffboardingReceiptCLITests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = packet(("provider", "provider_statement"), ("archive", "retention_schedule"))
        self.files = {}
        for key in ("request", "policy", "evidence"):
            self.files[key] = self.store(key + ".json", self.data[key])
        self.inputs = [
            "--policy", self.files["policy"], "--request", self.files["request"],
            "--evidence", self.files["evidence"], "--expect-request", self.data["request_hash"],
        ]
        for item in self.data["request"]["items"]:
            descriptor, _, _ = record(item, self.data["request_hash"])
            exhibit = self.root / (item["id"] + ".txt")
            exhibit.write_bytes(item["id"].encode("ascii"))
            self.inputs.extend(["--record", self.store(item["id"] + ".json", descriptor), exhibit])

    def store(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def invoke(self, expected_exit, *args):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            actual_exit = main(["offboarding", *map(str, args)])
        self.assertEqual(actual_exit, expected_exit, output.getvalue())
        return output.getvalue()

    def test_replay_v032_receipt_and_recompute_exact_bytes(self):
        # The checked-in receipt was generated with the original v0.3.2 CLI from these inputs.
        command = ["verify-receipt", *self.inputs, "--receipt", LEGACY_RECEIPT]
        verified = self.invoke(2, *command)
        self.assertIn("Receipt verified.", verified)
        self.assertIn("Technical verdict PASS", verified)
        self.assertIn("Review status OPEN_ITEMS", verified)
        self.assertIn("SCHEDULED archive", verified)
        output = self.root / "new-receipt.json"
        self.invoke(2, "receipt", *self.inputs, "--output", output)
        self.assertEqual(output.read_bytes(), LEGACY_RECEIPT.read_bytes())
        self.invoke(3, "receipt", *self.inputs, "--output", output)
        self.invoke(3, *command, "--expect-job", "neighboring-job")

    def test_replay_requires_original_exhibits_and_exact_json_types(self):
        for field, value in (("version", True), ("technical_verdict", "FAIL"), ("review_status", "READY_FOR_REVIEW")):
            with self.subTest(field=field):
                modified = json.loads(LEGACY_RECEIPT.read_bytes())
                modified[field] = value
                path = self.store("modified.json", modified)
                self.invoke(3, "verify-receipt", *self.inputs, "--receipt", path)
        (self.root / "provider.txt").write_bytes(b"different document")
        self.invoke(3, "verify-receipt", *self.inputs, "--receipt", LEGACY_RECEIPT)

    def test_replay_allows_only_the_declared_clock_skew(self):
        observed = datetime.fromisoformat(self.data["evidence"]["job"]["observed_at"])
        with patch("setrya.offboarding.receipt_cli.datetime", wraps=datetime) as clock:
            for seconds, expected_exit in ((300, 2), (301, 3), (-86400, 2)):
                with self.subTest(seconds=seconds):
                    clock.now.return_value = observed - timedelta(seconds=seconds)
                    self.invoke(expected_exit, "verify-receipt", *self.inputs, "--receipt", LEGACY_RECEIPT)

    def test_request_issues_fresh_identity_once_per_output(self):
        args = ["request", "--policy", self.files["policy"], "--revision", "v3", "--run-id", "r7",
                "--context", "staging", "--statement", "archive"]
        digests = []
        for name in ("case-a.json", "case-b.json"):
            path = self.root / name
            stdout = self.invoke(0, *args, "--output", path)
            content = path.read_bytes()
            digests.append(hashlib.sha256(content).hexdigest())
            self.assertIn(digests[-1], stdout)
            self.invoke(3, *args, "--output", path)
            self.assertEqual(content, path.read_bytes())
        self.assertNotEqual(*digests)

    def test_concurrent_publish_does_not_replace_another_receipt(self):
        output = self.root / "raced.json"
        def attempt(value):
            try:
                return publish_new(value, output)
            except InvalidReport:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = list(filter(None, pool.map(attempt, [{"case": "a"}, {"case": "b"}])))
        self.assertEqual(winners, [hashlib.sha256(output.read_bytes()).hexdigest()])
