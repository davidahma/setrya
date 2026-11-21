"""Delete one synthetic customer's note and check the answer endpoint."""
import argparse
from pathlib import Path

from setrya.erasure.v3 import run_shared
from setrya.erasure.v3_receipt import exit_code, summary
from setrya.examples.workspace import SIMPLE as CASE
from setrya.examples.workspace import open_fixture

# Replace CASE and open_fixture with imports from your disposable application.
# CASE names the caller, questions, store keys and actions. open_fixture seeds
# the two facts and supplies (subject, application, external_items).


def run(output, mode):
    with open_fixture() as (subject, application, items):
        return run_shared(subject, case=CASE, application=application, external_items=items,
                          mode=mode, output=output, cancel_hold=getattr(application, "cancel_and_drain", None))


def test_subject_erasure(tmp_path):
    receipt = run(tmp_path / "receipt", "deletion")
    assert exit_code(receipt) == 0, summary(receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("deletion", "late-write"), default="deletion")
    args = parser.parse_args()
    receipt = run(args.output, args.mode)
    print(summary(receipt))
    raise SystemExit(exit_code(receipt))
