"""Exercise the fixture-free copy scan and its advisory PR boundary."""

import contextlib
import json
import os
import subprocess
import unittest
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from unittest.mock import patch

from setrya.cli import main
from setrya.discovery import copy_sites

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("setrya_copy_action", ROOT / "scripts/action.py")
action = module_from_spec(SPEC)
SPEC.loader.exec_module(action)

REDIS_SOURCE = """from redis import Redis
cache = Redis.from_url(cache_url)
def save_answer(key, answer):
    cache.set(key, answer)
"""


class CopySites(unittest.TestCase):
    def test_thread_callbacks_and_ordinary_database_writes(self):
        source = dedent("""\
            import sqlite3
            import psycopg
            from concurrent.futures import ThreadPoolExecutor as Pool
            from threading import Thread, Timer
            from sqlalchemy import create_engine, insert, text

            def pg_write(key, answer):
                with psycopg.connect(dsn) as conn:
                    cur = conn.cursor()
                    cur.executemany("INSERT INTO messages VALUES (%s, %s)", [(key, answer)])
                    cur.execute("SELECT * FROM messages")

            db = sqlite3.connect(storage)
            engine = create_engine(sql_url)

            def sqlite_write():
                db.execute("UPDATE notes SET body = ?", data)

            def core_write():
                with engine.begin() as conn:
                    conn.execute(text("INSERT INTO notes VALUES (:body)"), params)
                    conn.execute(insert(notes).values(body=answer))

            Thread(target=pg_write)
            Timer(1, lambda: db.execute("INSERT INTO cache VALUES (?, ?)", data))
            with Pool() as pool:
                pool.submit(sqlite_write)
            core_write()

            def shadow(Thread):
                def immediate():
                    db.execute("UPDATE notes SET body = ?", data)
                return Thread(target=immediate)
        """)
        self.assertEqual([(row.kind, row.operation, row.writer) for row in copy_sites._inspect(
            source.encode(), "storage.py"
        )], [
            ("database", "psycopg.executemany", "thread"),
            ("database", "sqlite3.execute", "thread"),
            ("database", "sqlalchemy.execute", None),
            ("database", "sqlalchemy.execute", None),
            ("database", "sqlite3.execute", "thread-timer"),
            ("database", "sqlite3.execute", None),
        ])

    def test_unscanned_and_unresolved_source_files_stay_advisory(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "worker.js").write_text("database.save(payload)\n")
            (path / "other.md").write_text("ordinary documentation")
            (path / "answers.py").write_text("import psycopg\npsycopg.connect(dsn).execute(query)\n")
            report = copy_sites.scan(path)
            self.assertTrue(copy_sites.review_needed(report))
            self.assertIn("Coverage unknown · 2 source file(s)", copy_sites.summarize(report))
            self.assertNotIn("No supported copy operations", copy_sites.summarize(report))
            self.assertEqual(report["coverage"]["unresolved"], [
                {"path": "answers.py", "reason": "unresolved-storage-write"},
            ])
            groups = {row["extension"]: row for row in report["coverage"]["skipped_extensions"]}
            self.assertEqual(groups[".js"]["paths"], ["worker.js"])
            self.assertFalse(groups[".md"]["source"])
            signals = action.copy_signals({**report, "map_changed": False})
            self.assertIn("Coverage unknown", action.copy_details(json.dumps(signals)))
            with contextlib.redirect_stdout(StringIO()):
                self.assertEqual(main(["discovery", "copy-scan", temp]), 3)

    def test_thread_pool_stored_on_instance_flags_its_method(self):
        source = dedent("""\
            from concurrent.futures import ThreadPoolExecutor as Pool
            from sqlite3 import connect

            database = connect(path)

            class Memory:
                def remember(self, value):
                    self.executor.submit(self.save, value)

                def __init__(self):
                    self.executor = Pool()

                def save(self, value):
                    database.execute("INSERT INTO memory VALUES (?)", (value,))

            class DirectMemory:
                def __init__(self, Pool):
                    self.executor = Pool()

                def remember(self, value):
                    self.executor.submit(self.save, value)

                def save(self, value):
                    database.execute("INSERT INTO direct VALUES (?)", (value,))
        """)
        sites = copy_sites._inspect(source.encode(), "memory.py")
        self.assertEqual([site.writer for site in sites], ["thread", None])
        self.assertEqual(sites[0].wire()["starter"]["mode"], "late-write")

    def test_direct_scheduled_sql_is_ranked_or_reported_unknown(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "app.py"
            path.write_text(dedent("""\
                from concurrent.futures import ThreadPoolExecutor
                from sqlite3 import connect
                from threading import Timer

                db = connect(path)
                pool = ThreadPoolExecutor()
                pool.submit(db.execute, "INSERT INTO notes VALUES (?)", (message,))
                Timer(1, db.execute, args=("UPDATE notes SET body = ?", (message,)))
                pool.submit(db.execute, constructed_query, (message,))
            """))
            report = copy_sites.scan(path.parent)
        self.assertEqual([row["writer"] for row in report["sites"]], ["thread", "thread-timer"])
        self.assertEqual(report["coverage"]["unresolved"], [{"path": "app.py", "reason": "unresolved-storage-write"}])

    def test_table_created_from_select_is_a_copy(self):
        source = b"""\
import sqlite3
db = sqlite3.connect(path)
db.execute("CREATE TABLE first_archive AS SELECT * FROM notes")
db.execute("CREATE TABLE empty_table (name TEXT)")
"""
        sites = copy_sites._inspect(source, "storage.py")
        self.assertEqual([(site.line, site.operation) for site in sites], [(3, "sqlite3.execute")])

    def test_qualified_calls_across_families_without_executing_source(self):
        source = dedent("""\
            from pathlib import Path
            import boto3 as aws
            from qdrant_client import QdrantClient
            from redis.asyncio import Redis
            from mem0 import Memory
            from openai import OpenAI
            from langfuse import get_client
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            raise RuntimeError("this file must never execute")
            Path("synthetic-local.txt").write_text(body)
            with open("another.txt", "wb") as output:
                output.write(body)
            s3 = aws.client("s3")
            s3.put_object(Body=body, Bucket=bucket, Key=key)
            vectors = QdrantClient()
            vectors.upsert(collection_name=name, points=points)
            cache = Redis.from_url(url)
            async def remember():
                await cache.set(key, answer)
            Memory.from_config(config).add(message, user_id=user)
            ai = OpenAI()
            ai.responses.create(input=message)
            ai.responses.create(input=message, store=False)
            ai.chat.completions.create(messages=messages)
            ai.chat.completions.create(messages=messages, store=True)
            ai.conversations.items.create(conversation_id=id, items=items)
            get_client().start_observation(input=message)
            OTLPSpanExporter().export(spans)
            unrelated.set(key, answer)
            Path("read-only.txt").open("r")
        """)
        with TemporaryDirectory() as tmp:
            path = Path(tmp, "answer.py")
            path.write_text(source)
            result = copy_sites.scan(Path(tmp))
            rename_stable = result["sites"]
            path.write_text("\n\n" + source)
            shifted = copy_sites.scan(Path(tmp))["sites"]
        self.assertEqual(result["format"], copy_sites.SCAN_FORMAT)
        self.assertEqual([site["kind"] for site in rename_stable],
                         ["file", "file", "object", "vector", "cache", "memory",
                          "model-state", "model-state", "model-state", "trace", "trace"])
        self.assertEqual([site["site_id"] for site in rename_stable], [site["site_id"] for site in shifted])
        self.assertEqual([site["line"] + 2 for site in rename_stable], [site["line"] for site in shifted])
        self.assertNotIn("synthetic-local.txt", json.dumps(result))
        self.assertEqual(rename_stable[4]["starter"]["store"], "redis-answer-cache")

    def test_vector_sql_sdk_and_document_rest_writes(self):
        source = dedent('''\
            from elasticsearch import AsyncElasticsearch, Elasticsearch
            from elasticsearch.helpers import bulk as bulk_index
            raise RuntimeError("the scanner must not execute this fixture")

            def write(cursor, client, index, key, doc):
                cursor.executemany("""INSERT INTO vector_index VALUES
                                      (%s, %s::public.vector)""", rows)
                cursor.execute("UPDATE index SET embedding = %s::halfvec", parameters)
                cursor.execute("INSERT INTO notes VALUES ('example ::vector')")
                cursor.execute("INSERT INTO notes VALUES (%s) -- query uses ::vector", parameters)
                cursor.execute("SELECT %s::vector", parameters)
                client.request("PUT", f"/{index}/_doc/{key}", doc)
                client.request("PUT", "/_doc/note?refresh=true", doc)
                client.request("POST", "/index/_update/note", doc)
                client.request("GET", "/_doc/note")
                client.request("PUT", "/index/other", doc)
                client.request("PUT", route, doc)
                engine = Elasticsearch()
                engine.options(refresh=True).index(index=index, document=doc)
                bulk_index(engine, actions)
                engine.search(index=index, query=query)

            async def async_write():
                engine = AsyncElasticsearch()
                await engine.bulk(operations=actions)
        ''')
        with TemporaryDirectory() as tmp:
            Path(tmp, "vectors.py").write_text(source, encoding="utf-8")
            sites = copy_sites.scan(tmp)["sites"]
        self.assertEqual([(site["kind"], site["operation"]) for site in sites], [
            ("vector", "pgvector.executemany"), ("vector", "pgvector.execute"),
            ("vector", "elasticsearch-http.document"), ("vector", "elasticsearch-http.document"),
            ("vector", "elasticsearch-http.update"), ("vector", "elasticsearch.index"),
            ("vector", "elasticsearch.helpers.bulk"), ("vector", "elasticsearch.bulk"),
        ])
        self.assertNotIn("index/_update/note", json.dumps(sites))
        self.assertTrue(all(site["starter"]["store"] == "vector-index" for site in sites))

    def test_bundled_vector_examples_are_seen(self):
        examples = ROOT / "setrya/examples"
        expected = {"pgvector_search/app.py": {"pgvector.executemany"},
                    "elasticsearch_search.py": {"elasticsearch-http.document"}}
        for name, operations in expected.items():
            with self.subTest(example=name):
                sites = copy_sites._inspect((examples / name).read_bytes(), name)
                self.assertTrue(operations <= {site.operation for site in sites if site.kind == "vector"})

    def test_deferred_writer_patterns_use_resolved_framework_calls(self):
        source = dedent("""\
            from asyncio import create_task as start_task, get_running_loop
            from celery import Celery, shared_task as task
            from crewai import Memory as CrewMemory
            from dramatiq import actor
            from langmem import ReflectionExecutor
            from redis import Redis
            from redis.asyncio import Redis as AsyncRedis
            from rq import Queue
            from rq.decorators import job
            from temporalio import activity
            from tenacity import retry as try_again

            cache = Redis.from_url(location)
            async_cache = AsyncRedis.from_url(location)
            application = Celery("support")
            queue = Queue()

            def write_note(key, body):
                cache.set(key, body)

            @application.task
            def celery_task():
                write_note(key, body)

            @task()
            def shared():
                cache.set(shared_key, body)

            @job("copies")
            def rq_job():
                cache.set(rq_key, body)

            @actor
            def background_actor():
                cache.set(actor_key, body)

            @activity.defn(name="write")
            async def temporal_writer():
                await async_cache.set(temporal_key, body)

            @try_again
            def retried():
                cache.set(retry_key, body)

            async def coroutine():
                await async_cache.set(coro_key, body)

            def queued_copy():
                cache.set(queue_key, body)

            async def schedule():
                start_task(coroutine())
                get_running_loop().create_task(async_cache.set(pending_key, body))
                start_task(cache.set(immediate_key, body))
                queue.enqueue(queued_copy)
                queue.enqueue(cache.set, callback_key, body)

            memory = CrewMemory()
            memory.remember(body)
            memory.remember_many([body])
            extractor = ReflectionExecutor(reflector, store=client)
            extractor.submit(payload, after_seconds=10)

            def no_writer_hints(task, start_task, extractor):
                @task
                def unrelated():
                    cache.set(unrelated_key, body)
                start_task(async_cache.set(unknown_scheduler_key, body))
                extractor.submit(payload)
        """)
        site_at = {site.line: site.wire() for site in copy_sites._inspect(source.encode(), "deferred.py")}
        labels = [(source.splitlines()[line - 1].strip(), row.get("writer")) for line, row in site_at.items()]
        self.assertEqual(labels, [
            ("cache.set(key, body)", "celery"), ("cache.set(shared_key, body)", "celery"),
            ("cache.set(rq_key, body)", "rq"), ("cache.set(actor_key, body)", "dramatiq"),
            ("await async_cache.set(temporal_key, body)", "temporal"), ("cache.set(retry_key, body)", "retry"),
            ("await async_cache.set(coro_key, body)", "asyncio"), ("cache.set(queue_key, body)", "rq"),
            ("get_running_loop().create_task(async_cache.set(pending_key, body))", "asyncio"),
            ("start_task(cache.set(immediate_key, body))", None), ("queue.enqueue(cache.set, callback_key, body)", "rq"),
            ("memory.remember(body)", None), ("memory.remember_many([body])", "crewai"),
            ("extractor.submit(payload, after_seconds=10)", "langmem"), ("cache.set(unrelated_key, body)", None),
            ("start_task(async_cache.set(unknown_scheduler_key, body))", None),
        ])
        self.assertTrue(all(row["starter"].get("mode") == ("late-write" if "writer" in row else None)
                            for row in site_at.values()))

    def test_forward_module_helpers_keep_their_deferred_writers(self):
        source = dedent("""\
            from asyncio import create_task
            from celery import shared_task
            from redis import Redis
            from redis.asyncio import Redis as AsyncRedis
            from rq import Queue

            cache = Redis()
            async_cache = AsyncRedis()
            queue = Queue()

            @shared_task
            def task():
                save_answer()
                rebound()

            async def request():
                create_task(write_answer())
                queue.enqueue(queue_answer)

            async def shadowed_argument(write_unrelated):
                create_task(write_unrelated())

            def factory():
                @shared_task
                def shadowed_closure():
                    save_unrelated()
                save_unrelated = injected_callback()

            def save_answer():
                write_to_cache()

            def write_to_cache():
                cache.set(celery_key, answer)

            async def write_answer():
                await async_cache.set(asyncio_key, answer)

            def queue_answer():
                cache.set(rq_key, answer)

            def rebound():
                cache.set(overwritten_key, answer)

            rebound = another_callback()

            async def write_unrelated():
                await async_cache.set(argument_key, answer)

            def save_unrelated():
                cache.set(closure_key, answer)
        """)
        sites = copy_sites._inspect(source.encode(), "forward.py")
        self.assertEqual([(source.splitlines()[site.line - 1].strip(), site.writer) for site in sites], [
            ("cache.set(celery_key, answer)", "celery"),
            ("await async_cache.set(asyncio_key, answer)", "asyncio"),
            ("cache.set(rq_key, answer)", "rq"),
            ("cache.set(overwritten_key, answer)", None),
            ("await async_cache.set(argument_key, answer)", None),
            ("cache.set(closure_key, answer)", None),
        ])

    def git(self, path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def test_git_pr_compares_symbols_and_unreviewed_mapping_cannot_clear_gap(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(StringIO()) as output:
            repo = Path(tmp)
            self.git(repo, "init")
            self.git(repo, "config", "user.email", "copy-scan@example.invalid")
            self.git(repo, "config", "user.name", "Copy Scan")
            app = repo / "answers.py"
            app.write_text(REDIS_SOURCE.replace("    cache.set(key, answer)", "    return answer"))
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-m", "Answer without storage")
            base = self.git(repo, "rev-parse", "HEAD")
            app.write_text(REDIS_SOURCE)
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-m", "Cache the answer")
            after = self.git(repo, "rev-parse", "HEAD")
            flags = ["--repo", str(repo), "--base", base, "--candidate", after]
            report = repo / "copy-report.json"
            self.assertEqual(main(["discovery", "copy-check", *flags, "--output", str(report)]), 3)
            data = json.loads(report.read_bytes())
            self.assertEqual(data["candidate_commit"], after)
            self.assertEqual(data["review_required"], 1)
            site, = data["sites"]
            self.assertEqual((site["kind"], site["path"], site["line"]), ("cache", "answers.py", 4))
            self.assertEqual(site["site_id"], "2ef1d1f56f60672111745aada96642b9313039653ab2302e3516188ca3669723")
            self.assertIn("NEEDS-REVIEW  answers.py:4", output.getvalue())
            mapping = {"format": copy_sites.MAP_FORMAT, "sites": [site["starter"]]}
            (repo / copy_sites.MAP_FILE).write_text(json.dumps(mapping))
            self.git(repo, "add", copy_sites.MAP_FILE)
            self.git(repo, "commit", "-m", "Propose a test mapping without evidence")
            app.write_text("raise RuntimeError('dirty work tree must not be read')\n")
            updated = copy_sites.check(repo, base)
            self.assertTrue(updated["map_changed"])
            self.assertEqual(updated["review_required"], 1)
            self.assertEqual(updated["sites"][0]["binding"], site["starter"])
            no_changes = copy_sites.check(repo, after)
            self.assertEqual(no_changes["sites"], [])

    def test_comment_uses_only_bounded_rows_and_separate_marker(self):
        head, site_id = "a" * 40, "b" * 64
        signals = [1, [["answers.py", 4, "cache", site_id]], False]
        rendered = action.copy_details(json.dumps(signals))
        self.assertIn("answers.py:4", rendered)
        self.assertIn("redis-answer-cache", rendered)
        for bad_signals in ([1, [["path with spaces.py", 4, "cache", site_id]], False],
                            [1, [["answers.py", 4, "cache", "bad-id"]], False],
                            [0, signals[1], False]):
            with self.subTest(bad=bad_signals), self.assertRaises(ValueError):
                action.copy_details(json.dumps(bad_signals))
        with TemporaryDirectory() as tmp:
            event = Path(tmp, "event.json")
            event.write_text(json.dumps({"number": 4, "pull_request": {"base": {"repo": {"full_name": "owner/repo"}},
                                                                          "head": {"sha": head}}}))
            env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "owner/repo",
                   "GITHUB_RUN_ID": "27", "GITHUB_SERVER_URL": "https://github.com",
                   "SETRYA_COPY_SUMMARY": json.dumps(signals)}
            saved = [{"id": 67, "body": action.MARKER + "\nOriginal verdict",
                      "user": {"login": "github-actions[bot]", "type": "Bot"}}]
            writes = []

            def api(method, path, body=None):
                if method == "GET":
                    return saved if "/comments?" in path else {"head": {"sha": head}}
                writes.append((method, path, body))
                return None

            with patch.dict(os.environ, env), patch.object(action, "api", side_effect=api):
                self.assertEqual(action.copy_comment(), 0)
            method, dest, body = writes[0]
            self.assertEqual((method, dest), ("POST", "repos/owner/repo/issues/4/comments"))
            self.assertTrue(body["body"].startswith(action.COPY_MARKER + "\n"))
            self.assertIn("answers.py:4", body["body"])

            saved.append({"id": 68, "body": action.COPY_MARKER + "\n" + action.copy_details("[0,[],false]"),
                          "user": {"login": "github-actions[bot]", "type": "Bot"}})
            with patch.dict(os.environ, {**env, "SETRYA_COPY_SUMMARY": "error"}), patch.object(action, "api", side_effect=api):
                self.assertEqual(action.copy_comment(), 0)
            method, dest, body = writes[-1]
            self.assertEqual((method, dest), ("PATCH", "repos/owner/repo/issues/comments/68"))
            self.assertTrue(body["body"].startswith(action.COPY_MARKER + "\n**Setrya copy scan · ERROR**"))
            self.assertNotIn("No new copy sites", body["body"])

    def test_copy_action_ranks_deferred_writers_and_defaults_the_starter(self):
        self.assertEqual(action.COPY_WRITERS, copy_sites.WRITERS)
        report = {"map_changed": False, "sites": [
            {"path": "a.py", "line": 8, "kind": "memory", "site_id": "a" * 64, "status": "needs-review"},
            {"path": "z.py", "line": 3, "kind": "cache", "site_id": "b" * 64, "status": "needs-review", "writer": "celery"},
        ]}
        signals = action.copy_signals(report)
        self.assertEqual(signals[1][0], ["z.py", 3, "cache", "b" * 64, "celery"])
        details = action.copy_details(json.dumps(signals))
        self.assertIn("Hold it across a deletion.", details)
        self.assertIn("`z.py:3` · cache · deferred writer (Celery task)", details)
        self.assertIn('"mode": "late-write"', details)
        signals[1][0][-1] = "unknown writer"
        with self.assertRaisesRegex(ValueError, "invalid deferred writer"):
            action.copy_details(json.dumps(signals))

    def test_action_labels_only_a_replayed_stale_write_back_fail(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(StringIO()):
            folder = Path(tmp)
            workspace = folder / "workspace"
            self.assertEqual(main(["demo", "--output", str(workspace)]), 0)
            from setrya.core.receipt_v3 import MAX_JOURNAL_OBSERVATIONS
            from setrya.receipts import STALE_WRITE_BACK_LEAD, replay, stale_write_back_entry, write_back_lead

            self.assertEqual(action.WRITE_BACK, STALE_WRITE_BACK_LEAD)
            self.assertEqual(action.MAX_JOURNAL_OBSERVATIONS, MAX_JOURNAL_OBSERVATIONS)
            broken = workspace / "broken"
            fixed = workspace / "guarded"
            receipt = replay(broken)
            entry = stale_write_back_entry(receipt, broken)
            self.assertEqual(entry, receipt["write_attribution"][0]["journal_entry"])
            self.assertIsNone(stale_write_back_entry(replay(fixed), fixed))
            signal = ["write-back", "case", entry]
            encoded = json.dumps([signal, *action.signals(receipt)])
            rendered = action.receipt_details("1", "RESIDUAL_FOUND", encoded)
            lead = write_back_lead(receipt, broken)
            self.assertTrue(rendered.startswith(lead + "\n\n**Setrya · FAIL**"))
            zero = action.receipt_details("1", "RESIDUAL_FOUND", '[["write-back","case",0]]')
            self.assertTrue(zero.startswith(action.WRITE_BACK.format(entry=0)))
            output, summary = folder / "output.txt", folder / "summary.md"
            env = {"SETRYA_OUTPUT": str(broken), "SETRYA_STARTER": "fixture.py", "SETRYA_MODE": "write-back",
                   "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
            with (patch.dict(os.environ, env), patch.object(action, "require_version"),
                  patch("setrya.erasure.cli.run_person_starter", return_value=(1, receipt)) as run):
                self.assertEqual(action.run(), 1)
            run.assert_called_once_with(Path("fixture.py"), "late-write", broken.absolute(), test_files=[])
            self.assertIn(f'evidence-summary=[["write-back","case",{entry}]', output.read_text())
            self.assertTrue(summary.read_text().startswith(lead + "\n\n**Setrya · FAIL**"))
            invalid = [["write-back", "case", value] for value in (True, -1, MAX_JOURNAL_OBSERVATIONS, "0")]
            for code, rows in (("0", [signal]), ("1", [signal, signal]), *(("1", [row]) for row in invalid)):
                with self.subTest(code=code, rows=rows), self.assertRaises(ValueError):
                    action.receipt_details(code, "RESIDUAL_FOUND", json.dumps(rows))

    def test_committed_operator_binding_needs_replayed_candidate_provenance(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(StringIO()):
            folder = Path(tmp)
            repo = folder / "app"
            repo.mkdir()
            self.git(repo, "init")
            self.git(repo, "config", "user.email", "copy-scan@example.invalid")
            self.git(repo, "config", "user.name", "Copy Scan")
            fixture = repo / "case.py"
            fixture.write_bytes((ROOT / "setrya/erasure/workspace_starter.py").read_bytes())
            app = repo / "answers.py"
            app.write_text(REDIS_SOURCE)
            site = next(row for row in copy_sites.scan(repo)["sites"] if row["kind"] == "cache")
            binding = {"site_id": site["site_id"], "test": fixture.name, "store": "notes", "key": "customer-note"}
            (repo / copy_sites.MAP_FILE).write_text(json.dumps({"format": copy_sites.MAP_FORMAT, "sites": [binding]}))
            app.write_text(REDIS_SOURCE.replace("    cache.set(key, answer)", "    return answer"))
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-m", "Approve the synthetic test mapping")
            base = self.git(repo, "rev-parse", "HEAD")
            app.write_text(REDIS_SOURCE)
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-m", "Enable answer copy")
            candidate = self.git(repo, "rev-parse", "HEAD")
            clean, dirty = folder / "clean", folder / "dirty"
            with contextlib.chdir(repo), patch.dict(os.environ, {"PYTHONPATH": str(ROOT)}):
                self.assertEqual(main(["run", "--starter", str(fixture), "--output", str(clean)]), 0)
                app.write_text("# Uncommitted code\n")
                self.assertEqual(main(["run", "--starter", str(fixture), "--output", str(dirty)]), 0)
            _, = copy_sites.check(repo, base, candidate)["sites"]
            reviewed, = copy_sites.check(repo, base, candidate, receipts=(clean,))["sites"]
            self.assertEqual(reviewed["status"], "evidence-replayed")
            self.assertEqual(reviewed["receipt_sha256"], sha256((clean / "erasure-receipt.json").read_bytes()).hexdigest())
            self.assertEqual(copy_sites.check(repo, base, candidate, receipts=(dirty,))["review_required"], 1)

            app.write_text(REDIS_SOURCE.replace("def save_answer", "from celery import shared_task\n@shared_task\ndef save_answer"))
            self.git(repo, "add", "answers.py")
            self.git(repo, "commit", "-m", "Defer the existing answer write")
            deferred = self.git(repo, "rev-parse", "HEAD")
            deletion, late = folder / "deletion", folder / "late"
            with contextlib.chdir(repo), patch.dict(os.environ, {"PYTHONPATH": str(ROOT)}):
                self.assertEqual(main(["run", "--starter", str(fixture), "--output", str(deletion)]), 0)
                self.assertEqual(main(["run", "--starter", str(fixture), "--output", str(late), "--mode", "late-write"]), 0)
            scanned, = copy_sites.check(repo, candidate, deferred, receipts=(deletion,))["sites"]
            self.assertEqual(scanned["site_id"], site["site_id"])
            self.assertEqual(scanned["writer"], "celery")
            self.assertEqual(scanned["status"], "needs-review")
            self.assertEqual(copy_sites._mapping(json.dumps({"format": copy_sites.MAP_FORMAT,
                             "sites": [scanned["starter"]]}).encode())[site["site_id"]]["mode"], "late-write")
            reviewed, = copy_sites.check(repo, candidate, deferred, receipts=(deletion, late))["sites"]
            self.assertEqual(reviewed["status"], "evidence-replayed")
            self.assertEqual(reviewed["receipt_sha256"], sha256((late / "erasure-receipt.json").read_bytes()).hexdigest())
