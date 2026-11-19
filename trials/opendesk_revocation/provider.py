"""An OpenAI-compatible synthetic provider with an observable inference barrier."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEED = "The copper compass is ready."
UPDATED = "The copper compass was recalibrated."
MARKER = "recalibration-scheduled"


class Handler(BaseHTTPRequestHandler):
    def reply(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/trial/state":
            with self.server.lock:
                self.reply({"calls": self.server.calls, "released": self.server.release.is_set()})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/trial/release":
            self.server.release.set()
            self.reply({})
            return
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= 128000:
            self.send_error(400)
            return
        prompt = json.dumps(json.loads(self.rfile.read(size)).get("messages", [])).lower()
        if "copper compass" not in prompt:
            self.send_error(422)
            return
        summary = SEED
        if MARKER in prompt:
            with self.server.lock:
                self.server.calls += 1
            if not self.server.release.wait(65):
                self.send_error(504)
                return
            summary = UPDATED
        content = {"summary": summary, "key_points": [], "sentiment": "neutral"}
        self.reply({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10}})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.lock = threading.Lock()
    server.release = threading.Event()
    server.calls = 0
    server.serve_forever()
