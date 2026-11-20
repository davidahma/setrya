"""Replay the synthetic celery-langchain-endpoint.zip recordings from release v0.14.0."""

from hashlib import sha256
from pathlib import Path

from setrya.endpoint_evidence import _read_bundle, contract_for
from setrya.status import Status


RECORDINGS = Path(__file__).with_name("access-v0140")
CASES = (
    ("broken", "Broken cache", Status.FAIL),
    ("guarded", "Guarded cache", Status.PASS),
    ("unobserved", "Missing serving evidence", Status.NOT_TESTED),
)
# The release records fingerprints and answer classifications. Show the fixture's
# human-readable text only when it matches the recorded fingerprint.
ANSWERS = {
    sha256(answer.encode()).hexdigest(): answer
    for answer in ("The staff refund window is 45 days.", "The public refund window is 14 days.")
}


def demo_access() -> int:
    print("Offline replay of synthetic access-revocation recordings from Setrya v0.14.0")
    print("A worker had prepared the staff refund answer when one user lost access.")
    ok = True
    for directory, title, expected in CASES:
        verdict, _, report, manifest = _read_bundle(RECORDINGS / directory)
        contract = contract_for(manifest)
        roles = {contract.revoked: "Revoked user", contract.retained: "Retained colleague"}
        answers = {
            "private-answer": ANSWERS.get(contract.private_answer_sha256, "Private answer"),
            "public-only": ANSWERS.get(contract.public_answer_sha256, "Public answer"),
        }
        print(f"\n{title} · {verdict.status.label}")
        receipt = next((e for e in report.events if e["kind"] == "receipt"), None)
        paused = "After revocation, worker paused" if manifest["held_after_reads"] else "After revocation"
        resumed = f"After worker resumed, late write {receipt['outcome']}" if receipt else "Final reads"
        for stage, heading in (("freshness", paused), ("final", resumed)):
            rows = [row for row in manifest["observations"] if row["stage"] == stage]
            if rows:
                print(f"  {heading}")
            for row in rows:
                caller = row["caller"]
                role = roles.get(caller, "Unidentified user")
                body = answers.get(row["body"], row["body"])
                cache = {True: "hit", False: "miss"}.get(row["cache_hit"], "unknown")
                print(f"    {role} ({caller}) · {body} · cache {cache}")
        print(f"  {verdict.detail}")
        ok &= verdict.status is expected
    print("\nGenerate a pytest starter for your own endpoint in your application checkout.")
    print("  setrya init endpoint")
    return 0 if ok else 1
