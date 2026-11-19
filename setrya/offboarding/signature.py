"""Sign exact receipt bytes and check the signer against a caller-supplied policy."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

from .receipt_schema import validate_receipt
from .inputs import InvalidReport, read_regular_bytes, unique_fields

MAX_SIGSTORE_BYTES = 32 * 1024 * 1024


def add_commands(commands):
    for name, help_text in (
        ("sign-receipt", "create a keyless Sigstore bundle for a deletion receipt"),
        ("verify-signature", "verify the exact receipt bytes and signer offline"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--receipt", required=True, type=Path)
        command.add_argument("--bundle", required=True, type=Path)
        if name == "verify-signature":
            command.add_argument("--identity", required=True, help="exact authorized certificate identity")
            command.add_argument("--issuer", required=True, help="exact trusted OIDC issuer")
            command.add_argument("--trusted-root", required=True, type=Path, help="independently provisioned Sigstore root JSON")


def run(args):
    signing = args.offboarding_action == "sign-receipt"
    try:
        raw = read_regular_bytes(args.receipt, "receipt")
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
        validate_receipt(obj)
        if signing:
            if os.path.lexists(args.bundle):
                raise InvalidReport("Choose a new output path for the signature bundle.")
            args.bundle.parent.mkdir(parents=True, exist_ok=True)
        elif not args.identity or not args.issuer:
            raise InvalidReport("Both an exact signer identity and issuer are required.")
        with TemporaryDirectory(dir=args.bundle.parent if signing else None) as folder:
            # Sign and verify the same bytes inspected above, even if the original is replaced.
            snapshot = Path(folder, "receipt.json").resolve()
            snapshot.write_bytes(raw)
            bundle = Path(folder, "sigstore.json").resolve()
            if signing:
                command = ["cosign", "sign-blob", "--yes", "--bundle", str(bundle), str(snapshot)]
            else:
                root = Path(folder, "trusted-root.json").resolve()
                bundle.write_bytes(read_regular_bytes(args.bundle, "signature bundle", MAX_SIGSTORE_BYTES))
                root.write_bytes(read_regular_bytes(args.trusted_root, "trusted root", MAX_SIGSTORE_BYTES))
                command = ["cosign", "verify-blob", "--bundle", str(bundle),
                           "--trusted-root", str(root),
                           "--certificate-identity", args.identity, "--certificate-oidc-issuer", args.issuer,
                           str(snapshot)]
            try:
                subprocess.run(command, check=True)
            except FileNotFoundError:
                raise InvalidReport("Install Cosign 3 to sign or verify receipt signatures.") from None
            if signing:
                os.link(bundle, args.bundle)
        print("Receipt signature bundle saved." if signing else "Receipt signature and signer verified.")
        return 0
    except (ValueError, OSError, RecursionError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, InvalidReport) else "Check the receipt, bundle, trust root and Cosign result."
        print(f"Signature operation failed. {message}", file=sys.stderr)
        return 2
