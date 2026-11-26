"""Identify built-in test setups separately from the release that runs them."""

from hashlib import sha256
from inspect import getsource
import json

from . import _agent_memory, _mem0, fixtures
from .report import LOCAL_AGENT_TARGETS, PHASES, SETUP, TARGETS

_FIXTURE_CODE = {
    "pgvector": (fixtures.pgvector,),
    "langgraph-store": (fixtures.langgraph_store, fixtures.embed),
    "mem0": (fixtures.mem0, _mem0),
}
_AGENT_MEMORY_CODE = (fixtures.agent_memory, _agent_memory.record, _agent_memory.main, _agent_memory.reject_network)

# The fixture sources, phase and surface lists, and recorded backend-version
# fields match across these immutable tags. Keep this history static when
# changing a fixture so an old receipt cannot acquire the new setup ID.
_LEGACY_RELEASES = frozenset(("1.13.0", "1.15.0", "1.16.0", "1.17.0"))
_LEGACY_SETUPS = {
    "pgvector": "6a6178bfef795393e650bcc7f7233c3cad46ca8910e523726b4d65dcd5e7d1cf",
    "langgraph-store": "43c14ecf183cf07b9f10884eba39b42b47c6457430919ce6e3b22e8460907985",
    "mem0": "c9926f7b01f93698b970140c597a3dd4934004b8463ee2abd8f16b1944fa7de0",
    "crewai-memory": "ccc4b59cd3cfe9286ec4d2095d071ff9835d29048936d84d40a57b868f5f7d67",
    "cognee-dataset": "73262e5a676903b9f27708ddd14ec65360f6b161ade224a03250b59f923afa05",
    "google-adk-session": "769a96875858e313a509f7ab9a8b108d74d93cc14417fa09072468130f45fa70",
    "openai-agents-session": "6def30d0dddfb71f70a93057e76c7ef8e279f719d51fca0889ad01dc360f6f7c",
}


def fingerprint(target):
    """Fingerprint the named probe, its inputs and its execution path."""
    if target in LOCAL_AGENT_TARGETS:
        code = (_agent_memory.FIXTURES[target], *_AGENT_MEMORY_CODE)
        if target == "cognee-dataset":
            code += (_agent_memory.local_file, _agent_memory.small_text)
    else:
        code = _FIXTURE_CODE[target]
    contract = {
        "format": "setrya.vendor.fixture-setup.v1",
        "target": target,
        "surfaces": TARGETS[target],
        "phases": PHASES,
        "backend_versions": SETUP[target],
        "marker": fixtures.MARKER,
        "records": list(fixtures.TEXT.items()),
        "identities": fixtures.IDENTITIES if target in ("pgvector", "langgraph-store") else None,
        "source": [getsource(item) for item in (fixtures.run_fixture, _mem0.observation, *code)],
    }
    return sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def recorded_fingerprint(target, execution):
    if "fixture_setup" in execution:
        return execution["fixture_setup"]
    if execution["setrya_version"] in _LEGACY_RELEASES:
        return _LEGACY_SETUPS.get(target)
    return None
