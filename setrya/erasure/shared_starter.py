"""Verify selective erasure in your disposable application, starting with deletion-only."""
import argparse
from contextlib import contextmanager
from pathlib import Path
import shlex

from setrya.erasure.v3 import Artifact, SharedCase, run_shared
from setrya.erasure.v3_receipt import exit_code, summary

CASE = SharedCase("investigation-1", "/investigations", ("analyst-one", "analyst-two"),
                  "Which laptop was quarantined?", "Which credential was reset?",
                  (Artifact("investigations", "shared-summary", "rewrite"),))


@contextmanager
def open_fixture():
    """Seed two synthetic people in the same record, then yield subject, application, external_items.

    Wire application.inspect to a direct read of the named key, read to the authenticated
    route, and erase to the app's own selective rewrite. For late-write mode, also
    connect hold, checkpoint and release_and_drain around the real worker write.
    Register all required stores in CASE. See docs/erasure-v3.md for the typed observations.
    When the fixture uses a Setrya cache, import make_cache from the generated answer_cache.py.
    """
    raise NotImplementedError("connect open_fixture() to your disposable application")
    yield  # marks this hook as a context manager


def run(output, mode):
    with open_fixture() as (subject, app, external_items):
        return run_shared(subject, application=app, case=CASE, external_items=external_items, output=output, mode=mode)


def test_subject_erasure(tmp_path):
    import pytest

    output = tmp_path / "run"
    try:
        receipt = run(output, "deletion")
    except NotImplementedError as exc:
        pytest.fail(f"NOT TESTED · {exc}", pytrace=False)
    print(f"Replay: setrya erasure replay {shlex.quote(str(output))}")
    if exit_code(receipt):
        pytest.fail(summary(receipt), pytrace=False)
    print(summary(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("deletion", "late-write"), default="deletion")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = run(args.output, args.mode)
    except NotImplementedError as exc:
        parser.exit(3, f"NOT TESTED · {exc}\n")
    print(summary(receipt))
    raise SystemExit(exit_code(receipt))
