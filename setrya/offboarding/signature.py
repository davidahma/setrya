"""Sign exact receipt bytes and check the signer against a caller-supplied policy."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

from .receipt_schema import validate_receipt
from .inputs import MAX_JSON_BYTES, InvalidReport, read_regular_bytes, unique_fields

MAX_SIGSTORE_BYTES = 32 * 1024 * 1024


def add_commands(commands, *, local_keys=False, aliases=False, scope=True):
    for name, help_text in (
        ("sign-receipt", "create a keyless Sigstore bundle for a deletion receipt"),
        ("verify-signature", "verify the exact receipt bytes and signer offline"),
    ):
        command = commands.add_parser(name, help=help_text, aliases=[name.split("-")[0]] if aliases else [])
        if aliases and scope:
            command.add_argument("--scope", choices=("person", "tenant"), default="person")
        command.add_argument("--receipt", required=True, type=Path)
        command.add_argument("--bundle", required=True, type=Path)
        if local_keys:
            command.add_argument("--key", type=Path, help="local signing or trusted verification key for detached signatures")
        if name == "verify-signature":
            command.add_argument("--identity", required=not local_keys, help="exact authorized certificate identity")
            command.add_argument("--issuer", required=not local_keys, help="exact trusted OIDC issuer")
            command.add_argument("--trusted-root", required=not local_keys, type=Path, help="independently provisioned Sigstore root JSON")


def run(args, *, validate=None, receipt_limit=MAX_JSON_BYTES):
    signing = args.offboarding_action == "sign-receipt"
    try:
        raw = read_regular_bytes(args.receipt, "receipt", receipt_limit)
        if validate is None:
            validate_receipt(json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields))
        else:
            validate(raw)
        key = getattr(args, "key", None)
        if key and not signing and any((args.identity, args.issuer, args.trusted_root)):
            raise InvalidReport("Choose one signer policy.")
        if signing:
            if os.path.lexists(args.bundle):
                raise InvalidReport("Choose a new output path for the signature bundle.")
            args.bundle.parent.mkdir(parents=True, exist_ok=True)
        elif not key and not (args.identity and args.issuer and args.trusted_root):
            raise InvalidReport("Both an exact signer identity and issuer are required.")
        with TemporaryDirectory(dir=args.bundle.parent if signing else None) as folder:
            # Sign and verify the same bytes inspected above, even if the original is replaced.
            snapshot = Path(folder, "receipt.json").resolve()
            snapshot.write_bytes(raw)
            bundle = Path(folder, "sigstore.json").resolve()
            if key:
                if not key.resolve().is_file():
                    raise InvalidReport("Expected a local Cosign key file.")
                if signing:
                    command = ["cosign", "sign-blob", "--yes", "--key", str(key.resolve()), "--tlog-upload=false",
                               "--use-signing-config=false", "--output-signature", str(bundle), str(snapshot)]
                else:
                    bundle.write_bytes(read_regular_bytes(args.bundle, "detached signature"))
                    command = ["cosign", "verify-blob", "--key", str(key.resolve()), "--insecure-ignore-tlog",
                               "--signature", str(bundle), str(snapshot)]
            elif signing:
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
