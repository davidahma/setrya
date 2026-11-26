"""Generate signed receipt cases and check the Python verifier's stable results."""

import argparse
import json
import os
import subprocess
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from sqlite3 import connect
from tempfile import TemporaryDirectory

# This qualification script also runs against a checkout without an installed package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setrya.casework import exchange, observe_deletion
from setrya.casework.observed import LEGACY_FORMAT
from setrya.casework.profiles import OBSERVED, VENDOR, issue
from setrya.core import receipt_v3
from setrya.erasure.journal import CopyJournal
from setrya.erasure.subject import Subject
from setrya.erasure.v3 import run_shared
from setrya.examples.workspace import CASE, demo, open_fixture
from setrya.provenance import producer_only


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def document(path):
    return json.loads(path.read_bytes())


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def _observe_examples(scratch):
    subject = Subject.derive(b"fixture" * 8, "synthetic", "target", "restricted")
    control = Subject.derive(b"fixture" * 8, "synthetic", "control", "restricted")
    journal, store = scratch / "journal.sqlite", scratch / "store.sqlite"
    frame, mapping = scratch / "frame.json", scratch / "observer.toml"
    CopyJournal(journal).record(subject, store="notes", key="target", source="fixture", action="stored")
    write(frame, {"format": "setrya.completed-requests.v1", "requests": [{
        "id": "EXCHANGE-42", "subject_ref": subject.ref, "stores": ["notes"], "completed_at": "2020-01-01T00:00:00Z",
    }]})
    mapping.write_text(f'''[stores.notes]
kind = "sqlite"
path = "store.sqlite"
table = "notes"
key_column = "id"
control_key = "control"
subject_column = "ref"
control_subject_ref = "{control.ref}"
owner = "fixture-owner"
follow_up = "2020-01-02"
''', encoding="utf-8")
    with closing(connect(store)) as database, database:
        database.execute("CREATE TABLE notes(id TEXT PRIMARY KEY, ref TEXT)")
        database.execute("INSERT INTO notes VALUES('control', ?)", (control.ref,))
    for name, verdict, update, parameters in (
        ("passed", "PASS", None, ()),
        ("residual", "FAIL", "INSERT INTO notes VALUES('target', ?)", (subject.ref,)),
        ("unavailable", "NOT TESTED", "DELETE FROM notes", ()),
    ):
        if update is not None:
            with closing(connect(store)) as database, database:
                database.execute(update, parameters)
        receipt = observe_deletion(frame=frame, request="EXCHANGE-42", journal=journal,
                                   inventory_file=mapping, output=scratch / name)
        if receipt["technical_verdict"] != verdict:
            raise RuntimeError(f"Expected {name} to produce {verdict}.")
    return digest(frame)


def _key(directory):
    directory.mkdir()
    subprocess.run(["cosign", "generate-key-pair"], cwd=directory, check=True, capture_output=True, timeout=30)
    return directory / "cosign.key", directory / "cosign.pub"


def _deployment_example(scratch):
    """Issue a grouped v3.3 codec vector with synthetic transport observations."""
    signals = {"origin": "https://staging.example.invalid", "build_id": "example-42", "tls_sha256": "d" * 64}
    record = {"format": "setrya.deployment-record.v1", "environment": "staging", **signals}
    behavior, deployment = scratch / "behavior.json", scratch / "deployment.json"
    write(behavior, CASE.spec())
    write(deployment, record)
    source = producer_only()
    source["application"]["execution"] = "http-case"
    source["test_files"] = [
        {"path": path.name, "location": "external", "role": role, "sha256": digest(path), "commit_bound": False}
        for path, role in ((behavior, "entrypoint"), (deployment, "test"))
    ]
    attempts = 3 * len(CASE.callers) * len(CASE.questions()) * CASE.samples
    observed = {"record": record, "behavior_sha256": digest(behavior), "deployment_sha256": digest(deployment),
                "reads": [signals] * attempts, "erase": signals}
    with open_fixture(shared=True) as (subject, app, items):
        result = run_shared(subject, case=CASE, application=app, external_items=items, mode="deletion",
                            output=scratch / "deployed", provenance=source, deployment_evidence=observed)
    if result["technical_verdict"] != "PASS" or result["deployment"]["status"] != "verified":
        raise RuntimeError("The grouped deployment fixture did not pass.")
    return record


def _committed_example(scratch):
    repository = scratch / "app"
    repository.mkdir()
    starter = repository / "test_erasure.py"
    starter.write_bytes((ROOT / "setrya/erasure/workspace_starter.py").read_bytes())

    def run(*command):
        return subprocess.run(command, cwd=repository, env={**os.environ, "PYTHONPATH": str(ROOT)},
                              check=True, capture_output=True, text=True, timeout=30).stdout

    run("git", "init", "-q")
    run("git", "add", starter.name)
    run("git", "-c", "user.name=Setrya fixtures", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Commit the fixture")
    commit = run("git", "rev-parse", "HEAD").strip()
    run(sys.executable, "-m", "setrya", "run", "--starter", starter.name,
        "--output", str(scratch / "committed"))
    return commit


def _historical_examples(scratch):
    """Repack historical evidence with the rules that originally issued it."""
    original = document(scratch / "workspace/guarded" / exchange.DRILL_EVIDENCE)
    for name, format_name in (("grouped-v31", receipt_v3.GROUPED_V31_FORMAT),
                              ("flat-sampled", receipt_v3.FLAT_EVIDENCE_FORMAT)):
        folder = scratch / name
        folder.mkdir()
        historical = {k: v for k, v in original.items() if k != "provenance"}
        historical["format"] = format_name
        if format_name == receipt_v3.FLAT_EVIDENCE_FORMAT:
            del historical["journal_chain"]
            for phase in ("baseline", "immediate", "final"):
                stage = historical[phase]
                historical[phase] = {k: v for k, v in stage.items() if k != "scan_details"}
                historical[phase]["answers"] = receipt_v3.answer_records(original, phase)
        evidence = folder / exchange.DRILL_EVIDENCE
        write(evidence, historical)
        write(folder / exchange.RECEIPT, receipt_v3.derive(historical, digest(evidence)))
        yield name, True

    folder = scratch / "observed-v1"
    folder.mkdir()
    historical = document(scratch / "passed" / exchange.OBSERVED_EVIDENCE)
    del historical["provenance"]
    historical["format"] = LEGACY_FORMAT
    issue(OBSERVED, historical, folder)
    yield folder.name, False


def _vendor_example(scratch):
    folder = scratch / "vendor-residual"
    folder.mkdir()
    issue(VENDOR, {
        "target": "mem0", "observed_at": "2026-11-25T10:00:00+00:00",
        "versions": {"mem0ai": "1", "qdrant-client": "1"}, "error": None,
        "observations": {
            phase: {surface: {"target": phase == "baseline" or surface == "history", "retained": True}
                    for surface in ("store", "index", "history")}
            for phase in ("baseline", "after_delete", "after_retry")
        },
        "execution": {"operator": "vector-fixture", "run_id": "0b2f88b1-47ad-4723-a8fe-0eab52bc995e",
                      "setrya_version": "1.12.0"},
    }, folder)
    return folder


def build(output, scratch):
    output.mkdir()
    key, public_key = _key(scratch / "signer")
    _, wrong_public_key = _key(scratch / "other")
    (output / "issuer.pub").write_bytes(public_key.read_bytes())
    (output / "wrong.pub").write_bytes(wrong_public_key.read_bytes())
    frame_digest = _observe_examples(scratch)
    if demo(scratch / "workspace", "late-write") != 0:
        raise RuntimeError("Workspace drill did not reproduce its expected verdicts.")
    cases = []
    for name, source, synthetic in (("passed", "passed", False), ("residual", "residual", False),
                                    ("unavailable", "unavailable", False), ("open-items", "workspace/guarded", True)):
        report = exchange.pack(scratch / source, output=output / name, key=key,
                               system="synthetic-support", version="1", synthetic=synthetic)
        cases.append({"name": name, "code": None, "verdict": report["technical_verdict"],
                      **({} if synthetic else {"ticket": "EXCHANGE-42", "frame": frame_digest})})
    for name, synthetic in _historical_examples(scratch):
        exchange.pack(scratch / name, output=output / name, key=key,
                      system="synthetic-support", version="1", synthetic=synthetic)
        cases.append({"name": name, "code": None, "verdict": "PASS",
                      **({} if synthetic else {"ticket": "EXCHANGE-42", "frame": frame_digest})})
    vendor = _vendor_example(scratch)
    exchange.pack(vendor, output=output / vendor.name, key=key, system="mem0", version="1")
    cases.append({"name": vendor.name, "code": None, "verdict": "FAIL",
                  "detail": ["Conformance fixture · mem0 · recorded operator vector-fixture",
                             "Authentication applies to the exchange issuer.",
                             "Recorded versions mem0ai 1, qdrant-client 1", "FAIL · history"]})
    provider = scratch / "provider-readback"
    provider.mkdir()
    issue(VENDOR, {
        "target": "mem0-hosted-history", "observed_at": "2026-11-25T10:00:00+00:00",
        "versions": {"api": "v1"}, "error": None,
        "observations": {
            "baseline": {"memory-history-content": {"target": True, "retained": True}},
            "after_delete": {"memory-history-content": {"target": False, "retained": True}},
        },
        "readback": {"baseline_at": "2026-11-25T09:00:00+00:00", "scope_sha256": "a" * 64},
    }, provider)
    exchange.pack(provider, output=output / provider.name, key=key, system="synthetic-provider", version="v1")
    cases.append({"name": provider.name, "code": None, "verdict": "PASS",
                  "detail": ["Provider API readback", "Observed API scope SHA-256 " + "a" * 64,
                             "PASS · memory-history-content", "NOT TESTED · provider-internals"]})
    record = _deployment_example(scratch)
    exchange.pack(scratch / "deployed", output=output / "deployed", key=key,
                  system="synthetic-support", version="1", synthetic=True)
    cases.append({"name": "deployed", "code": None, "verdict": "PASS", "deployment": record})
    copytree(output / "deployed", output / "other-deployment")
    cases.append({"name": "other-deployment", "code": "DEPLOYMENT_MISMATCH",
                  "deployment": {**record, "environment": "production"}})
    commit = _committed_example(scratch)
    exchange.pack(scratch / "committed", output=output / "committed", key=key,
                  system="synthetic-support", version="1", synthetic=True)
    cases.append({"name": "committed", "code": None, "verdict": "PASS", "appCommit": commit})

    def variant(name, code, *, source="passed", **expected):
        destination = output / name
        copytree(output / source, destination)
        cases.append({"name": name, "code": code, **expected})
        return destination

    def resign(bundle, manifest, *, raw=None):
        manifest_file = bundle / exchange.MANIFEST
        if raw is None:
            write(manifest_file, manifest)
        else:
            manifest_file.write_bytes(raw)
        signature_file = bundle / exchange.SIGNATURE
        signature_file.unlink()
        exchange.signing.sign_bytes(manifest_file.read_bytes(), signature_file, key=key)

    def rebind(bundle, *, original=None, evidence_name=exchange.OBSERVED_EVIDENCE):
        receipt_file = bundle / exchange.RECEIPT
        receipt = document(receipt_file) if original is None else original
        receipt["evidence"]["sha256"] = digest(bundle / evidence_name)
        write(receipt_file, receipt)
        manifest = document(bundle / exchange.MANIFEST)
        manifest["files"] = {evidence_name: digest(bundle / evidence_name), exchange.RECEIPT: digest(receipt_file)}
        return manifest

    archived = variant("legacy-provider-readback", None, source=provider.name, verdict="PASS")
    manifest = document(archived / exchange.MANIFEST)
    manifest["evidence_kind"] = "vendor-attestation"
    del manifest["observation"]["first_checked_at"]
    del manifest["vendor"]["scope_sha256"]
    resign(archived, manifest)

    missing = variant("missing-file", "UNEXPECTED_FILES")
    (missing / exchange.OBSERVED_EVIDENCE).unlink()
    extra = variant("extra-file", "UNEXPECTED_FILES")
    (extra / "extra.txt").write_text("extra", encoding="utf-8")
    swapped = variant("swapped-evidence-filename", "EVIDENCE_KIND_MISMATCH")
    (swapped / exchange.OBSERVED_EVIDENCE).rename(swapped / exchange.DRILL_EVIDENCE)
    swapped = variant("swapped-evidence-bytes", "EVIDENCE_KIND_MISMATCH")
    (swapped / exchange.OBSERVED_EVIDENCE).write_bytes((output / "open-items" / exchange.DRILL_EVIDENCE).read_bytes())
    resign(swapped, rebind(swapped))
    swapped = variant("vendor-declared-with-observed-profile", "EVIDENCE_KIND_MISMATCH", source=vendor.name)
    (swapped / exchange.OBSERVED_EVIDENCE).write_bytes((output / "passed" / exchange.OBSERVED_EVIDENCE).read_bytes())
    resign(swapped, rebind(swapped, original=document(output / "passed" / exchange.RECEIPT)))
    altered = variant("vendor-outcome-claim", "CLAIMS_MISMATCH", source=vendor.name)
    manifest = document(altered / exchange.MANIFEST)
    manifest["vendor"]["checks"][2]["status"] = "PASS"
    resign(altered, manifest)
    variant("vendor-ticket-pin", "TICKET_ID_MISMATCH", source=vendor.name, ticket="EXCHANGE-42")
    changed = variant("evidence-digest", "EVIDENCE_DIGEST_MISMATCH")
    with (changed / exchange.OBSERVED_EVIDENCE).open("ab") as stream:
        stream.write(b"\n")
    changed = variant("original-evidence-digest", "EVIDENCE_DIGEST_MISMATCH")
    evidence_file = changed / exchange.OBSERVED_EVIDENCE
    evidence_file.write_bytes(evidence_file.read_bytes() + b"\n")
    manifest = document(changed / exchange.MANIFEST)
    manifest["files"][exchange.OBSERVED_EVIDENCE] = digest(evidence_file)
    resign(changed, manifest)
    variant("ticket-pin", "TICKET_ID_MISMATCH", ticket="EXCHANGE-43")
    variant("export-pin", "EXPORT_DIGEST_MISMATCH", frame="0" * 64)
    variant("checkout-pin", "APP_COMMIT_MISMATCH", appCommit="a" * 40)
    variant("deployment-pin", "DEPLOYMENT_MISMATCH", deployment={
        "format": "setrya.deployment-record.v1", "environment": "production", "origin": "https://example.invalid",
        "build_id": "release-42", "tls_sha256": "a" * 64,
    })
    variant("wrong-signer", "WRONG_SIGNER", wrongKey=True)
    for field, value in (("sha256", "malformed"), ("location", "external"), ("role", "test")):
        invalid = variant(f"committed-invalid-{field}", "CLAIMS_MISMATCH", source="committed", appCommit=commit)
        evidence_file = invalid / exchange.DRILL_EVIDENCE
        evidence = document(evidence_file)
        evidence["provenance"]["test_files"][0][field] = value
        write(evidence_file, evidence)
        original = document(invalid / exchange.RECEIPT)
        original["provenance"] = evidence["provenance"]
        resign(invalid, rebind(invalid, original=original, evidence_name=exchange.DRILL_EVIDENCE))
    missing_deployment = variant("missing-deployment", "CLAIMS_MISMATCH", source="deployed")
    evidence_file = missing_deployment / exchange.DRILL_EVIDENCE
    evidence = document(evidence_file)
    del evidence["deployment"]
    write(evidence_file, evidence)
    original = document(missing_deployment / exchange.RECEIPT)
    del original["deployment"]
    manifest = rebind(missing_deployment, original=original, evidence_name=exchange.DRILL_EVIDENCE)
    del manifest["deployment"]
    resign(missing_deployment, manifest)
    unverified_pass = variant("unverified-deployment-pass", "VERDICT_MISMATCH", source="deployed")
    evidence_file = unverified_pass / exchange.DRILL_EVIDENCE
    evidence = document(evidence_file)
    evidence["deployment"]["reads"][-1]["build_id"] = "another-build"
    evidence["final"]["answers"][-1][-1] = None
    write(evidence_file, evidence)
    original = document(unverified_pass / exchange.RECEIPT)
    original["deployment"]["status"] = "unverified"
    manifest = rebind(unverified_pass, original=original, evidence_name=exchange.DRILL_EVIDENCE)
    manifest["deployment"] = original["deployment"]
    resign(unverified_pass, manifest)
    duplicate = variant("duplicate-manifest-key", "DUPLICATE_JSON_KEY")
    raw = (duplicate / exchange.MANIFEST).read_bytes().replace(b"{", b'{"scope":"person",', 1)
    resign(duplicate, None, raw=raw)
    duplicate = variant("duplicate-evidence-key", "DUPLICATE_JSON_KEY")
    evidence_file = duplicate / exchange.OBSERVED_EVIDENCE
    evidence_file.write_bytes(evidence_file.read_bytes().replace(b"{", b'{"format":"duplicate",', 1))
    resign(duplicate, rebind(duplicate))
    overlap = variant("overlapping-snapshots", "SNAPSHOT_ORDER")
    evidence_file = overlap / exchange.OBSERVED_EVIDENCE
    data = document(evidence_file)
    data["find"].update(journal_read_at="2020-01-01T00:00:01Z", checked_at="2020-01-01T00:00:02.123456Z")
    data["verify_absent"]["journal_read_at"] = "2020-01-01T00:00:02.123455Z"
    write(evidence_file, data)
    resign(overlap, rebind(overlap))
    wrong = variant("issuer-verdict", "VERDICT_MISMATCH")
    manifest = document(wrong / exchange.MANIFEST)
    manifest.update(technical_verdict="FAIL", review_status="RESIDUAL_FOUND")
    resign(wrong, manifest)
    wrong = variant("issuer-and-receipt-verdict", None, pythonCode="VERDICT_MISMATCH", verdict="FAIL")
    original = document(wrong / exchange.RECEIPT)
    original.update(technical_verdict="FAIL", review_status="RESIDUAL_FOUND")
    manifest = rebind(wrong, original=original)
    manifest.update(technical_verdict="FAIL", review_status="RESIDUAL_FOUND")
    resign(wrong, manifest)
    write(output / "cases.json", cases)
    for case in cases:
        expected = case.get("pythonCode", case["code"])
        try:
            result = exchange.verify(output / case["name"], key=wrong_public_key if case.get("wrongKey") else public_key,
                                     expect_ticket=case.get("ticket"), expect_frame=case.get("frame"),
                                     expected_app_commit=case.get("appCommit"), expected_deployment=case.get("deployment"))
        except exchange.ExchangeError as error:
            if error.code != expected:
                raise RuntimeError(f"{case['name']} returned {error.code}, expected {expected}.") from error
        else:
            if expected is not None or result["technical_verdict"] != case["verdict"]:
                raise RuntimeError(f"{case['name']} accepted an unexpected result.")
    print(f"Python verified {len(cases)} signed exchange cases.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["COSIGN_PASSWORD"] = ""
    with TemporaryDirectory(prefix="setrya-exchange-vectors-") as temporary:
        build(args.output, Path(temporary))


if __name__ == "__main__":
    main()
