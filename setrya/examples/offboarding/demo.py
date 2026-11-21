"""Run the same offboarding policy in three CI cases and one request-specific case."""

import hashlib
import json
import shlex
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from setrya.offboarding.receipt_cli import publish_new

from . import fixture


def _command(*args, expected=0):
    from setrya.offboarding.cli import main

    code = main([str(arg) for arg in args])
    if code != expected:
        raise RuntimeError(f"{args[0]} returned exit {code}, expected {expected}.")


def _exhibit(folder, digest, name, kind, text, **fields):
    body = f"Synthetic exhibit for request {digest}.\n{text}\n".encode()
    (folder / f"{name}.txt").write_bytes(body)
    publish_new(
        {"version": 1, "id": name, "kind": kind, "issuer": f"fixture-{name}",
         "request_sha256": digest, "document_sha256": hashlib.sha256(body).hexdigest(), **fields},
        folder / f"{name}.json",
    )


def _request_case(folder, output):
    folder.mkdir()
    policy = folder / "policy.json"
    publish_new(fixture.policy(), policy)
    request = folder / "request.json"
    _command("request", "--policy", policy, "--output", request,
             "--context", "staging", "--revision", fixture.revision(), "--run-id", "receipt-example",
             "--statement", "provider", "--schedule", "backups")
    digest = hashlib.sha256(request.read_bytes()).hexdigest()
    # In a real case, the independent case system keeps this digest before dispatch.
    (folder / "request.sha256").write_text(digest + "\n", encoding="ascii")
    evidence = folder / "evidence.json"
    publish_new(fixture.run_case("fixed", request_dir=folder), evidence)
    _exhibit(folder, digest, "provider", "provider_statement",
             "The fixture provider reports removal of its support copy.", outcome="reported_deleted")
    _exhibit(folder, digest, "backups", "retention_schedule",
             "The fixture schedules expiry on 2030-01-31.", expires_on="2030-01-31")
    inputs = ["--request", request, "--expect-request", digest, "--policy", policy,
              "--evidence", evidence, "--record", folder / "provider.json", folder / "provider.txt",
              "--record", folder / "backups.json", folder / "backups.txt"]
    _command("receipt", *inputs, "--output", output)
    _command("replay", output, "--expect-request", digest, "--case-review", expected=3)
    result = json.loads((output / "erasure-receipt.json").read_bytes())
    statuses = {item["id"]: item["status"] for item in result["items"]}
    if (result["technical_verdict"] != "PASS" or result["review_status"] != "OPEN_ITEMS"
            or statuses != {"provider": "PROVIDER_REPORTED", "backups": "SCHEDULED"}):
        raise RuntimeError("The example receipt did not reproduce the expected findings.")
    return digest


def run(output: Path, *, invocation=("setrya", "offboarding")) -> int:
    """Publish a complete, synthetic walkthrough into a new directory."""
    output = Path(output)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() or output.is_symlink():
            raise FileExistsError
        # A failed demonstration never publishes a partial set of evidence.
        with TemporaryDirectory(dir=output.parent) as directory:
            work = Path(directory) / "evidence"
            work.mkdir()
            policy = work / "policy.json"
            publish_new(fixture.policy(), policy)
            for mode, code in (("broken", 1), ("fixed", 0), ("inconclusive", 3)):
                observations = work / f"{mode}.json"
                publish_new(fixture.run_case(mode), observations)
                print(f"\nCI gate · {mode}")
                _command("gate", "--policy", policy, "--observations", observations,
                         "--expect-run", mode, "--expect-revision", fixture.revision(),
                         "--output", work / f"{mode}-decision.json", expected=code)
            print("\nDeletion receipt · separate staging request")
            digest = _request_case(Path(directory) / "request-fixture", work / "receipt")
            # All full outputs are nonempty, so overlapping runs cannot replace a completed run.
            work.rename(output)
    except FileExistsError:
        print("Choose a new output directory for the offboarding demo.", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"Could not complete the offboarding demo ({type(error).__name__}).", file=sys.stderr)
        return 2
    print(f"\nEvidence saved to {output}")
    # Only known fixture basenames and a digest enter the review command.
    cli = [*invocation, "replay"] if invocation[:1] != ("setrya",) else ["setrya", "replay"]
    review_command = [*cli, ".", "--expect-request", digest, "--case-review"]
    print(f"From {output / 'receipt'}, replay the synthetic receipt with:")
    print(shlex.join(review_command))
    return 0
