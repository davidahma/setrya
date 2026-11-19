"""Sign exact receipt bytes and check the signer against a caller-supplied policy."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

from .inputs import InvalidReport, MAX_JSON_BYTES, regular_file_opener, unique_fields


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
        with open(args.receipt, "rb", opener=regular_file_opener) as src:
            raw = src.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise InvalidReport("Receipt exceeds the 1 MiB limit.")
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
        if (not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] != 1
                or not {"request_sha256", "evidence_sha256", "technical_verdict", "review_status"} <= obj.keys()):
            raise InvalidReport("Expected a version 1 deletion receipt.")
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
                command = ["cosign", "verify-blob", "--bundle", str(args.bundle.resolve()),
                           "--trusted-root", str(args.trusted_root.resolve()),
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
