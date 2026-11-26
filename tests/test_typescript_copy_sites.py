"""Run the optional source parser through working tree, Git and PR review paths."""

from contextlib import redirect_stderr
from importlib.util import find_spec
from io import StringIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import action
from setrya.casework import assessment, assessment_comparison
from setrya.cli import main
from setrya.discovery import copy_sites, typescript


WRITES = """\
import * as files from 'node:fs/promises';
import { createClient as connect } from 'redis';
import { DatabaseSync as Db } from 'node:sqlite';
import { S3Client, PutObjectCommand as Put } from '@aws-sdk/client-s3';
import OpenAI from 'openai';
import { setTimeout as later } from 'node:timers';
import { Worker } from 'bullmq';

const cache = connect();
const db = new Db(':memory:');
const s3 = new S3Client();
const ai = new OpenAI();
const put = db.prepare("INSERT INTO notes VALUES (?)");
const query = db.prepare("SELECT * FROM notes");

files.writeFile('private-location', 'synthetic-secret-sentinel');
cache.set('k', answer);
put.run(answer);
query.get();
s3.send(new Put(args));
ai.responses.create({input: answer, store: false});
ai.conversations.create();
ai.chat.completions.create({messages});
ai.chat.completions.create({messages, [key]: true});
ai.responses.create({input: answer, store: false, ...patch});
later(() => cache.set('timer', answer), 5);
new Worker('copies', async (job) => { await cache.set(job.id, answer); });

function noMatch({cache}: Request, ai: Custom, files: Wrapper) {
  cache.set('k', answer);
  ai.conversations.create();
  files.writeFile('path', answer);
}
function rebound() {
  if (config) { var cache = db; }
  cache.set('shadowed-by-var', answer);
}
"""


@unittest.skipUnless(find_spec("tree_sitter") and find_spec("tree_sitter_typescript"), "install setrya[typescript]")
class TypeScriptCopySitesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def test_recognized_writes_no_literals_and_stable_ids(self):
        source = self.root / "routes.ts"
        source.write_text(WRITES, encoding="utf-8")
        (self.root / "types.d.ts").write_text("not valid code", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/unread.ts").write_text("!!! malformed source }", encoding="utf-8")
        report = copy_sites.scan(self.root, language="typescript")
        self.assertEqual(report["languages"], ["typescript"])
        choices = [(site["kind"], site["operation"], site.get("writer")) for site in report["sites"]]
        self.assertEqual(choices, [
            ("cache", "redis.set", "timer"), ("cache", "redis.set", "bullmq"),
            ("file", "fs.promises.writeFile", None), ("cache", "redis.set", None),
            ("database", "node:sqlite.StatementSync.run", None), ("object", "s3.PutObjectCommand", None),
            ("model-state", "openai.conversations.create", None),
            ("model-state", "openai.chat.completions.create", None),
            ("model-state", "openai.responses.create", None),
        ])
        self.assertNotIn("synthetic-secret-sentinel", json.dumps(report))
        self.assertNotIn("private-location", json.dumps(report))
        self.assertEqual(report["sites"][0]["starter"]["test"], "setrya-case/test_erasure.py")
        self.assertEqual(report["sites"][2]["starter"]["test"], "setrya-case/case.yml")
        source.write_text("\n\n" + WRITES.replace("cache.set('k', answer)", "cache . set ( 'k' , answer )"),
                          encoding="utf-8")
        shifted = copy_sites.scan(self.root, language="typescript")
        self.assertEqual([row["site_id"] for row in shifted["sites"]], [row["site_id"] for row in report["sites"]])

    def test_tsx_and_mixed_scopes(self):
        (self.root / "component.tsx").write_text(
            "import {writeFileSync} from 'node:fs';"
            "const x = <button onClick={() => writeFileSync('path', props.text)} />;",
            encoding="utf-8",
        )
        (self.root / "writer.py").write_text("from pathlib import Path\nPath('test').write_text('value')\n", encoding="utf-8")
        python = copy_sites.scan(self.root)
        self.assertEqual([row["path"] for row in python["sites"]], ["writer.py"])
        both = copy_sites.scan(self.root, language="all")
        self.assertEqual(both["languages"], ["python", "typescript"])
        self.assertEqual(both["sites"][1], python["sites"][0])
        self.assertEqual(both["sites"][0]["operation"], "fs.writeFileSync")

    def test_changed_git_copy_check_and_action_comment(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Setrya Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "Base")
        base = self.git("rev-parse", "HEAD")
        path = self.root / "answers.mts"
        path.write_text(WRITES, encoding="utf-8")
        self.git("add", "answers.mts")
        self.git("commit", "-qm", "Add TypeScript cache")
        report = copy_sites.check(self.root, base, language="typescript")
        self.assertEqual(report["review_required"], 9)
        self.assertEqual(copy_sites.check(self.root, base)["review_required"], 0)
        details = action.copy_details(json.dumps(action.copy_signals(report)))
        self.assertIn("Scanned TypeScript.", details)
        self.assertIn("deferred writer (Node timer callback)", details)
        self.assertIn("database write", details)
        self.assertIn('"mode": "late-write"', details)
        candidate = self.git("rev-parse", "HEAD")
        path.write_text("\n" + WRITES, encoding="utf-8")
        self.git("add", "answers.mts")
        self.git("commit", "-qm", "Shift source without a new write")
        result = copy_sites.check(self.root, candidate, language="typescript")
        self.assertEqual(result["review_required"], 0)

    def test_parse_error_writes_no_report(self):
        (self.root / "broken.ts").write_text("const = {", encoding="utf-8")
        output = self.root / "scan.json"
        with redirect_stderr(StringIO()) as err:
            self.assertEqual(main(["discovery", "copy-scan", str(self.root), "--language", "typescript",
                                   "--output", str(output)]), 2)
        self.assertIn("Could not parse TypeScript source", err.getvalue())
        self.assertFalse(output.exists())


class TypeScriptCopyContractTests(unittest.TestCase):
    def test_missing_optional_extra_has_install_instruction(self):
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"tree_sitter": None}):
            self.assertEqual(copy_sites.scan(temp)["sites"], [])
            with redirect_stderr(StringIO()) as err:
                self.assertEqual(main(["discovery", "copy-scan", temp, "--language", "typescript"]), 2)
            self.assertIn("pip install 'setrya[typescript]'", err.getvalue())

    def test_approval_detects_lost_typescript_even_without_copy_sites(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            report = {"format": copy_sites.REVIEW_FORMAT, "candidate_commit": "a" * 40,
                      "review_required": 0, "sites": []}
            path.write_text(json.dumps({**report, "languages": ["python", "typescript"]}), encoding="utf-8")
            approved = assessment._check(path, "a" * 40, detailed=True)
            path.write_text(json.dumps(report), encoding="utf-8")
            candidate = assessment._check(path, "a" * 40, detailed=True)
        self.assertEqual(approved["languages"], ["python", "typescript"])
        self.assertTrue(assessment_comparison._check_coverage([approved], [approved])[1])
        lines, ready = assessment_comparison._check_coverage([approved], [candidate])
        self.assertFalse(ready)
        self.assertTrue(any("Candidate did not scan typescript" in row for row in lines))
