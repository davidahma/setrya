"""Import a synthetic Macie case and test its actual shared investigation and two caches."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from importlib.resources import files
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory

from setrya.drill import InputError
from setrya.erasure.investigation import shared_case
from setrya.erasure.journal import CopyJournal
from setrya.erasure.subject import Subject
from setrya.erasure.v3 import Artifact, ExternalItem

from .demo_application import CACHE_KEY, CACHE_STORE, CatalogInvestigation


# Commit of this synthetic application's application.py; the test recreates the commit.
APPLICATION_REVISION = "faba5b7caa3c011d72543e3c8126c9b59d943885"
ENVIRONMENT = "fixture-workspace"


def init_example(output):
    """Write an offline finding, its independently approved mapping, and a runnable fixture."""
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    for packaged, writable in (("macie-example.json", "finding.json"), ("mapping-example.json", "mapping.json")):
        (destination / writable).write_bytes(files(__package__).joinpath(packaged).read_bytes())
    (destination / "fixture.py").write_text(
        '"""Replace this synthetic application with your disposable test deployment."""\n'
        'from setrya.discovery.demo import open_fixture\n', encoding="utf-8",
    )
    return destination


@contextmanager
def open_fixture(binding, *, guarded=True, connect_analyst_cache=True):
    """Supply facts observed by the application independently of the submitted binding."""
    try:
        import langgraph.checkpoint.sqlite  # noqa: F401
    except ImportError:
        raise InputError("Install the LangGraph demo with python -m pip install 'setrya[erasure]'.") from None
    # A fixed key on fictional identities keeps the packaged finding and fixture reproducible.
    subject, retained = (Subject.derive(bytes(range(32)), ENVIRONMENT, person, "restricted")
                         for person in ("person-one", "person-two"))
    case = shared_case()
    artifacts = case.artifacts
    if connect_analyst_cache:
        artifacts += (Artifact(CACHE_STORE, CACHE_KEY, "rewrite"),)
    case = replace(case, artifacts=artifacts, scan_stores=())
    follow_up = (date.today() + timedelta(days=7)).isoformat()
    provider = ExternalItem("model-provider", "sample-invocation", "provider-owner", "configuration",
                            "fixture-config-17", follow_up=follow_up, subject_ref=subject.ref)
    with TemporaryDirectory(prefix="setrya-catalog-app-") as tmp:
        folder = Path(tmp)
        journal = CopyJournal(folder / "copies.sqlite")
        with CatalogInvestigation(folder, subject, retained, journal, guarded=guarded) as application:
            application.seed()
            yield dict(subject=subject, case=case, application=application, environment=ENVIRONMENT,
                       revision=APPLICATION_REVISION, external_items=(provider,), journal=journal)


def run_demo(output):
    from .case import exit_code, replay, run_case, summary
    from .macie import load_finding
    from .mapping import bind_finding, load_mapping

    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    example = init_example(destination / "example")
    binding = bind_finding(load_finding(example / "finding.json"), load_mapping(example / "mapping.json"))
    print("SEC-417 checks one source, two cached copies and the investigation reopened by two analysts.")
    for name, expected in (("gap", 3), ("broken", 1), ("guarded", 0)):
        directory = destination / name
        with open_fixture(binding, guarded=name != "broken", connect_analyst_cache=name != "gap") as settings:
            run_case(binding, **settings, mode="late-write", output=directory)
        result = replay(directory, expected_binding=binding)
        print(f"\n{name}\n{summary(result)}")
        print(f"Replay: setrya replay {quote(str(directory))}")
        if exit_code(result) != expected:
            raise InputError(f"The {name} discovery fixture produced an unexpected verdict.")
    return 0
