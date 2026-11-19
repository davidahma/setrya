"""One normal HTTP request inside the trial-only Docker network."""

import json
import sys
from contextlib import closing
from http.client import HTTPConnection


def main():
    call = json.load(sys.stdin)
    headers = {"Content-Type": "application/json"}
    if call["token"]:
        headers["Authorization"] = "Bearer " + call["token"]
    if call["workspace"]:
        headers["OD-Workspace"] = call["workspace"]
    body = None if call["body"] is None else json.dumps(call["body"]).encode()
    # HTTPConnection makes one request to this service and never follows redirects.
    with closing(HTTPConnection("app", 8080, timeout=30)) as connection:
        connection.request(
            call["method"], "/v1" + call["path"], body=body, headers=headers
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            json.dump([response.status, {}], sys.stdout)
            return
        raw = response.read()
        json.dump([response.status, json.loads(raw) if raw else {}], sys.stdout)


if __name__ == "__main__":
    main()
