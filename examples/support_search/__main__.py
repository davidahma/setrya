"""Run the reference app or its independent background worker."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
from socketserver import TCPServer
import sqlite3
import sys

from .app import App, MODES, work


class FixtureServer(HTTPServer):
    def server_bind(self):
        # HTTPServer resolves the bound IP through getfqdn(). The fixture serves
        # a literal loopback address and needs neither local nor external DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def do_POST(self):
        try:
            self.handle_request()
        except (ValueError, TypeError, KeyError):
            self.respond(400, {"error": "invalid fixture request"})
        except (RuntimeError, sqlite3.Error):
            self.respond(500, {"error": "fixture operation failed"})

    def handle_request(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 <= length <= 65_536:
            return self.respond(400, {"error": "invalid request size"})
        payload = json.loads(self.rfile.read(length)) if length else {}
        app = self.server.app
        if self.path == "/search":
            return self.respond(200, app.search(**payload))
        if self.headers.get("X-Fixture-Key") != self.server.key:
            return self.respond(403, {"error": "invalid fixture key"})
        operations = {
            "/fixture/seed": app.seed, "/fixture/start": app.start_old, "/fixture/checkpoint": app.checkpoint,
            "/fixture/publish": app.publish, "/fixture/release": app.release, "/fixture/receipt": app.receipt,
        }
        operation = operations.get(self.path)
        if operation is None:
            return self.respond(404, {"error": "unknown fixture operation"})
        return self.respond(200, operation())

    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def terminate(_signum, _frame):
    raise SystemExit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["serve", "worker"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("run_id")
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("scenario", choices=["refund", "retired"])
    args = parser.parse_args(argv)
    if args.operation == "worker":
        work(args.directory, args.mode, args.scenario)
        return 0

    signal.signal(signal.SIGTERM, terminate)
    app = App(args.directory, args.run_id, args.mode, args.scenario)
    try:
        with FixtureServer(("127.0.0.1", 0), Handler) as server:
            server.key = os.environ.get("SETRYA_FIXTURE_KEY", args.run_id)
            server.app = app
            ready = args.directory / "port"
            pending = ready.with_suffix(".tmp")
            pending.write_text(str(server.server_port), encoding="ascii")
            pending.replace(ready)
            server.serve_forever()
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
