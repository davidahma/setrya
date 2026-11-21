"""Connect three application functions and run this file against a disposable test fixture."""

import argparse
from functools import partial
from pathlib import Path
from secrets import token_bytes
from shlex import quote
from tempfile import TemporaryDirectory

from setrya.erasure import CopyJournal, ErasureCase, Subject, run_erasure
from setrya.erasure.worker_fixture import WorkerFixture


CASE = ErasureCase("reopen-handoff", "Which charts are in the saved handoff?", ("care-reader", "shift-reviewer"))


def open_fixture(subject, journal):
    """Return a context manager that seeds two test records and yields (app, adapters).

    Record real writes with CopyHooks(journal, subject).invoke_graph(...) and
    .put_memory(...). Register the stores in inventory.toml. Keep the control's
    thread and memory namespace separate using a different Subject.
    """
    raise NotImplementedError("connect open_fixture() to a disposable, seeded application")


def snapshot_job(app, subject, before_write):
    """Run the application's stale snapshot job through its normal write path.

    Call before_write() after reading the old data and immediately before the
    real write. Return only after the background work and refreshes settle.
    """
    raise NotImplementedError("connect snapshot_job() to your worker's pre-write checkpoint")


def read_chat(app, subject, caller, question):
    """Use that caller's credentials to reopen the chat through the real endpoint.

    Return Read(authenticated_caller, observed_route, observed_question,
                subject_marker_in_response, retained_marker_in_response).
    """
    raise NotImplementedError("connect read_chat() to your authenticated endpoint")


def run(output):
    # The fixture is freshly seeded each run. A persistent test app needs the
    # same tenant-specific reference in all workers and across test processes.
    subject = Subject.derive(token_bytes(32), "erasure-test", "fictional-record-17", "restricted")
    inventory = Path(__file__).with_name("inventory.toml")
    with TemporaryDirectory(prefix="setrya-journal-") as scratch:
        journal = CopyJournal(Path(scratch) / "copies.sqlite")
        with open_fixture(subject, journal) as (app, adapters):
            with WorkerFixture(partial(snapshot_job, app), partial(read_chat, app)) as worker:
                return run_erasure(subject, journal=journal, inventory=inventory, adapters=adapters,
                                   case=CASE, application=worker, output=output)


def summary(receipt, output):
    return (f"{receipt['verdict']} · {receipt['unknown_copies']} unknown copies · "
            f"{receipt['time_to_first_verdict_ms']} ms drill duration\n"
            f"Replay: setrya replay {quote(str(output))}")


def test_subject_erasure(tmp_path):
    import pytest

    output = tmp_path / "erasure-run"
    try:
        receipt = run(output)
    except NotImplementedError as exc:
        pytest.fail(f"NOT TESTED · {exc}", pytrace=False)
    message = summary(receipt, output)
    if receipt["verdict"] != "PASS":
        pytest.fail(message, pytrace=False)
    print(message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new directory for the receipt and evidence")
    args = parser.parse_args()
    try:
        receipt = run(args.output)
    except NotImplementedError as exc:
        parser.exit(3, f"NOT TESTED · {exc}\n")
    print(summary(receipt, args.output))
    raise SystemExit({"PASS": 0, "FAIL": 1, "ERROR": 2, "NOT TESTED": 3}[receipt["verdict"]])
