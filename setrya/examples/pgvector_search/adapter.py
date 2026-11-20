"""Copy this factory to connect an HTTP app with the same test hooks."""

from setrya.examples.support_search.adapter import SupportAdapter, final_reads_after_receipt
from setrya.examples.support_search.client import Client
from setrya.adapter import RunContext
from setrya.drill import InputError
from . import MODULE, MODES, SCENARIOS


def http_fixture(context: RunContext) -> SupportAdapter:
    options = context.drill.options
    mode = options.get("mode", "fixed")
    scenario = options.get("scenario", "refund")
    if options.keys() - {"mode", "scenario", "final_reads"} or mode not in MODES or scenario not in SCENARIOS:
        raise InputError("unsupported pgvector fixture mode or scenario")
    after_receipt = final_reads_after_receipt(context)
    client = Client(context.directory, context.run_id, mode, scenario, module=MODULE)
    return SupportAdapter(client, final_reads_after_receipt=after_receipt)
