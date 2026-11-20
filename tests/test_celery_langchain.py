"""Cross-process adoption and request/worker evidence for the framework starter."""

from contextlib import ExitStack
from hashlib import sha256
from importlib.util import find_spec
import os
import shutil
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from uuid import uuid4

import setrya
from setrya.adapter import Receipt
from setrya.drill import InputError
from setrya.integrations.celery_langchain import CeleryLangChainEndpoint, RedisCeleryCheckpoint, record_served


URL = os.environ.get("SETRYA_REDIS_URL")
HERE = Path(__file__).resolve().parent
LIVE = URL and all(find_spec(p) for p in ("celery", "redis", "langchain_core", "pytest"))


@unittest.skipUnless(LIVE, "requires SETRYA_REDIS_URL and the celery-langchain extra")
class CeleryLangChainTests(unittest.TestCase):
    def setUp(self):
        from redis import Redis
        self.db = Redis.from_url(URL)
        self.addCleanup(self.db.close)
        self.run_id, self.task_id = uuid4().hex, uuid4().hex

    def test_lease_expiry_and_terminal_order(self):
        client, run, task = self.db, self.run_id, self.task_id
        gate = RedisCeleryCheckpoint.create(client, run, task)
        self.addCleanup(gate.close)
        with self.assertRaises(InputError):
            RedisCeleryCheckpoint.create(client, run, task)
        with self.assertRaises(InputError):
            gate.release()
        with self.assertRaises(InputError):
            gate.hold(document="doc", revision="rev", name="after-read", caller="alice",
                      question="q", answer="private", documents=[], timeout=0.03)
        req = SimpleNamespace(id=task, headers={"setrya_test_run": run}, is_eager=False, called_directly=False,
                              retries=1, delivery_info={})
        with self.assertRaises(InputError):
            RedisCeleryCheckpoint.for_task(client, SimpleNamespace(request=req))
        req.retries = 0
        worker_gate = RedisCeleryCheckpoint.for_task(client, SimpleNamespace(request=req))
        with self.assertRaisesRegex(InputError, "resumed from this hold"):
            worker_gate.finish(Receipt(run, task, "doc", "rev", "rejected"))
        with self.assertRaises(TimeoutError):
            worker_gate.hold(document="doc", revision="rev", name="after-read", caller="alice", question="q",
                             answer="private", documents=[], timeout=0.03)
        self.assertIsNone(gate.checkpoint())
        with self.assertRaises(InputError):
            gate.release()
        self.assertIsNone(gate.receipt())

    def test_observer_rejects_missing_duplicate_and_wrong_body(self):
        client = self.db
        def send(caller, question, ticket):
            row = dict(route="search", caller="server-principal", question="served question",
                       answer="sensitive fixture text", sources=["source"], cache_hit=True, cache_writer="fill")
            if caller != "missing":
                self.assertTrue(record_served(client, ticket, **row))
                raw = client.get("setrya:served:" + ticket)
                self.assertNotIn(b"sensitive fixture text", raw)
                self.assertNotIn(b"served question", raw)
                if caller == "duplicate":
                    self.assertFalse(record_served(client, ticket, **row))
            return 200, "different public response" if caller == "wrong-body" else row["answer"]
        with CeleryLangChainEndpoint(client, self.run_id, None, task=None, seed=None, revoke=None, send=send) as app:
            for caller in ("missing", "duplicate", "wrong-body"):
                result = app.answer(caller, "requested question")
                self.assertNotIn("caller", result)
            result = app.answer("matched", "requested question")
            self.assertEqual(result["caller"], "server-principal")
            self.assertEqual(result["query_sha256"], sha256(b"served question").hexdigest())
        req = SimpleNamespace(headers={})
        self.assertIsNone(RedisCeleryCheckpoint.for_task(client, SimpleNamespace(request=req)))

    def test_generated_starter_across_worker_process_and_native_http_endpoint(self):
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            work = Path(directory)
            queue = os.environ.get("SETRYA_FRAMEWORK_EXTERNAL_QUEUE") or "setrya-" + uuid4().hex
            env = dict(os.environ, SETRYA_FRAMEWORK_QUEUE=queue, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                       PYTHONPATH=os.pathsep.join((str(Path(setrya.__file__).resolve().parents[1]), str(HERE / "fixtures"))))
            def run(*args):
                return subprocess.run([sys.executable, "-m", *args], cwd=work, env=env, timeout=60,
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            if "SETRYA_FRAMEWORK_EXTERNAL_QUEUE" not in os.environ:
                log = work / "worker.log"
                stream = cleanup.enter_context(log.open("w"))
                process = subprocess.Popen(
                    [sys.executable, "-m", "celery", "-A", "celery_endpoint_app:worker", "worker", "--pool=solo",
                     "--concurrency=1", "--without-gossip", "--without-mingle", "--without-heartbeat", "-Q", queue,
                     "--hostname=" + queue + "@%h", "--loglevel=INFO"], cwd=work, env=env,
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=stream)

                def stop():
                    process.terminate()
                    try:
                        process.wait(8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                cleanup.callback(stop)
                deadline = monotonic() + 25
                while " ready." not in log.read_text():
                    if process.poll() is not None or monotonic() > deadline:
                        self.fail("Celery test worker did not start: " + log.read_text()[-2500:])
                    sleep(0.05)

            starter = run("setrya", "init", "endpoint", "--framework", "celery-langchain")
            self.assertEqual(starter.returncode, 0, starter.stdout)
            test = work / "tests/test_setrya_endpoint.py"
            with test.open("a") as f:
                f.write("\nfrom celery_endpoint_app import Application as open_test_app\n")

            for mode, status, cli_exit in (("guarded", "PASS", 0), ("broken", "FAIL", 1), ("unobserved", "NOT TESTED", 3)):
                env["SETRYA_FRAMEWORK_TEST_MODE"] = mode
                child = run("pytest", "-q", "-s", "--basetemp", str(work / mode), str(test))
                self.assertEqual(child.returncode, int(cli_exit != 0), child.stdout)
                files = list((work / mode).rglob("answer-cache.json"))
                self.assertEqual(len(files), 1, child.stdout)
                bundle = files[0].parent
                if destination := os.environ.get("SETRYA_FRAMEWORK_EVIDENCE"):
                    copy = Path(destination) / mode
                    copy.mkdir(parents=True)
                    for name in ("answer-cache.json", "run.json"):
                        shutil.copyfile(bundle / name, copy / name)
                replay = run("setrya", "replay", str(bundle))
                self.assertEqual(replay.returncode, cli_exit, replay.stdout)
                self.assertTrue(replay.stdout.startswith(status), replay.stdout)
                if mode == "broken":
                    self.assertIn("resumed task", replay.stdout)
                elif mode == "guarded":
                    self.assertIn("cache hit", replay.stdout)
