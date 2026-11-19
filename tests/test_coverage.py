"""Exercise declared route and job coverage against real Git revisions."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.cli import EXAMPLE
from setrya.coverage import coverage, main
from setrya.drill import Drill, InputError
from setrya.examples.recorded_search.verify import git
from setrya.runner import run

POLICY = '''version = 1
[[component]]
name = "search"
owner = "@readers"
root = "app"
decorators = ["router.get"]
[[component.binding]]
file = "app/routes.py"
surface = "router.get /support"
drill = "refund.toml"
probe = "refund-window"
'''
ROUTES = '''@router.get("/support")
def support():
    return backend()
'''


class CoverageTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.repo = self.root / "app-repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Coverage")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        self.policy = self.repo / "setrya-coverage.toml"
        self.policy.write_text(POLICY)
        self.drill = self.repo / "refund.toml"
        self.drill.write_bytes((EXAMPLE / "python.toml").read_bytes())
        (self.repo / "app").mkdir()
        self.source = self.repo / "app/routes.py"
        self.source.write_text(ROUTES)
        self.base = self.commit()

    def commit(self):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        return git(self.repo, "rev-parse", "HEAD")

    def decision(self, *reports):
        return coverage(self.repo, self.base, "HEAD", self.policy.name, reports)

    def test_new_route_and_ordinary_refactor(self):
        self.source.write_text(ROUTES + "\n# Formatting only.\n")
        self.commit()
        code, summary = self.decision()
        self.assertEqual(code, 0)
        self.assertIn("No changed routes", summary)
        self.source.write_text(ROUTES + '\n@router.get(path="/agent")\nasync def agent(): return summary()\n')
        self.commit()
        code, summary = self.decision()
        self.assertEqual(code, 3)
        self.assertIn("router.get /agent", summary)
        self.assertIn("@readers", summary)
        self.assertNotIn("router.get /support", summary)

    def test_reordering_first_match_routes_requires_evidence(self):
        fixed = '@router.get("/users/me")\ndef me(): return current_user()\n'
        wildcard = '@router.get("/users/{id}")\ndef user(id): return lookup(id)\n'
        self.source.write_text(ROUTES + fixed + wildcard)
        self.base = self.commit()
        self.source.write_text(ROUTES + wildcard + fixed)
        self.commit()
        code, summary = self.decision()
        self.assertEqual(code, 3)
        self.assertEqual(summary.count("GAP  "), 2)
        self.assertIn("router.get /users/me", summary)
        self.assertIn("router.get /users/{id}", summary)
        self.assertNotIn("router.get /support", summary)

    def test_moving_router_installation_before_handler_requires_evidence(self):
        install = "app.include_router(router)\n"
        self.source.write_text(ROUTES + install)
        self.base = self.commit()
        self.source.write_text(install + ROUTES)
        self.commit()
        code, summary = self.decision()
        self.assertEqual(code, 3)
        self.assertIn("router.get /support · missing candidate evidence", summary)

    def test_route_path_takes_precedence_over_operation_name(self):
        self.source.write_text(ROUTES.replace('("/support")', '("/support", name="supportSearch")'))
        self.base = self.commit()
        self.source.write_text(ROUTES.replace('("/support")', '(name="supportSearch", path="/agent")'))
        self.commit()
        code, summary = self.decision()
        self.assertEqual(code, 3)
        self.assertIn("router.get /support · removed surface", summary)
        self.assertIn("router.get /agent · no mapped drill", summary)
        self.assertNotIn("supportSearch", summary)

    def test_dynamic_registration_options_require_a_file_mapping(self):
        self.source.write_text(ROUTES.replace('("/support")', '(name="supportSearch", **options)'))
        self.commit()
        with self.assertRaisesRegex(InputError, "file-level coverage"):
            self.decision()

    def test_candidate_binding_and_failure_precedence(self):
        self.source.write_text(ROUTES.replace("backend()", "cache()"))
        head = self.commit()
        report = self.root / "run.json"
        drill = Drill.load(self.drill)
        result = run(drill, revision=head).report
        result.save(report)
        self.assertEqual(self.decision(report)[0], 0)
        for inputs in ({**result.inputs, "revision": self.base},
                       {**result.inputs, "adapter": "a" * 64},
                       {**result.inputs, "questions": dict.fromkeys(result.inputs["questions"], "a" * 64)}):
            replace(result, inputs=inputs).save(report)
            self.assertEqual(self.decision(report)[0], 3)
        result.save(report)
        bad = self.root / "broken.json"
        run(replace(drill, options={"mode": "broken"}), revision=head).report.save(bad)
        self.assertEqual(self.decision(report, bad)[0], 1)
        # Uncommitted edits cannot supply a different map.
        self.policy.write_text("invalid")
        self.assertEqual(self.decision(report)[0], 0)

    def test_removed_or_unmapped_surface_survives_policy_narrowing(self):
        self.source.unlink()
        self.policy.write_text(POLICY.split("[[component.binding]]")[0].replace('"app"', '"elsewhere"'))
        self.commit()
        code, summary = self.decision()
        self.assertEqual(code, 3)
        self.assertIn("removed surface", summary)
        self.assertIn("router.get /support", summary)

    def test_file_level_jobs_and_output_protection(self):
        self.policy.write_text(POLICY + '\n[[component]]\nname="jobs"\nowner="@indexing"\nroot="jobs"\n')
        (self.repo / "jobs").mkdir()
        (self.repo / "jobs/refresh").write_text("index")
        self.commit()
        code, summary = self.decision()
        self.assertEqual(code, 3)
        self.assertIn("owner @indexing · jobs/refresh", summary)
        args = ["--repo", str(self.repo), "--base", self.base, "--summary"]
        with redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            self.assertEqual(main([*args, str(self.source)]), 2)
            self.assertEqual(main([*args, str(self.root / "report.md")]), 3)
        self.assertEqual(self.source.read_text(), ROUTES)
        self.assertIn("GAP", (self.root / "report.md").read_text())
