"""Run the pinned opendesk trial in a newly allocated, disposable Compose project."""

import argparse
import base64
import hmac
from importlib.resources import files
import json
import os
import secrets
import struct
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from .verify_report import (
    HEALTHY_V2,
    LATEST_VERSION,
    METADATA,
    TARGET,
    UNTESTED,
    outcome,
    validate,
)

HERE = Path(__file__).resolve().parent
COMPOSE_TIMEOUTS = {"config": 30, "up": 15 * 60, "exec": 60, "down": 90}
SUPPORT_EMAIL = "agent@example.test"
CASES = {
    "Acme": ("copper compass", "Route returns to the copper-compass desk."),
    "Birch": ("violet sail", "Route returns to the violet-sail desk."),
}


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class Stack:
    def __init__(self, compose_dir=HERE):
        self.compose_dir = compose_dir
        self.project = "setrya-od-" + secrets.token_hex(6)
        self.env = os.environ.copy()
        for name in (
            "DATABASE_PASSWORD",
            "AUTH_SECRET",
            "CREDENTIALS_KEY",
            "NATS_TOKEN",
            "MODEL_KEY",
        ):
            self.env["TRIAL_" + name] = secrets.token_hex(32)

    def compose(self, *args, stdin=None, capture=False):
        command = [
            "docker",
            "compose",
            "-f",
            str(self.compose_dir / "compose.yaml"),
            "-p",
            self.project,
            *args,
        ]
        result = subprocess.run(
            command,
            input=stdin,
            env=self.env,
            text=True,
            check=True,
            timeout=COMPOSE_TIMEOUTS[args[0]],
            stdout=subprocess.PIPE if capture else sys.stderr,
        )
        return result.stdout.strip() if capture else None

    def sql(self, role, statement):
        return self.compose(
            "exec",
            "-T",
            "database",
            "psql",
            "-XqAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            role,
            "-d",
            "opendesk",
            stdin=statement,
            capture=True,
        )


class API:
    def __init__(self, stack):
        self.stack = stack

    def request(
        self,
        method,
        path,
        body=None,
        token=None,
        workspace=None,
        allowed=(200, 201, 204),
    ):
        wire = {
            "method": method,
            "path": path,
            "body": body,
            "token": token,
            "workspace": workspace,
        }
        result = self.stack.compose(
            "exec",
            "-T",
            "model",
            "python",
            "-B",
            "/stub/transport.py",
            stdin=json.dumps(wire),
            capture=True,
        )
        status, value = json.loads(result)
        if allowed is not None and status not in allowed:
            code = value.get("code", "unclassified")
            raise RuntimeError(f"{method} {path} returned HTTP {status} ({code})")
        return status, value

    def mutate(self, path, body, token=None, workspace=None):
        return self.request("POST", path, body, token, workspace)[1]


def account(api, email, name):
    password = secrets.token_urlsafe(28) + "aA9!"
    user = api.mutate(
        "/auth/register", {"email": email, "password": password, "name": name}
    )
    return user["access_token"], password


def verified_owner(api):
    email = "owner@example.test"
    token, password = account(api, email, "Demo Owner")
    seed = api.mutate("/auth/totp/setup", {}, token)["secret"]
    key = base64.b32decode(seed + "=" * (-len(seed) % 8))
    digest = hmac.digest(key, struct.pack(">Q", int(time.time()) // 30), "sha1")
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFF_FFFF
    recovery = api.mutate(
        "/auth/totp/enable", {"code": f"{number % 1_000_000:06d}"}, token
    )
    login = api.mutate("/auth/login", {"email": email, "password": password})
    return api.mutate(
        "/auth/mfa",
        {
            "mfa_token": login["mfa_token"],
            "code": recovery["recovery_codes"][0],
        },
    )["access_token"]


def seed(api, owner, agent, label):
    workspace = str(UUID(api.mutate("/workspaces", {"name": label}, owner)["id"]))
    invitation = api.mutate(
        "/invitations", {"email": SUPPORT_EMAIL, "role": "agent"}, owner, workspace
    )
    link = api.mutate(
        "/invitations/" + invitation["id"] + "/link", {}, owner, workspace
    )
    invite_token = parse_qs(urlsplit(link["url"]).query)["token"][0]
    api.mutate("/invitations/accept", {"token": invite_token}, agent)
    marker, summary = CASES[label]
    ticket = api.mutate(
        "/tickets",
        {
            "subject": "Returns desk handoff",
            "channel": "api",
            "kind": "reply",
            "direction": "inbound",
            "contact": {"name": "Demo Contact", "email": "contact@example.test"},
            "body_html": "<p>Use the "
            + marker
            + " reference for the returns desk.</p>",
        },
        owner,
        workspace,
    )
    ticket_id = str(UUID(ticket["id"]))
    messages = api.request(
        "GET", "/tickets/" + ticket_id + "/messages", token=agent, workspace=workspace
    )[1]
    source_ids = [
        item["id"] for item in messages["data"] if marker in item["body_text"]
    ]
    if len(source_ids) != 1:
        raise RuntimeError("Could not establish an unambiguous source message")
    answer = api.mutate("/tickets/" + ticket_id + "/ai/summary", {}, agent, workspace)
    if answer.get("summary") != summary:
        raise RuntimeError(
            "Summary endpoint did not return the expected synthetic derivative"
        )
    return workspace, ticket_id, str(UUID(source_ids[0]))


def summary_state(summary, marker):
    """Classify only attributable strings in the native text/key_points fields."""
    if summary is None or summary == {}:
        return "ABSENT_BY_ID"
    if not isinstance(summary, dict):
        return "INCONCLUSIVE"
    text = summary.get("text")
    points = summary.get("key_points", [])
    values = [text, *(points if isinstance(points, list) else [])]
    if any(
        isinstance(item, str) and marker in item.casefold().replace("-", " ")
        for item in values
    ):
        return "FOUND"
    if (
        (text is None or text == "")
        and points == []
        and set(summary)
        <= {
            "text",
            "key_points",
            "sentiment",
            "updated_at",
            "message_count",
        }
    ):
        return "ABSENT_BY_ID"
    return "INCONCLUSIVE"


def agent_read(status, value, ticket, marker):
    if status != 200:
        reason = "http_error"
        if (
            status == 400
            and isinstance(value, dict)
            and value.get("code") == "workspace_required"
        ):
            reason = "workspace_required"
        return "INCONCLUSIVE", status, reason
    if (
        not isinstance(value, dict)
        or value.get("id") != ticket
        or not isinstance(value.get("custom"), dict)
    ):
        return "INCONCLUSIVE", status, "unexpected_ticket"
    result = summary_state(value["custom"].get("ai_summary"), marker)
    if result == "INCONCLUSIVE":
        return result, status, "unexpected_summary"
    return ("RETURNED" if result == "FOUND" else "NOT_RETURNED"), status, "none"


READ_ERRORS = (
    RuntimeError,
    ValueError,
    OSError,
    TypeError,
    KeyError,
    subprocess.SubprocessError,
)


def observe(stack, api, agent, label, ids, row):
    workspace, ticket, message = ids
    # Each bound identifier was parsed as UUID above. Look up the records by
    # their own primary keys so workspace visibility cannot hide a leftover.
    marker = CASES[label][0]
    try:
        source = stack.sql(
            "axiom_observer",
            f"SELECT count(*) FROM messages WHERE id = '{message}'::uuid;",
        )
        row["source_message_row"] = {"0": "ABSENT_BY_ID", "1": "FOUND"}[source]
    except READ_ERRORS:
        row["source_message_row"] = "INCONCLUSIVE"
    try:
        raw = stack.sql(
            "axiom_observer",
            f"""SELECT COALESCE((SELECT CASE WHEN jsonb_typeof(custom) = 'object'
                         THEN custom->'ai_summary' ELSE 'false'::jsonb END
                         FROM tickets WHERE id = '{ticket}'::uuid), 'null'::jsonb);""",
        )
        row["stored_ticket_summary"] = summary_state(json.loads(raw), marker)
    except READ_ERRORS:
        row["stored_ticket_summary"] = "INCONCLUSIVE"
    try:
        status, value = api.request(
            "GET", "/tickets/" + ticket, token=agent, workspace=workspace, allowed=None
        )
        result = agent_read(status, value, ticket, marker)
    except READ_ERRORS:
        result = "INCONCLUSIVE", None, "read_error"
    row["agent_summary"], row["agent_http_status"], row["agent_read_reason"] = result


def checkpoint(stack, api, agent, ids, report, phase):
    rows = {label: UNTESTED.copy() for label in CASES}
    report["observations"].append(
        {"phase": phase, "observed_at": now(), "workspaces": rows}
    )
    for label, item in ids.items():
        observe(stack, api, agent, label, item, rows[label])
    return rows


def run(stack, report):
    config = stack.compose(
        "config", "--no-interpolate", "--format", "json", capture=True
    )
    app_image = json.loads(config)["services"]["app"]["image"]
    expected = f"ghcr.io/useopendesk/opendesk:{TARGET['version']}@{TARGET['image']}"
    if app_image != expected:
        print("Compose app image differs from the report target.", file=sys.stderr)
        raise ValueError("Unexpected opendesk image")
    stack.compose("up", "--detach", "--wait", "--wait-timeout", "150")
    api = API(stack)
    stack.sql(
        "opendesk",
        """
        CREATE ROLE axiom_observer LOGIN;
        GRANT SELECT (id) ON messages TO axiom_observer;
        GRANT SELECT (id, custom) ON tickets TO axiom_observer;
    """,
    )
    report["interrupted_at"] = "seeding"
    owner = verified_owner(api)
    agent, _ = account(api, SUPPORT_EMAIL, "Demo Agent")
    ids = {label: seed(api, owner, agent, label) for label in CASES}

    report["interrupted_at"] = "baseline"
    baseline = checkpoint(stack, api, agent, ids, report, "before_offboarding")
    if any(row != HEALTHY_V2 for row in baseline.values()):
        return
    report["interrupted_at"] = "offboarding"
    status, _ = api.request(
        "DELETE",
        "/workspace",
        {"confirm_name": "Acme"},
        owner,
        ids["Acme"][0],
        allowed=(204,),
    )
    report["offboarding_http_status"] = status
    report["interrupted_at"] = "observation"
    checkpoint(stack, api, agent, ids, report, "after_offboarding")
    report["interrupted_at"] = None


def capture(report_path, stack):
    evidence = {
        **METADATA,
        "report_version": LATEST_VERSION,
        "outcome": "INCONCLUSIVE",
        "interrupted_at": "startup",
        "observations": [],
        "offboarding_http_status": None,
    }
    try:
        run(stack, evidence)
    except (
        RuntimeError,
        ValueError,
        OSError,
        TypeError,
        AttributeError,
        IndexError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            f"Trial interrupted at {evidence['interrupted_at']} ({type(exc).__name__})",
            file=sys.stderr,
        )
    finally:
        try:
            stack.compose("down", "--volumes", "--remove-orphans")
        except (OSError, subprocess.SubprocessError):
            print(
                f"Cleanup failed for disposable project {stack.project}",
                file=sys.stderr,
            )
            evidence["interrupted_at"] = "cleanup"
    evidence["outcome"] = outcome(evidence["observations"], evidence["report_version"])
    if evidence["interrupted_at"] and evidence["outcome"] != "FAIL":
        evidence["outcome"] = "INCONCLUSIVE"
    validate(evidence)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=report_path.parent, delete=False
    ) as output:
        json.dump(evidence, output, indent=2)
        output.write("\n")
    Path(output.name).replace(report_path)
    return 0 if evidence["outcome"] == "COMPLETED" else 1


def collect(report_path):
    # Compose needs ordinary files for the model server's bind mounts, including in a zipapp.
    with tempfile.TemporaryDirectory(prefix="setrya-opendesk-") as directory:
        compose_dir = Path(directory)
        assets = files("examples.opendesk")
        for name in ("compose.yaml", "transport.py", "provider.py"):
            (compose_dir / name).write_bytes(assets.joinpath(name).read_bytes())
        return capture(report_path, Stack(compose_dir))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    return collect(parser.parse_args().report)


if __name__ == "__main__":
    sys.exit(main())
