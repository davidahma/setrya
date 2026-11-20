"""A disposable approvals app for testing the public pytest starter over HTTP."""

from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from secrets import token_urlsafe
from threading import Event, Thread

from setrya.adapter import Checkpoint, Publication, Receipt
from setrya.endpoint import EndpointCase


CASE = EndpointCase(
    route="approval-assistant", revoked="reviewer", retained="owner",
    question="Which approval level applies?", private_source="limits@2",
    public_source="handbook@5", private_answer="The synthetic threshold is seventeen.",
    public_answer="Ask the test supervisor.", document="approvals",
    old_revision="team", new_revision="restricted", checkpoint="prepared",
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        app = self.server.app
        principal = app.sessions.get(self.headers.get("Authorization"))
        if principal is None or self.path != "/approvals":
            self.send_error(403)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cached = app.cache.get(principal)
        body, sources, writer = cached or (CASE.public_answer, [CASE.public_source], None)
        answer = {"answer": body, "sources": sources, "route": "approval-assistant", "caller": principal,
                  "cache_hit": cached is not None, "cache_writer": writer,
                  "query_sha256": sha256(request["prompt"].encode()).hexdigest(),
                  "generation": app.run_id, "commit": app.position}
        if app.mode == "wrong-principal" and app.finished.is_set():
            answer["caller"] = "other-reader"
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Application:
    def __init__(self, run_id):
        self.run_id = run_id
        self.mode = os.environ["APP_TEST_MODE"]
        self.sessions = {"Bearer " + token_urlsafe(): user for user in ("reviewer", "owner")}
        self.old = ("The synthetic threshold is seventeen.", ["limits@2", "handbook@5"], "seed")
        self.cache = {}
        self.position = 1
        self.granted = True
        self.held, self.resume, self.finished = Event(), Event(), Event()
        self.worker = None

    def __enter__(self):
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self
        self.http = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.http.start()
        return self

    def __exit__(self, *exc):
        self.resume.set()
        if self.worker:
            self.worker.join(4)
        self.server.shutdown()
        self.http.join()
        self.server.server_close()

    def seed(self):
        self.cache = dict.fromkeys(self.sessions.values(), self.old)

    def start_old(self):
        def fill():
            body, sources, _ = self.cache["reviewer"]
            self.capture = {"run_id": self.run_id, "attempt": "job-73", "caller": "reviewer",
                            "revision": "team", "question": "Which approval level applies?",
                            "answer": body, "sources": sources}
            self.held.set()
            if not self.resume.wait(5):
                return
            if self.granted or self.mode == "broken":
                self.cache["reviewer"] = (body, sources, "job-73")
                self.position += 1
                self.outcome = Receipt(self.run_id, "job-73", "approvals", "team",
                                       "committed", self.run_id, self.position)
            else:
                self.outcome = Receipt(self.run_id, "job-73", "approvals", "team", "rejected")
            self.finished.set()

        self.worker = Thread(target=fill)
        self.worker.start()
        return "job-73"

    def checkpoint(self):
        if self.held.is_set() and not self.resume.is_set():
            return Checkpoint(self.run_id, "job-73", "approvals", "team", "prepared")
        return None

    def captured(self):
        return self.capture if self.held.is_set() else None

    def revoke(self):
        self.granted = False
        del self.cache["reviewer"]
        return Publication(self.run_id, "approvals", "restricted")

    def release(self):
        self.resume.set()

    def receipt(self):
        return self.outcome if self.finished.is_set() else None

    def answer(self, caller, question):
        token = next(key for key, user in self.sessions.items() if user == caller)
        client = HTTPConnection(*self.server.server_address, timeout=3)
        try:
            client.request("POST", "/approvals", json.dumps({"prompt": question}), {"Authorization": token})
            response = client.getresponse()
            return {**json.load(response), "http_status": response.status}
        finally:
            client.close()
