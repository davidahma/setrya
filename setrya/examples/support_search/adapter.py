"""Map an application's regular search and worker observations into Setrya."""

from setrya.adapter import Checkpoint, Publication, ReadView, Receipt, RunContext, SearchResult
from setrya.drill import InputError, Probe

from .app import App, MODES
from .client import Client


class SupportAdapter:
    def __init__(self, fixture, *, final_reads_after_receipt=False):
        self.fixture = fixture
        self.final_reads_after_receipt = final_reads_after_receipt

    def seed(self):
        self.fixture.seed()

    def start_old(self):
        return self.fixture.start_old()

    def checkpoint(self):
        observed = self.fixture.checkpoint()
        return Checkpoint(**observed) if observed is not None else None

    def publish(self):
        return Publication(**self.fixture.publish())

    def release(self):
        self.fixture.release()

    def receipt(self):
        observed = self.fixture.receipt()
        return Receipt(**observed) if observed is not None else None

    def search(self, probe: Probe):
        result = self.fixture.search(probe.question, probe.rules.route, probe.rules.scope, probe.rules.top_k)
        return SearchResult(
            passages=tuple(hit["id"] for hit in result["hits"]),
            view=ReadView(**result["view"]) if result["view"] is not None else None,
            origins=tuple(hit["origin"] for hit in result["hits"]),
            route=result["route"], scope=result["scope"],
        )

    def close(self):
        self.fixture.close()


def final_reads_after_receipt(context: RunContext) -> bool:
    timing = context.drill.options.get("final_reads", "during-settle")
    if timing not in ("during-settle", "after-receipt"):
        raise InputError("unsupported final read schedule for the example fixture")
    return timing == "after-receipt"


def fixture_options(context: RunContext):
    options = context.drill.options
    mode = options.get("mode", "fixed")
    scenario = options.get("scenario", "refund")
    if options.keys() - {"mode", "scenario", "final_reads"} or mode not in sorted(MODES) or scenario not in ("refund", "retired"):
        raise InputError("unsupported support fixture mode or scenario")
    return context.directory, context.run_id, mode, scenario


def http_fixture(context: RunContext):
    after_receipt = final_reads_after_receipt(context)
    return SupportAdapter(Client(*fixture_options(context)), final_reads_after_receipt=after_receipt)


def python_fixture(context: RunContext):
    after_receipt = final_reads_after_receipt(context)
    return SupportAdapter(App(*fixture_options(context)), final_reads_after_receipt=after_receipt)
