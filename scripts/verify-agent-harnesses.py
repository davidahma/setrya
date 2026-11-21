"""Run the native harness tests against a temporary, loopback-only Langfuse stack."""
import hashlib
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

# Upstream v4.83.0 Compose file and multi-platform image indexes; no credentials needed.
COMPOSE_URL = ("https://raw.githubusercontent.com/langfuse/langfuse/"
               "fe82bdc24d3bbaac919b20d8fe8708920a9a81a8/docker-compose.yml")
COMPOSE_SHA256 = "ca711796d082a789eb6875dce856794151f3e67181d1fb53bb72547c43013f76"
IMAGES = {
    "langfuse-web": "docker.langfuse.com/langfuse/langfuse@sha256:a91445265d1f5a13d666ef2f1491a437255fb9e5b7a76525d5543e4a4dc030f3",
    "langfuse-worker": "docker.langfuse.com/langfuse/langfuse-worker@sha256:a6eac4bb99df27dd01a090e63d1a54660ba14044ae5e4c1ef1c883cec5d68ee4",
    "clickhouse": "clickhouse/clickhouse-server@sha256:de3e47c64078aa196dae2349e048fc98ba81947f651f5c20e06e46baea8ae40a",
    "minio": "cgr.dev/chainguard/minio@sha256:e9cf6b99c226e9539ed0f14b6f00e37b07da153c7ba1f4f8709c807574b471ec",
    "postgres": "postgres@sha256:b60e90042aa6b8dbecd58e224b3e36114dbf8ad754465264c2476ad58a86e45c",
    "redis": "redis@sha256:2e3eb11462c4e3759e6972678dd051d150ff54b1e60714b3c33b701260172a90",
}


def qualify(directory):
    for module in ("agents", "claude_agent_sdk", "langfuse"):
        if find_spec(module) is None:
            raise RuntimeError("install the agent-openai, agent-claude and traces-langfuse extras")
    original = directory / "upstream.yml"
    with urlopen(COMPOSE_URL, timeout=30) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != COMPOSE_SHA256:
        raise ValueError("unexpected Langfuse Compose source")
    original.write_bytes(content)
    compose = ["docker", "compose", "-p", "setrya-harness-" + uuid4().hex[:12], "-f"]
    # Resolve the upstream defaults in isolation from the user's application environment.
    docker_env = {k: v for k, v in os.environ.items()
                  if k in {"PATH", "HOME", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK"} or k.startswith("DOCKER_")}
    config = json.loads(subprocess.check_output([*compose, str(original), "config", "--format", "json"],
                                                env={**docker_env, "TELEMETRY_ENABLED": "false"}))
    config["services"] = {name: config["services"][name] for name in IMAGES}
    for name, service in config["services"].items():
        service["image"] = IMAGES[name]
        service["restart"] = "no"
        service.pop("ports", None)
    web = config["services"]["langfuse-web"]
    web["ports"] = ["127.0.0.1::3000"]
    web["environment"].update(LANGFUSE_INIT_ORG_ID="setrya", LANGFUSE_INIT_PROJECT_ID="setrya-fixture",
                              LANGFUSE_INIT_PROJECT_PUBLIC_KEY="pk-lf-setrya-fixture",
                              LANGFUSE_INIT_PROJECT_SECRET_KEY="sk-lf-setrya-fixture",
                              LANGFUSE_INIT_USER_EMAIL="fixture@example.invalid",
                              LANGFUSE_INIT_USER_PASSWORD="setrya-local-fixture")
    fixture = directory / "compose.json"
    fixture.write_text(json.dumps(config))
    compose.append(str(fixture))
    try:
        subprocess.run([*compose, "up", "-d"], check=True)
        endpoint = subprocess.check_output([*compose, "port", "langfuse-web", "3000"], text=True).strip()
        url = "http://" + endpoint
        for _ in range(120):
            try:
                with urlopen(url + "/api/public/health", timeout=3) as response:
                    if json.load(response)["status"] == "OK":
                        break
            except (URLError, OSError):
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Langfuse did not become healthy")
        subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_agent_harnesses.py"], check=True,
                       env={**os.environ, "SETRYA_LANGFUSE_URL": url})
    finally:
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], check=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        qualify(Path(tmp))
