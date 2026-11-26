"""Emit editable connection bindings for an existing, seeded application."""
from importlib.resources import files
from pathlib import Path

STACKS = ("langgraph-postgres", "openai-agents", "claude-agent", "redis-cache", "typescript")
SESS = "43815787-7631-4e38-a59c-b7c73f6aaa21"

_TEMPLATE = """# Point only at an isolated, seeded fixture. The run calls its DELETE route.
format: setrya.http-case.v1
conversation: ticket-204
subject_ref_env: SETRYA_SUBJECT_REF
origin_env: SETRYA_TEST_ORIGIN
read_route: /answer
erase_route: /erasure
caller_tokens:
  duty-agent: SETRYA_READER_TOKEN
erase_token_env: SETRYA_ERASER_TOKEN
samples: 3
facts:
  target:
    marker: Harbor-17
    questions: ["What was the customer's depot code?", "Which depot handled this ticket?"]
    answers:
      present: ["Harbor-17", "Harbor seventeen"]
      absent: ["Thursday", "No matching note."]
{target_stored}  control:
    marker: Thursday
    questions: ["When is dispatch?"]
    answers:
      present: ["Thursday"]
      absent: ["Harbor-17", "Harbor seventeen", "No matching note."]
{control_stored}stores:
  memory:
    kind: {stack}
    connection_env: {env}
    scope: {scope}
{options}    artifacts:
{artifacts}
{extra_stores}"""



def init_stack(path: Path, stack: str):
    if stack == "typescript":
        template = files("setrya.erasure.recipes").joinpath("typescript")
        path.mkdir(parents=True, exist_ok=False)
        for name in ("case.yml", "fixture.mts", "run.py"):
            (path / name).write_bytes(template.joinpath(name).read_bytes())
        return path / "case.yml"
    settings = {
        "langgraph-postgres": ("SETRYA_TEST_POSTGRES_URL", "ticket-204", "", "      note: delete"),
        "openai-agents": ("SETRYA_TEST_AGENTS_DB", "ticket-204", "", '      "1": delete'),
        "claude-agent": ("SETRYA_TEST_TRANSCRIPTS", SESS, "", f"      {SESS}: rewrite"),
        "redis-cache": ("SETRYA_TEST_REDIS_URL", "ticket-204", "    prefix: support\n", "      note: delete"),
    }
    env, scope, options, artifacts = settings[stack]
    target_stored = ""
    control_stored = ""
    extra_stores = ""
    if stack == "claude-agent":
        target_stored = ('    stored:\n      absent:\n'
                         f'        - [{{sessionId: "{scope}", message: {{content: Thursday}}}}]\n')
    elif stack == "openai-agents":
        control_stored = "    stored:\n      absent: [{role: user, content: Harbor-17}]\n"
    elif stack == "langgraph-postgres":
        from .recipes.langgraph_postgres import CONTROL_STORED, STORES
        control_stored, extra_stores = CONTROL_STORED, STORES
    body = _TEMPLATE.format(stack=stack, env=env, scope=scope, options=options, artifacts=artifacts,
                            target_stored=target_stored, control_stored=control_stored, extra_stores=extra_stores)
    path.mkdir(parents=True, exist_ok=False)
    target = path / "case.yml"
    target.write_text(body, encoding="utf-8")
    (path / "test_erasure.py").write_bytes(files("setrya.erasure").joinpath("stack_starter.py.tmpl").read_bytes())
    return target
