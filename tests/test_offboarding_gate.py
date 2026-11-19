"""Setrya integration tests adapted from the Axiom v0.3.2 gate suite."""

import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from setrya.cli import main


def documents(checkpoint="after_offboarding"):
    required = {"id": "cache", "scope": "answers", "checkpoint": checkpoint}
    probe = {
        **required,
        "before": {"subject": "present", "control": "present"},
        "after": {"subject": "absent", "control": "present"},
    }
    if checkpoint == "after_worker":
        probe["worker"] = {
            "captured_before_offboarding": True,
            "finished_after_offboarding": True,
        }
    policy = {"version": 1, "target": "helpdesk", "probes": [required]}
    observations = {
        "version": 1,
        "target": "helpdesk",
        "revision": "build-17",
        "run_id": "run-28",
        "offboarding": "completed",
        "probes": [probe],
    }
    return policy, observations


class GateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.policy = self.root / "policy.json"
        self.observations = self.root / "observations.json"
        self.report = self.root / "result" / "report.json"

    def write_inputs(self, policy, observations):
        for path, data in ((self.policy, policy), (self.observations, observations)):
            path.write_bytes((json.dumps(data, indent=1) + "\r\n").encode("utf-8"))

    def invoke(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "offboarding",
                    "gate",
                    "--policy",
                    str(self.policy),
                    "--observations",
                    str(self.observations),
                    *map(str, extra),
                ]
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def evaluate(self, policy, observations, expected_code, *extra):
        self.write_inputs(policy, observations)
        self.report.unlink(missing_ok=True)
        code, stdout, stderr = self.invoke("--output", self.report, *extra)
        self.assertEqual(code, expected_code, stderr or stdout)
        if code == 2:
            self.assertFalse(self.report.exists())
            self.assertTrue(stderr)
            self.assertFalse(stdout)
            return stderr
        self.assertFalse(stderr)
        result = json.loads(self.report.read_bytes())
        self.assertEqual(result["verdict"], {0: "PASS", 1: "FAIL", 3: "INCONCLUSIVE"}[code])
        self.assertIn(f"Gate {'NOT TESTED' if code == 3 else result['verdict']}", stdout)
        for probe in result["probes"]:
            for item in [
                probe["id"],
                probe["scope"],
                probe["checkpoint"],
                *probe["reasons"],
            ]:
                self.assertIn(item, stdout)
        return result

    def test_pass_binds_identity_and_both_input_byte_digests(self):
        result = self.evaluate(
            *documents(), 0, "--expect-run", "run-28", "--expect-revision", "build-17"
        )
        self.assertEqual(result["target"], "helpdesk")
        self.assertEqual(result["revision"], "build-17")
        self.assertEqual(result["run_id"], "run-28")
        for name, path in (
            ("policy_sha256", self.policy),
            ("observations_sha256", self.observations),
        ):
            self.assertEqual(
                result[name], hashlib.sha256(path.read_bytes()).hexdigest()
            )
        self.assertEqual(result["probes"][0]["reasons"], [])
        self.assertNotIn("before", result["probes"][0])

    def test_each_read_state_requires_the_expected_subject_and_control(self):
        for phase, role, pass_state in (
            ("before", "subject", "present"),
            ("before", "control", "present"),
            ("after", "subject", "absent"),
            ("after", "control", "present"),
        ):
            for state in ("present", "absent", "unavailable", "not_tested"):
                with self.subTest(phase=phase, role=role, state=state):
                    policy, observations = documents()
                    observations["probes"][0][phase][role] = state
                    code = 0 if state == pass_state else 3
                    if (phase, role, state) == ("after", "subject", "present"):
                        code = 1
                    result = self.evaluate(policy, observations, code)
                    if code:
                        self.assertIn(
                            f"{phase}_{role}_{state}", result["probes"][0]["reasons"]
                        )

    def test_positive_match_survives_incomplete_baseline_and_another_missing_probe(
        self,
    ):
        policy, observations = documents()
        policy["probes"].append(
            {"id": "source", "scope": "rows", "checkpoint": "after_offboarding"}
        )
        observed = observations["probes"][0]
        observed["before"]["subject"] = "unavailable"
        observed["after"].update(subject="present", control="not_tested")
        cache, source = self.evaluate(policy, observations, 1)["probes"]
        self.assertEqual(cache["verdict"], "FAIL")
        self.assertCountEqual(
            cache["reasons"],
            [
                "before_subject_unavailable",
                "after_subject_present",
                "after_control_not_tested",
            ],
        )
        self.assertEqual(source["verdict"], "INCONCLUSIVE")
        self.assertIn("missing_probe", source["reasons"])

    def test_unconfirmed_offboarding_cannot_establish_a_deletion_result(self):
        for state in ("present", "absent"):
            with self.subTest(state=state):
                policy, observations = documents()
                observations["offboarding"] = "unconfirmed"
                observations["probes"][0]["after"]["subject"] = state
                result = self.evaluate(policy, observations, 3)
                self.assertIn("offboarding_unconfirmed", result["probes"][0]["reasons"])

    def test_policy_requires_evidence_for_its_own_scope_and_checkpoint(self):
        policy, observations = documents()
        observations["probes"] = []
        self.assertIn(
            "missing_probe",
            self.evaluate(policy, observations, 3)["probes"][0]["reasons"],
        )
        policy["probes"] = []
        self.evaluate(policy, observations, 2)

        for field, value in (("scope", "replica"), ("checkpoint", "after_worker")):
            with self.subTest(field=field):
                policy, observations = documents()
                policy["probes"][0][field] = value
                observations["probes"][0]["after"]["subject"] = "present"
                result = self.evaluate(policy, observations, 3)
                self.assertIn(f"{field}_mismatch", result["probes"][0]["reasons"])
                self.assertIn(
                    "observed_after_subject_present_outside_policy",
                    result["probes"][0]["reasons"],
                )

    def test_worker_checkpoint_needs_both_overlap_observations(self):
        for captured in (True, False):
            for finished in (True, False):
                with self.subTest(captured=captured, finished=finished):
                    policy, observations = documents("after_worker")
                    observations["probes"][0]["worker"].update(
                        captured_before_offboarding=captured,
                        finished_after_offboarding=finished,
                    )
                    result = self.evaluate(
                        policy, observations, 0 if captured and finished else 3
                    )
                    reasons = result["probes"][0]["reasons"]
                    self.assertEqual(
                        "worker_capture_unobserved" in reasons, not captured
                    )
                    self.assertEqual(
                        "worker_completion_unobserved" in reasons, not finished
                    )

    def test_worker_flags_are_required_booleans_bound_to_worker_checkpoint(self):
        for field in ("captured_before_offboarding", "finished_after_offboarding"):
            for invalid in (None, 1, "false"):
                with self.subTest(field=field, invalid=invalid):
                    policy, observations = documents("after_worker")
                    worker = observations["probes"][0]["worker"]
                    if invalid is None:
                        del worker[field]
                    else:
                        worker[field] = invalid
                    self.evaluate(policy, observations, 2)
        policy, observations = documents("after_worker")
        worker = observations["probes"][0].pop("worker")
        self.evaluate(policy, observations, 2)
        policy, observations = documents()
        observations["probes"][0]["worker"] = worker
        self.evaluate(policy, observations, 2)

    def test_wrong_run_revision_or_target_is_invalid(self):
        for option in ("--expect-run", "--expect-revision"):
            with self.subTest(option=option):
                self.evaluate(*documents(), 2, option, "other")
        policy, observations = documents()
        observations["target"] = "other"
        self.evaluate(policy, observations, 2)

    def test_undeclared_duplicate_or_malformed_probes_are_invalid(self):
        policy, observations = documents()
        observations["probes"][0]["id"] = "unknown"
        self.evaluate(policy, observations, 2)
        for side in ("policy", "observations"):
            for mutation in (
                "duplicate",
                "omitted scope",
                "bad id",
                "bad checkpoint",
                "bad version",
            ):
                with self.subTest(side=side, mutation=mutation):
                    policy, observations = documents()
                    doc = policy if side == "policy" else observations
                    probe = doc["probes"][0]
                    if mutation == "duplicate":
                        doc["probes"].append(copy.deepcopy(probe))
                    elif mutation == "omitted scope":
                        del probe["scope"]
                    elif mutation == "bad id":
                        probe["id"] = "x" * 129
                    elif mutation == "bad checkpoint":
                        probe["checkpoint"] = "later"
                    else:
                        doc["version"] = True
                    self.evaluate(policy, observations, 2)

    def test_rejected_input_content_stays_out_of_console_and_report(self):
        marker = "sample customer content"
        for location in ("field", "probe", "identity", "state"):
            with self.subTest(location=location):
                policy, observations = documents()
                if location == "field":
                    observations[marker] = "extra"
                elif location == "probe":
                    observations["probes"][0]["answer"] = marker
                elif location == "identity":
                    observations["run_id"] = marker
                else:
                    observations["probes"][0]["before"]["subject"] = marker
                self.assertNotIn(marker, self.evaluate(policy, observations, 2))

    def test_inputs_require_bounded_utf8_json_with_unique_fields(self):
        self.write_inputs(*documents())
        valid = self.observations.read_bytes()
        for raw in (
            b"[]",
            b"\xff" + valid,
            b'{"target":"private",' + valid[1:],
            valid + b" " * (1024 * 1024),
        ):
            with self.subTest(prefix=raw[:16]):
                self.observations.write_bytes(raw)
                code, stdout, stderr = self.invoke("--output", self.report)
                self.assertEqual(code, 2)
                self.assertFalse(stdout)
                self.assertNotIn("private", stderr)
                self.assertFalse(self.report.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_file_inputs_reject_a_named_pipe_without_waiting_for_a_writer(self):
        self.write_inputs(*documents())
        fifo = self.root / "pending-input"
        os.mkfifo(fifo)
        alias = self.root / "pending-link"
        alias.symlink_to(fifo)
        for command, status in (
            (["gate", "--policy", self.policy, "--observations", alias], 2),
            (["gate", "--policy", fifo, "--observations", self.observations], 2),
        ):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-m", "setrya", "offboarding", *command],
                    cwd=Path(__file__).resolve().parents[1],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertFalse(result.stdout)
                self.assertFalse(self.report.exists())

    def test_symlink_to_regular_evidence_keeps_its_digest_and_verdict(self):
        self.write_inputs(*documents())
        alias = self.root / "evidence-link"
        alias.symlink_to(self.observations)
        self.observations = alias
        code, stdout, stderr = self.invoke("--output", self.report)
        self.assertEqual(code, 0, stderr or stdout)
        report = json.loads(self.report.read_bytes())
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(
            report["observations_sha256"],
            hashlib.sha256(alias.read_bytes()).hexdigest(),
        )

    def test_output_cannot_replace_an_input_through_a_file_alias(self):
        self.write_inputs(*documents())
        for path in (self.policy, self.observations):
            original = path.read_bytes()
            symbolic = self.root / f"symbolic-{path.name}"
            symbolic.symlink_to(path)
            hard = self.root / f"hard-{path.name}"
            os.link(path, hard)
            for alias in (path, symbolic, hard):
                with self.subTest(alias=alias.name):
                    code, stdout, stderr = self.invoke("--output", alias)
                    self.assertEqual(code, 2)
                    self.assertFalse(stdout)
                    self.assertTrue(stderr)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(alias.read_bytes(), original)

    def test_gate_output_rejects_an_unrelated_symlink(self):
        self.write_inputs(*documents())
        existing = self.root / "previous-run.json"
        existing.write_text("synthetic earlier run")
        alias = self.root / "destination.json"
        alias.symlink_to(existing)
        code, stdout, stderr = self.invoke("--output", alias)
        self.assertEqual(code, 2)
        self.assertTrue(stderr)
        self.assertFalse(stdout)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(existing.read_text(), "synthetic earlier run")

    def test_missing_required_cli_argument_uses_invalid_input_status(self):
        result = subprocess.run(
            [sys.executable, "-m", "setrya", "offboarding", "gate", "--policy", str(self.policy)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr)
        self.assertFalse(result.stdout)
