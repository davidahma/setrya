"""Run and sign a disposable vendor conformance fixture."""

import argparse
from pathlib import Path
from shlex import join
from uuid import uuid4

from setrya import __version__
from setrya.casework import profiles
from setrya.core import signing
from setrya.drill import InputError
from setrya.receipt_format import RECEIPT
from setrya.receipts import exit_code, replay

from . import hosted
from ._setup import fingerprint
from .fixtures import run_fixture
from .report import TARGETS, operator_label, summary
from .review import DETACHED_SIGNATURE, KEYLESS_SIGNATURE, assess_review, rerun_digest


def command(argv):
    parser = argparse.ArgumentParser(prog="setrya conformance", description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="operation", required=True)
    run = commands.add_parser("run", help="check a disposable backend and sign its evidence", allow_abbrev=False)
    run.add_argument("--target", choices=TARGETS, required=True)
    run.add_argument("--output", type=Path, default=Path("setrya-conformance"))
    run.add_argument("--operator", help="record the test operator as a lowercase identifier")
    run.add_argument("--rerun-of", type=Path, help="run the fixture again and link to an original self-check")
    review = commands.add_parser("review", help="authenticate and replay one result or a linked rerun", allow_abbrev=False)
    review.add_argument("directory", type=Path)
    review.add_argument("--key", type=Path, help="independently trusted Cosign public key")
    review.add_argument("--identity", help="exact trusted Sigstore certificate identity")
    review.add_argument("--issuer", help="exact trusted Sigstore OIDC issuer")
    review.add_argument("--trusted-root", type=Path, help="independently provisioned Sigstore root JSON")
    review.add_argument("--expect-operator", help="expected operator from your review policy")
    review.add_argument("--against", type=Path, help="original self-check for a linked rerun")
    review.add_argument("--against-key", type=Path, help="separate trusted Cosign key for the original self-check")
    review.add_argument("--against-identity", help="exact trusted Sigstore certificate identity for the original")
    review.add_argument("--against-issuer", help="exact trusted Sigstore OIDC issuer for the original")
    review.add_argument("--against-trusted-root", type=Path, help="independently provisioned Sigstore root for the original")
    review.add_argument("--accept-rerun", action="store_true",
                        help="use the rerun's technical exit code even if the linked results differ")
    baseline = commands.add_parser("baseline", help="record a read-only baseline in an authorized test project")
    baseline.add_argument("--target", choices=tuple(hosted.SURFACES), required=True)
    baseline.add_argument("--synthetic", action="store_true", required=True, help="use only synthetic target and control records")
    baseline.add_argument("--output", type=Path, required=True)
    readback = commands.add_parser("readback", help="read the same API after an operator-reported deletion and sign the result")
    readback.add_argument("baseline", type=Path)
    readback.add_argument("--after-delete", action="store_true", required=True, help="confirm deletion ran outside Setrya")
    readback.add_argument("--output", type=Path, required=True)
    for check in (baseline, readback):
        check.add_argument("--scope", help="parent identifier, or region[:session] for Bedrock")
        check.add_argument("--record", required=True, help="synthetic target identifier")
        check.add_argument("--retain", required=True, help="separate retained control identifier")
    for signed in (run, readback):
        signer = signed.add_mutually_exclusive_group(required=True)
        signer.add_argument("--key", type=Path, help="local Cosign private key (Cosign 3)")
        signer.add_argument("--keyless", action="store_true", help="sign online with Cosign and an OIDC identity")

    args = parser.parse_args(argv)
    if args.operation == "review":
        if args.accept_rerun and args.against is None:
            raise InputError("--accept-rerun requires a paired review with --against.")
        result = assess_review(args.directory, key=args.key, identity=args.identity, issuer=args.issuer,
                               trusted_root=args.trusted_root, expect_operator=args.expect_operator,
                               against=args.against, against_key=args.against_key, against_identity=args.against_identity,
                               against_issuer=args.against_issuer, against_trusted_root=args.against_trusted_root)
        if result.headline is not None:
            print(result.headline)
        print(summary(result.receipt, execution_review=result.details))
        if result.agrees is False:
            if not args.accept_rerun:
                print("Review exit 3: the linked results disagree.")
                return 3
            print("Review uses the rerun's technical exit code (--accept-rerun).")
        return exit_code(result.receipt)
    if args.operation == "baseline":
        observation = hosted.capture(args.target, args.scope, args.record, args.retain, args.output)
        if observation == {"target": True, "retained": True}:
            print("Both synthetic baseline records were visible.")
            return 0
        print("The API did not establish both positive controls.")
        return 3
    if args.operation == "readback":
        result = _sign_evidence(lambda: hosted.follow(args.baseline, args.scope, args.record, args.retain),
                                args.output, key=args.key)
    else:
        result = run_and_sign(args.target, args.output, key=args.key, operator=args.operator, rerun_of=args.rerun_of)
    if result is None:
        return 2
    print(summary(result))
    print(join(["setrya", "replay", str(args.output)]))
    return exit_code(result)


def run_and_sign(target, output, *, key, operator=None, rerun_of=None):
    if operator is not None:
        operator_label(operator)
    fixture_setup = fingerprint(target)
    execution = {"operator": operator, "run_id": str(uuid4()), "setrya_version": __version__,
                 "fixture_setup": fixture_setup}
    if rerun_of is not None:
        execution["rerun_of"] = rerun_digest(rerun_of, target, operator, setrya_version=execution["setrya_version"],
                                              fixture_setup=fixture_setup)
    return _sign_evidence(lambda: {**run_fixture(target), "execution": execution}, output, key=key)


def _sign_evidence(observe, output, *, key):
    if key is not None and not key.is_file():
        raise InputError("Choose an existing local Cosign signing key.")
    with profiles.new_bundle(output) as folder:
        result = profiles.issue("vendor-conformance", observe(), folder)
        manifest = folder / RECEIPT
        args = argparse.Namespace(receipt=manifest, bundle=folder / (DETACHED_SIGNATURE if key else KEYLESS_SIGNATURE),
                                  key=key, offboarding_action="sign-receipt")
        code = signing.run(args, validate=lambda raw: profiles.replay_bytes(folder, raw))
        if code == 0:
            if replay(folder) != result:
                raise InputError("Replay differs from the observed fixture.")
        else:
            # Keep the observations available for diagnosis. An unsigned bundle
            # and its replay can never produce a successful conformance command.
            print("Unsigned evidence was saved for diagnosis.")
            return None
    return result
