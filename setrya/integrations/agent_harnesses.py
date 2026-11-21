"""Optional, locally configured observation hooks for agent runs and trace sessions."""
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from setrya.drill import InputError
from setrya.erasure.adapters import _sql_name
from setrya.erasure.canary import LIMIT, SQLiteCanaryScanner, contains_marker, validate_probe
from setrya.erasure.hooks import CopyHooks
from setrya.erasure.journal import identifier


async def run_openai_agent(hooks: CopyHooks, agent, inputs, *, session, store: str, source: str, **options):
    """Run the native Agents runner with a local SQLiteSession, recording the session pointer."""
    from agents import Runner, SQLiteSession
    if not isinstance(session, SQLiteSession):
        raise InputError("provide an OpenAI Agents SQLiteSession")
    key = identifier(session.session_id, "OpenAI Agents session ID")
    hooks._attempt(store, key, source, "stored", operation="agents.session")
    result = await Runner.run(agent, inputs, session=session, **options)
    hooks._record(store, key, source, "stored", stage="returned", operation="agents.session")
    return result


class OpenAISessionCanaryScanner(SQLiteCanaryScanner):
    """Read message rows in a native Agents SQLiteSession database, including abandoned history."""

    def __init__(self, connection, *, messages_table="agent_messages"):
        self.db = connection
        self.query = (f'SELECT t.id AS copy_key FROM {_sql_name(messages_table)} t WHERE t.session_id COLLATE BINARY=? '
                      'AND EXISTS(SELECT 1 FROM json_tree(t.message_data) j '
                      "WHERE instr(CASE WHEN j.type='text' THEN j.atom END,?2)>0 OR instr(j.key,?2)>0) LIMIT 4097")


def openai_response(hooks: CopyHooks, client, request, *, store: str, key: str, source: str,
                    invocation_logging=None, prompt_caching=None, sources=()):
    """Send a synchronous Responses SDK request. Provider-side retention stays external."""
    model = identifier(request.get("model"), "model")
    return hooks._provider(client.responses.create, request, provider="openai", model=model, region=None,
                           store=store, key=key, source=source, invocation_logging=invocation_logging,
                           prompt_caching=prompt_caching, sources=sources, operation="responses.create")


def claude_tool_hooks(hooks: CopyHooks, *, session: str, store: str, source: str):
    """Return native ClaudeAgentOptions.hooks. Scope each tool input and result to the run."""
    from claude_agent_sdk import HookMatcher

    identifier(session, "Claude session ID")

    async def observe(event, tool_use_id, context):
        if event["session_id"] != session:
            raise InputError("Claude tool callback came from a different session")
        name = identifier(event["tool_name"], "Claude tool name")
        call_id = identifier(tool_use_id or event.get("tool_use_id"), "Claude tool use ID")
        base = f"{session}/{call_id}"
        if event["hook_event_name"] == "PreToolUse":
            hooks._attempt(store, base + "/input", source, "sent", tool=name, direction="input",
                           **hooks._fingerprint(event["tool_input"]))
        elif event["hook_event_name"] == "PostToolUse":
            hooks._record(store, base + "/result", source, "derived", stage="returned", tool=name,
                          direction="result", **hooks._fingerprint(event["tool_response"]))
        else:
            raise InputError("unexpected Claude hook event")
        return {}

    return {name: [HookMatcher(hooks=[observe])] for name in ("PreToolUse", "PostToolUse")}


class ClaudeTranscriptCanaryScanner:
    """Read every record in one declared local Claude Code session transcript."""

    def __init__(self, project_transcripts: str | Path):
        self.directory = Path(project_transcripts).resolve(strict=True)
        if not self.directory.is_dir():
            raise InputError("declare the directory holding this project's Claude transcripts")

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        try:
            if str(UUID(scope)) != scope:
                raise ValueError
        except ValueError:
            raise InputError("expected a canonical Claude session UUID") from None
        path = self.directory / (scope + ".jsonl")
        if path.is_symlink():
            raise InputError("a Claude transcript must be a regular file in the declared directory")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise InputError("a Claude transcript must be a regular file")
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise InputError("Claude transcript exceeds the byte budget")
        found = False
        for line in raw.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("sessionId", scope) != scope:
                raise InputError("unexpected Claude transcript record or session")
            found |= contains_marker(value, marker)
        return {scope} if found else set()


class LangfuseCanaryScanner:
    """Read observation inputs/outputs using Langfuse's Observations v2 cursor API."""

    def __init__(self, client, *, project_id: str, control: str):
        self.client = client
        self.project = identifier(project_id, "Langfuse project")
        self.control = identifier(control, "retained Langfuse trace/observation")

    def find(self, *, scope: str, marker: str) -> set[str]:
        validate_probe(scope, marker)
        found, seen, cursors = set(), set(), set()
        cursor = None
        while True:
            page = self.client.api.observations.get_many(session_id=scope, fields="core,basic,io", limit=100,
                                                         cursor=cursor)
            for row in page.data:
                if row.project_id != self.project or row.session_id != scope:
                    raise InputError("Langfuse observation belongs to a different project or session")
                if not {"input", "output"} <= row.model_fields_set:
                    raise InputError("Langfuse did not return the requested input/output fields")
                trace = identifier(row.trace_id, "Langfuse trace")
                observation = identifier(row.id, "Langfuse observation")
                key = identifier(trace + "/" + observation, "Langfuse copy key")
                if key in seen or len(seen) >= LIMIT:
                    raise InputError("duplicate observation or oversized Langfuse scan")
                seen.add(key)
                if any(_trace_field_matches(field, marker) for field in (row.input, row.output)):
                    found.add(key)
            cursor = page.meta.cursor
            if cursor is None:
                if self.control not in seen:
                    raise InputError("retained Langfuse observation is missing; check scope and ingestion")
                return found
            if not cursor or cursor in cursors or not page.data:
                raise InputError("incomplete Langfuse cursor traversal")
            cursors.add(cursor)


def _trace_field_matches(field, marker):
    if isinstance(field, str):
        if marker in field:
            return True
        try:
            field = json.loads(field)
        except ValueError:
            pass  # A plain string is a valid trace input or output.
    return contains_marker(field, marker)


@contextmanager
def langfuse_observation(hooks: CopyHooks, client, *, session: str, store: str, source: str, name: str, **fields):
    """Open a real Langfuse span and journal its pointer. The scanner checks export independently."""
    from langfuse import propagate_attributes
    identifier(session, "Langfuse session")
    with propagate_attributes(session_id=session), client.start_as_current_observation(name=name, **fields) as span:
        key = identifier(f"{span.trace_id}/{span.id}", "Langfuse trace/observation")
        hooks._attempt(store, key, source, "stored", operation="langfuse.observation")
        yield span
    hooks._record(store, key, source, "stored", stage="returned", operation="langfuse.observation")
