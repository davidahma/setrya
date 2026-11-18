"""Copy this factory to connect an HTTP app with the same test hooks."""

from examples.support_search.adapter import SupportAdapter
from examples.support_search.client import Client
from setrya.adapter import RunContext
from setrya.drill import InputError
from . import MODULE, MODES, SCENARIOS


def http_fixture(context: RunContext) -> SupportAdapter:
    options = context.drill.options
    mode = options.get("mode", "fixed")
    scenario = options.get("scenario", "refund")
    if options.keys() - {"mode", "scenario"} or mode not in MODES or scenario not in SCENARIOS:
        raise InputError("unsupported pgvector fixture mode or scenario")
    return SupportAdapter(Client(context.directory, context.run_id, mode, scenario, module=MODULE))
