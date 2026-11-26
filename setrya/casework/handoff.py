"""Carry the replayed exchange and its authenticated issuer into reviewer exports."""

from dataclasses import dataclass
from datetime import UTC, datetime

from setrya.core.inputs import require
from setrya.core.signing import trusted_public_key

from . import exchange


@dataclass(frozen=True)
class VerifiedHandoff:
    manifest: dict
    receipt: dict
    observations: dict
    signer: dict
    verified_at: str


def verify_handoff(directory, *, key=None, identity=None, issuer=None, trusted_root=None, **pins):
    """Bind the signing key fingerprint to the exact key used during replay."""
    if key is not None:
        require(not any((identity, issuer, trusted_root)), "Choose one exchange signer policy.")
        with trusted_public_key(key) as (snapshot, fingerprint):
            manifest, receipt, data = exchange.verify_source(directory, key=snapshot, **pins)
        signer = {"type": "cosign-public-key", "sha256": fingerprint}
    else:
        manifest, receipt, data = exchange.verify_source(
            directory, identity=identity, issuer=issuer, trusted_root=trusted_root, **pins,
        )
        signer = {"type": "sigstore", "identity": identity, "issuer": issuer}
    return VerifiedHandoff(manifest, receipt, data, signer, datetime.now(UTC).isoformat())
