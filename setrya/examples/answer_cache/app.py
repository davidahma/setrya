"""Disposable authenticated answer endpoint and a separately scheduled Celery cache fill."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

from setrya.integrations.celery import CeleryCheckpoint
from .contract import QUESTION, SCOPES
from .store import Store
from .worker import TASK, broker


class _AnswerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        app = self.server.application
        if self.path != "/answer":
            self.send_error(404)
            return
        caller = app.principals.get(self.headers.get("Authorization", ""))
        if caller is None:
            self.send_error(401)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                raise ValueError
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict) or set(payload) != {"question"} or payload["question"] not in tuple(SCOPES):
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        try:
            answer = app.store.answer(caller, payload["question"], app.read_attempt)
        except Exception:
            self.send_error(500)
            return
        body = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Application:
    def __init__(self, run_id, mode, directory):
        self.store = Store(run_id, mode)
        self.run_id, self.mode = run_id, mode
        self.attempt = uuid4().hex
        self.read_attempt = self.attempt
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.scratch = TemporaryDirectory(prefix="answer-", dir=directory)
        self.work = Path(self.scratch.name).resolve()
        self.tokens = {name: secrets.token_urlsafe(32) for name in ("revoked", "retained")}
        self.principals = {"Bearer " + token: name for name, token in self.tokens.items()}
        self.server = self.thread = self.process = self.producer = self.gate = None
        self.dispatched = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def seed(self):
        self.store.seed()
        (self.work / "messages").mkdir()
        (self.work / "control").mkdir()
        self.gate = CeleryCheckpoint.create(self.work / "gate.sqlite", run_id=self.run_id, task_id=self.attempt)
        self.producer = broker(self.work, self.run_id)
        with (self.work / "worker.log").open("wb") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "setrya.examples.answer_cache.worker", str(self.work), self.run_id, self.mode],
                cwd=Path(__file__).resolve().parents[3], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        deadline = monotonic() + 30
        while not (self.work / "ready").exists():
            self._worker_alive()
            if monotonic() >= deadline:
                raise RuntimeError("Celery worker did not become ready")
            sleep(0.05)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _AnswerHandler)
        self.server.application = self
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def _worker_alive(self):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Celery worker is unavailable")

    def start_old(self, caller="revoked", question=QUESTION):
        self._worker_alive()
        if self.dispatched:
            raise RuntimeError("cache fill already dispatched")
        self.dispatched = True
        self.producer.send_task(TASK, args=(caller, question, True), task_id=self.attempt)
        return self.attempt

    def checkpoint(self):
        self._worker_alive()
        return self.gate.checkpoint()

    def captured(self):
        return self.store.captured(self.attempt)

    def refill(self, caller, question):
        self._worker_alive()
        self.read_attempt = uuid4().hex
        self.producer.send_task(TASK, args=(caller, question, False), task_id=self.read_attempt)
        return self.read_attempt

    def revoke(self):
        return self.store.revoke()

    def release(self):
        self.gate.release()

    def receipt(self, attempt=None):
        self._worker_alive()
        return self.store.receipt(attempt or self.attempt)

    def answer(self, caller, question=QUESTION):
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", "/answer", json.dumps({"question": question}), {
                "Authorization": "Bearer " + self.tokens[caller], "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                return {"http_status": response.status}
            body = json.load(response)
            return {**body, "http_status": response.status}
        finally:
            connection.close()

    def close(self):
        try:
            if self.server:
                if self.thread and self.thread.is_alive():
                    self.server.shutdown()
                    self.thread.join()
                self.server.server_close()
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            if self.producer:
                self.producer.close()
        finally:
            try:
                self.store.close()
            finally:
                self.scratch.cleanup()
