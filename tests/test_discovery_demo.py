"""An external case reviewer uses the same catalog finding for all three worker outcomes."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from importlib.resources import files
from importlib.util import find_spec
from io import StringIO
import json
import os
import sqlite3
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.discovery import demo
from setrya.discovery.cli import main
from setrya.discovery.macie import load_finding
from setrya.discovery.mapping import bind_finding, load_mapping
from setrya.erasure.investigation import CALLERS, CONTROL_FACT, TARGET_FACT


def invoke(*args):
    with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
        code = main(list(map(str, args)))
    return code, out.getvalue() + err.getvalue()


def has_langgraph():
    try:
        return find_spec("langgraph.checkpoint.sqlite") and find_spec("langgraph.graph")
    except ModuleNotFoundError:
        return False


class ExampleSetupTests(unittest.TestCase):
    def test_sample_is_pinned_to_independently_reconstructible_application_commit(self):
        data = files("setrya.discovery").joinpath("demo_application.py").read_bytes()
        with TemporaryDirectory() as tmp:
            case = demo.init_example(Path(tmp) / "sample")
            mapping = load_mapping(case / "mapping.json")
            binding = bind_finding(load_finding(case / "finding.json"), mapping)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME="Setrya", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Setrya", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                       GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")

            def git(*args, payload=None):
                return subprocess.run(["git", *args], cwd=tmp, check=True, input=payload,
                                      env=env, capture_output=True).stdout.strip().decode()

            git("init", "--quiet", "--object-format=sha1")
            blob = git("hash-object", "-w", "--stdin", payload=data)
            tree = git("mktree", payload=f"100644 blob {blob}\tapplication.py\n".encode())
            revision = git("commit-tree", tree, "-m", "Shared investigation fixture")
            self.assertEqual(binding["revision"], revision)
            self.assertEqual(demo.APPLICATION_REVISION, revision)
            self.assertEqual(binding["case_id"], "SEC-417")
            self.assertEqual(next(s["owner"] for s in binding["services"] if s["id"] == "analyst-cache"),
                             "case-platform")
            self.assertNotIn("setrya-example-findings", json.dumps(binding))
            self.assertNotIn("source-17.json", json.dumps(binding))
            self.assertEqual(invoke("init", "--output", case)[0], 2)


@unittest.skipUnless(has_langgraph(), "install the erasure extra for the live discovery trial")
class DiscoveryTrialTests(unittest.TestCase):
    def test_same_finding_reports_coverage_and_returns_verified_result_to_case(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "trial"
            code, log = invoke("demo", "--output", folder)
            self.assertEqual(code, 0, log)
            sample = folder / "example"
            expected = ["--finding", sample / "finding.json", "--mapping", sample / "mapping.json"]
            binding = bind_finding(load_finding(expected[1]), load_mapping(expected[3]))
            for lane, code in (("gap", 3), ("broken", 1), ("guarded", 0)):
                with self.subTest(lane=lane):
                    bundle = folder / lane
                    status, text = invoke("replay", bundle, *expected)
                    self.assertEqual(status, code, text)
                    if lane == "gap":
                        self.assertIn("analyst-cache", text)
                        self.assertIn("case-platform", text)
                    report = (bundle / "case-result.json").read_bytes()
                    result = json.loads(report)
                    service = {row["id"]: row for row in result["services"]}
                    mirror = service["analyst-cache"]
                    self.assertEqual(mirror["status"], {"gap": "NOT TESTED", "broken": "FAIL", "guarded": "PASS"}[lane])
                    self.assertEqual(mirror["owner"], "case-platform")
                    self.assertEqual(service["model-host"]["status"], "CONFIGURATION")
                    self.assertEqual(service["model-host"]["exhibit"], "fixture-config-17")
                    self.assertTrue(service["model-host"]["follow_up"])
                    self.assertEqual(result["review_status"], "RESIDUAL_FOUND" if lane == "broken" else "OPEN_ITEMS")
                    for marker in ("SEC-417", binding["finding_id"], binding["revision"],
                                   binding["environment"], "provider-owner"):
                        self.assertIn(marker, report.decode())
                    for private in ("setrya-example-findings", "investigations/source-17.json",
                                    TARGET_FACT, CONTROL_FACT):
                        self.assertNotIn(private, report.decode())
                    ticket = Path(tmp) / f"{lane}-ticket.json"
                    status, text = invoke("export", bundle, "--output", ticket, *expected)
                    self.assertEqual(status, 0, text)
                    self.assertEqual(ticket.read_bytes(), report)
                    if lane != "gap":
                        ev = json.loads((bundle / "erasure" / "erasure-evidence.json").read_bytes())
                        call, = (e for e in ev["lineage"]["derivations"] if e["artifact"]["store"] == "model-provider")
                        self.assertEqual(call["inputs"], [{"store": "source", "key": "finding-17", "revision": "1"}])
                        sent = [row for row in ev["journal"] if row["store"] == "model-provider"]
                        self.assertEqual([row["details"]["stage"] for row in sent], ["attempted", "returned"])
                        self.assertTrue(all(row["details"]["sources"] == ["finding-17@1"] for row in sent))
                        self.assertNotIn(TARGET_FACT, json.dumps(sent))
                        self.assertEqual(ev["worker"]["held"], ev["worker"]["drained"])
                        self.assertTrue(ev["worker"]["held"])
                        final = ev["final"]
                        self.assertEqual([r["caller"] for r in final["answers"]], [c for c in CALLERS for _ in range(2)])
                        self.assertEqual([r["target_present"] for r in final["answers"]],
                                         [lane == "broken", False, lane == "broken", False])
                        self.assertEqual([r["control_present"] for r in final["answers"]], [False, True, False, True])
                        mirror = next(r for r in final["stored"] if r and r["store"] == "analyst-cache")
                        self.assertEqual(mirror["target_present"], lane == "broken")
                        self.assertTrue(mirror["control_present"])
            self.assertEqual(invoke("replay", folder / "guarded", "--case-review", *expected)[0], 3)
            # A different case with the same imported asset cannot consume this export.
            wrong = deepcopy(load_mapping(sample / "mapping.json"))
            wrong["case_id"] = "SEC-418"
            other_map = Path(tmp) / "other.json"
            other_map.write_text(json.dumps(wrong))
            other_ticket = Path(tmp) / "unrelated.json"
            status, _ = invoke("export", folder / "guarded", "--finding", expected[1],
                               "--mapping", other_map, "--output", other_ticket)
            self.assertEqual(status, 2)
            self.assertFalse(other_ticket.exists())

    def test_approved_source_mutation_preserves_other_same_person_records_and_rolls_back_cache_errors(self):
        from setrya.discovery.case import exit_code, run_case

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            sample = demo.init_example(folder / "example")
            binding = bind_finding(load_finding(sample / "finding.json"), load_mapping(sample / "mapping.json"))
            with demo.open_fixture(binding) as settings:
                app, subject = settings["application"], settings["subject"]
                with app.db:
                    app.db.execute("INSERT INTO findings VALUES (?,?,?,?,?,?)",
                                   ("unrelated", subject.ref, "9", "device", "second device", "private note"))
                actions = [row for row in binding["services"] if row.get("action") in ("delete", "rewrite", "preserve")]
                self.assertFalse(app.erase_approved(subject, dict(binding["source"], revision="obsolete"), actions))
                # This fires after the source, checkpoint, summary and primary cache have been rewritten.
                app.db.execute("""CREATE TRIGGER analyst.deny_edit BEFORE UPDATE ON handoffs
                                BEGIN SELECT RAISE(ABORT, 'hand off store unavailable'); END""")
                with self.assertRaises(sqlite3.IntegrityError):
                    app.erase_approved(subject, binding["source"], actions)
                for row in actions:
                    reading = app.inspect(subject, row["store"], row["key"])
                    self.assertTrue(reading.exists)
                    self.assertEqual(reading.target_present, row["action"] != "preserve")
                app.db.execute("DROP TRIGGER analyst.deny_edit")
                with patch.object(app, "erase", side_effect=AssertionError("subject-wide deletion called")):
                    result = run_case(binding, **settings, mode="late-write", output=folder / "run")
                self.assertEqual(exit_code(result), 0, result)
                self.assertEqual(app.db.execute("SELECT revision,raw_text FROM findings WHERE id='unrelated'").fetchone(),
                                 ("9", "private note"))

    def test_second_cache_can_fail_while_both_endpoint_sessions_stay_clean(self):
        from setrya.discovery.case import exit_code, run_case
        from setrya.discovery.demo_application import CACHE_KEY

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            sample = demo.init_example(folder / "example")
            binding = bind_finding(load_finding(sample / "finding.json"), load_mapping(sample / "mapping.json"))
            with demo.open_fixture(binding) as settings:
                app = settings["application"]
                finish = app.release_and_drain

                def stale_second_cache(*args):
                    observed = finish(*args)
                    with app.lock, app.db:
                        app.db.execute("UPDATE analyst.handoffs SET note=? WHERE id=?",
                                       (TARGET_FACT + CONTROL_FACT, CACHE_KEY))
                    return observed

                with patch.object(app, "release_and_drain", stale_second_cache):
                    result = run_case(binding, **settings, mode="late-write", output=folder / "run")
            self.assertEqual(exit_code(result), 1, result)
            ev = json.loads((folder / "run" / "erasure" / "erasure-evidence.json").read_bytes())
            self.assertTrue(all(not r["target_present"] for r in ev["final"]["answers"]))
            residual = [r["store"] for r in ev["final"]["stored"] if r and r["target_present"]]
            self.assertEqual(residual, ["analyst-cache"])
