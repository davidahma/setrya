"""Exercise the public-facing example, exit mappings and the comment boundary."""
import contextlib
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.cli import main
from setrya import POSITIONING

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("setrya_action", ROOT / "scripts" / "action.py")
action = module_from_spec(SPEC)
SPEC.loader.exec_module(action)


class PublicCLI(unittest.TestCase):
    def test_workspace_and_delete_only_handoff(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(StringIO()) as log:
            out = Path(tmp, "demo")
            self.assertEqual(main(["demo", "--output", str(out)]), 0)
            self.assertEqual(main(["replay", str(out / "broken")]), 1)
            self.assertEqual(main(["replay", str(out / "guarded" / "erasure-receipt.json")]), 0)
            self.assertEqual(main(["replay", str(out / "guarded"), "--case-review"]), 3)
            result = json.loads((out / "broken/erasure-evidence.json").read_bytes())
            self.assertEqual({row["caller"] for row in result["final"]["answers"]}, {"support-lead", "account-manager"})
            for row in result["final"]["answers"][::2]:
                self.assertEqual(row["judgments"]["target"]["excerpt"], "cobalt-bridge.example")
            starter = Path(tmp, "starter")
            self.assertEqual(main(["init", "--output", str(starter)]), 0)
            args = ["--starter", str(starter / "test_erasure.py")]
            self.assertEqual(main(["doctor", *args]), 0)
            self.assertEqual(main(["run", *args, "--output", f"{tmp}/case"]), 0)
            self.assertIn("READY", log.getvalue())

    def test_primary_options_route_with_equals_or_separate_arguments(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(StringIO()):
            self.assertEqual(main(["demo", "--mode=deletion", f"--output={tmp}/workspace"]), 0)
            self.assertTrue(Path(tmp, "workspace/guarded/erasure-receipt.json").is_file())
            self.assertEqual(main(["init", "--scope=tenant", f"--output={tmp}/policy"]), 0)
            self.assertTrue(Path(tmp, "policy/policy.json").is_file())
            self.assertEqual(main(["demo", "--scope=tenant", f"--output={tmp}/tenant"]), 0)
            self.assertTrue(Path(tmp, "tenant/receipt/erasure-receipt.json").is_file())

    def test_scoped_replay_and_verification_use_the_receipts_actual_scope(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()):
            person, tenant = Path(tmp, "person"), Path(tmp, "tenant")
            self.assertEqual(main(["demo", "--mode", "deletion", "--output", str(person)]), 0)
            self.assertEqual(main(["demo", "--scope", "tenant", "--output", str(tenant)]), 0)
            key, signature = Path(tmp, "key"), Path(tmp, "signature")
            key.write_text("test key")
            signature.write_text("test signature")
            for scope, wrong, folder in (("person", "tenant", person / "guarded"), ("tenant", "person", tenant / "receipt")):
                for cmd in (["replay"], ["erasure", "replay"]):
                    self.assertEqual(main([*cmd, str(folder), "--scope", scope]), 0)
                    self.assertEqual(main([*cmd, str(folder), "--scope", wrong]), 2)
                verify = ["verify", "--receipt", str(folder / "erasure-receipt.json"), "--bundle", str(signature), "--key", str(key)]
                with patch("setrya.offboarding.signature.subprocess.run") as cosign:
                    self.assertEqual(main([*verify, "--scope", wrong]), 2)
                    cosign.assert_not_called()
                    self.assertEqual(main([*verify, "--scope", scope]), 0)
                    self.assertEqual(main(verify), 0)
                    self.assertEqual(cosign.call_count, 2)
            legacy = ROOT / "tests/fixtures/offboarding-receipt-axiom-v0.3.2.json"
            with patch("setrya.offboarding.signature.subprocess.run") as cosign:
                verify = ["verify", "--receipt", str(legacy), "--bundle", str(signature), "--key", str(key)]
                self.assertEqual(main([*verify, "--scope", "person"]), 2)
                self.assertEqual(main([*verify, "--scope", "tenant"]), 0)
                cosign.assert_called_once()

    def test_starter_without_receipt_reports_a_safe_diagnostic(self):
        with TemporaryDirectory() as tmp:
            starter = Path(tmp, "starter.py")
            args = ["run", "--starter", str(starter), "--output", str(Path(tmp, "out"))]
            for code in (0, 1, 2, 3):
                starter.write_text(f"raise SystemExit({code})\n")
                with self.subTest(code=code), contextlib.redirect_stderr(StringIO()) as err:
                    self.assertEqual(main(args), 3 if code == 3 else 2)
                    self.assertIn("NOT TESTED" if code == 3 else "ERROR", err.getvalue())
                    self.assertIn(f"exited {code} before writing a receipt", err.getvalue())

    def test_doctor_validates_the_contract_before_opening_and_preserves_hook_errors(self):
        with TemporaryDirectory() as tmp:
            starter = Path(tmp, "starter.py")
            args = ["doctor", "--starter", str(starter)]
            starter.write_text("def open_fixture():\n    raise AssertionError('setup was called')\n")
            with contextlib.redirect_stderr(StringIO()) as err:
                self.assertEqual(main(args), 2)
                self.assertIn("define a SharedCase named CASE", err.getvalue())
            starter.write_text("from setrya.examples.workspace import SIMPLE as CASE\n"
                               "def open_fixture():\n    raise KeyError('private setting')\n")
            with contextlib.redirect_stderr(StringIO()) as err:
                self.assertEqual(main(args), 2)
                self.assertIn("KeyError", err.getvalue())
                self.assertNotIn("private setting", err.getvalue())
            starter.write_text("from contextlib import contextmanager\nfrom dataclasses import replace\n"
                               "from setrya.examples.workspace import SIMPLE, open_fixture as workspace\n"
                               "CASE = replace(SIMPLE, scan_stores=('notes',), require_lineage=True)\n"
                               "open_fixture = workspace\n")
            with contextlib.redirect_stdout(StringIO()) as out:
                self.assertEqual(main(args), 3)
                self.assertIn("Missing fixture methods: scan, lineage", out.getvalue())

    def test_public_help_names_only_primary_commands(self):
        with contextlib.redirect_stdout(StringIO()) as out, self.assertRaises(SystemExit):
            main(["--help"])
        help_text = out.getvalue()
        self.assertIn(POSITIONING, help_text)
        self.assertIn("{init,run,replay,demo,verify}", help_text)
        self.assertIn("doctor", help_text)
        self.assertNotIn("qualify", help_text)

    def test_four_action_outputs_and_comment_allowlist(self):
        for code, review, label in (("0", "OPEN_ITEMS", "PASS"), ("1", "RESIDUAL_FOUND", "FAIL"),
                                    ("2", "UNAVAILABLE", "ERROR"), ("3", "OPEN_ITEMS", "NOT TESTED")):
            self.assertIn(label, action.summary(code, review))
        for code, status in (("0", "UNAVAILABLE"), ("1", "READY_FOR_REVIEW"), ("0", "@reviewer")):
            with self.assertRaises(ValueError):
                action.summary(code, status)
        with TemporaryDirectory() as tmp:
            event = Path(tmp, "event.json")
            event.write_text(json.dumps({"number": 3, "pull_request": {"base": {"repo": {"full_name": "owner/repo"}}}}))
            env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "owner/repo",
                   "GITHUB_RUN_ID": "12", "SETRYA_CODE": "1", "SETRYA_REVIEW": "RESIDUAL_FOUND"}
            with patch.dict(os.environ, env), patch.object(action.subprocess, "run") as api:
                self.assertEqual(action.comment(), 0)
                self.assertIn("repos/owner/repo/issues/3/comments", api.call_args.args[0])
                self.assertIn("FAIL", json.loads(api.call_args.kwargs["input"])["body"])
                self.assertIn("https://github.com/owner/repo/actions/runs/12", api.call_args.kwargs["input"])
                with patch.dict(os.environ, {"SETRYA_CODE": "3", "SETRYA_REVIEW": "UNAVAILABLE"}):
                    self.assertEqual(action.comment(), 0)
                    self.assertNotIn("download the receipt", api.call_args.kwargs["input"])
                api.reset_mock()
                with patch.dict(os.environ, {"SETRYA_CODE": "0\nPASS"}), self.assertRaises(ValueError):
                    action.comment()
                api.assert_not_called()
