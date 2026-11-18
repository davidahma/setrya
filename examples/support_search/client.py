"""Fixture HTTP client shared by both consumers. No Setrya dependency."""

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4


class FixtureError(RuntimeError):
    pass


class Client:
    def __init__(self, directory: Path, run_id: str, mode: str, scenario: str, *, module="examples.support_search"):
        self.key = uuid4().hex
        ready = directory / "port"
        self.process = subprocess.Popen(
            [sys.executable, "-m", module, "serve", str(directory), run_id, mode, scenario],
            env={**os.environ, "SETRYA_FIXTURE_KEY": self.key},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until and self.process.poll() is None:
                if ready.exists():
                    self.port = int(ready.read_text(encoding="ascii"))
                    return
                time.sleep(0.01)
            raise FixtureError("search fixture did not start")
        except BaseException:
            self.close()
            raise

    def request(self, endpoint, **payload):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("POST", endpoint, json.dumps(payload), {
                "Content-Type": "application/json", "X-Fixture-Key": self.key,
            })
            response = connection.getresponse()
            if response.status != 200:
                raise FixtureError(f"search fixture returned HTTP {response.status}")
            return json.loads(response.read())
        finally:
            connection.close()

    def seed(self): return self.request("/fixture/seed")
    def start_old(self): return self.request("/fixture/start")
    def checkpoint(self): return self.request("/fixture/checkpoint")
    def publish(self): return self.request("/fixture/publish")
    def release(self): return self.request("/fixture/release")
    def receipt(self): return self.request("/fixture/receipt")

    def search(self, question, route, scope, top_k):
        return self.request("/search", question=question, route=route, scope=scope, top_k=top_k)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
