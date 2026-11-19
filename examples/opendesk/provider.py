"""Deterministic local summary producer for the two synthetic ticket bodies."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

TICKETS = {
    "copper compass": "Route returns to the copper-compass desk.",
    "violet sail": "Route returns to the violet-sail desk.",
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= 128_000:
            self.send_error(400)
            return
        request = json.loads(self.rfile.read(size))
        prompt = json.dumps(request.get("messages", [])).lower()
        matches = [summary for marker, summary in TICKETS.items() if marker in prompt]
        if len(matches) != 1:
            self.send_error(422)
            return
        content = {"summary": matches[0], "key_points": [], "sentiment": "neutral"}
        body = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(content),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
