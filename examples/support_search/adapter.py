"""Map an application's regular search and worker observations into Setrya."""

from setrya.adapter import Checkpoint, Publication, ReadView, Receipt, RunContext, SearchResult
from setrya.drill import InputError, Probe

from .app import App, MODES
from .client import Client


class SupportAdapter:
    def __init__(self, fixture):
        self.fixture = fixture

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


def fixture_options(context: RunContext):
    options = context.drill.options
    mode = options.get("mode", "fixed")
    scenario = options.get("scenario", "refund")
    if options.keys() - {"mode", "scenario"} or mode not in sorted(MODES) or scenario not in ("refund", "retired"):
        raise InputError("unsupported support fixture mode or scenario")
    return context.directory, context.run_id, mode, scenario


def http_fixture(context: RunContext):
    return SupportAdapter(Client(*fixture_options(context)))


def python_fixture(context: RunContext):
    return SupportAdapter(App(*fixture_options(context)))
