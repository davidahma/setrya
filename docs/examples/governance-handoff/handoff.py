"""Attach verified drill or vendor evidence to a governance ticket."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from setrya.casework import exchange
from setrya.casework.handoff import verify_handoff
from setrya.core.inputs import InvalidReport, require
from setrya.erasure import run_shared
from setrya.examples.workspace import CASE, open_fixture
from setrya.receipt_format import RECEIPT


CONTROL = {"framework": "Support governance", "reference": "ERASURE-07"}


def issue(output, key):
    with TemporaryDirectory(prefix="setrya-governance-") as scratch:
        original = Path(scratch) / "original"
        case = replace(CASE, controls=(CONTROL,))
        with open_fixture(shared=True, guarded=True, record_writes=True) as (subject, app, items):
            receipt = run_shared(subject, case=case, application=app, external_items=items,
                                 mode="write-back", output=original, journal=app.journal,
                                 cancel_hold=app.cancel_and_drain)
        require((receipt["technical_verdict"], receipt["review_status"]) == ("PASS", "OPEN_ITEMS"),
                "Expected the synthetic drill to pass with a provider follow-up.")
        exchange.pack(original, output=output, system="synthetic-support", version="1", synthetic=True, key=key)


def attachment(bundle, key, *, ticket=None, framework=None, reference=None):
    verified = verify_handoff(bundle, key=key)
    manifest = verified.manifest
    digest = manifest["files"][RECEIPT]
    kind = manifest["evidence_kind"]
    if kind == "vendor-attestation":
        require(all(isinstance(value, str) and value.strip() for value in (ticket, framework, reference)),
                "Supply the ticket and its control mapping for a vendor attestation.")
        control = {"framework": framework, "reference": reference}
        source = "reviewer"
    else:
        require(not any(value is not None for value in (ticket, framework, reference)),
                "The sample drill uses the control already recorded in its receipt.")
        require(CONTROL in verified.receipt.get("controls", []),
                "The verified receipt lacks the expected governance control.")
        ticket, control, source = "GOV-204", CONTROL, "receipt"
    return {
        "governance_ticket": ticket,
        "control_reference": control,
        "control_reference_source": source,
        "evidence_kind": kind,
        "exchange_attachment": bundle.name,
        "receipt_sha256": digest,
        "signer": verified.signer,
        "verified_at": verified.verified_at,
        "technical_verdict": manifest["technical_verdict"],
        "review_status": manifest["review_status"],
        "open_items": manifest["open_items"],
        **({"vendor": manifest["vendor"]} if kind == "vendor-attestation" else {}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    issuer = commands.add_parser("issue", help="run the fictional drill and sign its exchange")
    issuer.add_argument("output", type=Path, help="fresh exchange directory")
    issuer.add_argument("--key", required=True, type=Path, help="issuer's Cosign private key")
    reviewer = commands.add_parser("review", help="verify the exchange and print a ticket attachment record")
    reviewer.add_argument("bundle", type=Path, help="the received exchange directory")
    reviewer.add_argument("--key", required=True, type=Path, help="independently trusted Cosign public key")
    reviewer.add_argument("--ticket", help="ticket ID for the vendor attestation")
    reviewer.add_argument("--framework", help="ticket's control framework for the vendor attestation")
    reviewer.add_argument("--control", help="ticket's control reference for the vendor attestation")
    args = parser.parse_args(argv)
    try:
        if args.command == "issue":
            issue(args.output, args.key)
        else:
            print(json.dumps(attachment(args.bundle, args.key, ticket=args.ticket,
                                        framework=args.framework, reference=args.control), indent=2))
    except (InvalidReport, OSError) as error:
        parser.exit(2, f"Governance handoff failed. {error}\n")


if __name__ == "__main__":
    main()
