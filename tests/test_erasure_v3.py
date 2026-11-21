"""Release decisions for shared, versioned records and authenticated response facts."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setrya.erasure import Drain, Hold, Subject
from setrya.erasure.journal_merge import CollectedJournal, WorkerJournal
from setrya.erasure.v3 import Answer, Artifact, ExternalItem, SharedCase, Stored, run_shared
from setrya.erasure.v3_receipt import EVIDENCE, RECEIPT, derive, exit_code, replay
from setrya.offboarding.inputs import InvalidReport


class Application:
    def __init__(self):
        self.erased = False
        self.released = False
        self.hidden_key = True
        self.worker_calls = []

    def read(self, subject, caller, conversation, question):
        return Answer(caller, conversation, "/cases", question, question == "which?" and not self.erased,
                      question == "retained?")

    def inspect(self, subject, store, key):
        if store == "spool":
            return Stored(key, self.hidden_key, False, False, "v0" if self.hidden_key else None)
        return Stored(key, True, not self.erased, True, "v2" if self.erased else "v1")

    def scan(self, subject, store):
        return {"unknown-copy-id"} if not self.erased else set()

    def erase(self, subject):
        self.erased = True
        self.hidden_key = False

    def hold(self, subject):
        self.worker_calls.append("hold")
        return Hold("w")

    def checkpoint(self, subject):
        self.worker_calls.append("checkpoint")
        return Hold("w")

    def release_and_drain(self, subject, attempt):
        self.worker_calls.append("release")
        self.released = True
        return Drain(attempt, True)


class SharedReceiptTests(unittest.TestCase):
    def setUp(self):
        t = TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.out = Path(t.name)
        self.subject = Subject.derive(bytes(range(32)), "t", "s", "test")
        self.case = SharedCase("case-id", "/cases", ("reviewer", "owner"), "which?", "retained?",
                               (Artifact("notes", "summary", "rewrite"),), scan_stores=("spool",))
        self.app = Application()
        self.item = ExternalItem("provider", "call", "ops", "configuration", "ticket", "2027-01-03", self.subject.ref)

    def drill(self, name="run", **kwargs):
        directory = self.out / name
        receipt = run_shared(self.subject, case=self.case, application=self.app, output=directory, **kwargs)
        self.assertEqual(receipt, replay(directory))
        return receipt

    def test_deletion_only_and_external_followup(self):
        r = self.drill(mode="deletion", external_items=(self.item,))
        self.assertEqual((r["technical_verdict"], r["review_status"], r["race"]), ("PASS", "OPEN_ITEMS", "not exercised"))
        self.assertEqual((exit_code(r), exit_code(r, case_review=True)), (0, 3))
        self.assertEqual(self.app.worker_calls, [])
        self.assertEqual(r["open_items"], [self.item.__dict__])
        e = json.loads((self.out / "run" / EVIDENCE).read_bytes())
        self.assertEqual(e["baseline"]["discovered"][0]["key"], "unknown-copy-id")
        self.assertFalse(e["final"]["discovered"][0]["exists"])

    def test_semantic_residual_and_second_question_control(self):
        original = self.app.read
        def read(subject, caller, conversation, question):
            r = original(subject, caller, conversation, question)
            if self.app.released:
                return replace(r, target_present=question == "which?", control_present=False)
            return r
        self.app.read = read
        r = self.drill(external_items=[self.item])
        self.assertEqual((r["technical_verdict"], exit_code(r, case_review=True)), ("FAIL", 1))
        self.assertEqual(r["race"], "exercised")
        self.assertTrue(any("retained finding" in f for f in r["failures"]))
        self.assertTrue(any("recovered the erased finding" in f for f in r["failures"]))

    def test_lost_tag_and_then_missing_token_does_not_prove_key_deleted(self):
        self.app.erase = lambda _: setattr(self.app, "erased", True)
        r = self.drill()
        self.assertEqual(r["technical_verdict"], "FAIL")
        self.assertTrue(any("spool/unknown-copy-id" in f for f in r["failures"]))

    def test_shared_or_unknown_control_prevents_whole_key_deletion(self):
        original_case = self.case
        for declared in (False, True):
            for control in (True, None):
                with self.subTest(declared=declared, control=control):
                    self.app = Application()
                    inspect = self.app.inspect
                    def inspect_control(subject, store, key):
                        row = inspect(subject, store, key)
                        return replace(row, control_present=control) if store == "spool" else row
                    self.app.inspect = inspect_control
                    if declared:
                        artifact = Artifact("spool", "unknown-copy-id", "delete")
                        self.case = replace(original_case, artifacts=(*original_case.artifacts, artifact))
                    r = self.drill(f"mixed-{declared}-{control}")
                    self.assertEqual(r["technical_verdict"], "NOT TESTED")
                    self.assertTrue(any("Declare a rewrite" in gap and "spool/unknown-copy-id" in gap for gap in r["gaps"]))
                    self.assertFalse(self.app.erased)
                    self.assertTrue(self.app.released)

    def test_lineage_for_one_shared_artifact_cannot_cover_an_untracked_answer(self):
        source = {"store": "spool", "key": "unknown-copy-id", "revision": "v0"}
        summary = {"store": "notes", "key": "summary", "revision": "v1"}
        self.app.lineage = lambda subject: {"roots": [source], "derivations": [{"artifact": summary, "inputs": [source]}]}
        self.case = replace(self.case, require_lineage=True,
                            artifacts=(*self.case.artifacts, Artifact("answers", "downstream", "rewrite")))
        r = self.drill()
        self.assertEqual(r["technical_verdict"], "NOT TESTED")
        self.assertIn("Missing provenance for required rewrite answers/downstream@v1.", r["gaps"])

    def test_checked_caller_conversation_and_distinct_revision(self):
        original = self.app.read
        self.app.read = lambda *args: replace(original(*args), conversation="foreign")
        r = self.drill()
        self.assertEqual(r["technical_verdict"], "NOT TESTED")
        self.assertFalse(self.app.erased)
        self.assertTrue(self.app.released)

        self.app = Application()
        original = self.app.inspect
        self.app.inspect = lambda s, st, k: (replace(original(s, st, k), revision="v1")
                                             if st == "notes" else original(s, st, k))
        r = self.drill("unknown")
        self.assertEqual(r["technical_verdict"], "NOT TESTED")

    def test_wrong_worker_and_incomplete_journal_block_technical_pass(self):
        self.app.release_and_drain = lambda s, a: Drain("different", True)
        r = self.drill()
        self.assertEqual((r["technical_verdict"], r["race"]), ("NOT TESTED", "incomplete"))
        self.app = Application()
        class Partial:
            def snapshot(self, subject, *, run_id, expected_producers, max_bytes):
                return {"run_id": run_id, "expected": list(expected_producers), "received": ["web"],
                        "missing": ["job"], "unsealed": []}, []
        r = self.drill("distributed", journal=Partial(), expected_producers=("web", "job"), run_id="suite-17")
        self.assertEqual(r["technical_verdict"], "NOT TESTED")
        self.assertIn("Missing durable journal for job.", r["gaps"])

    def test_trace_from_unlisted_producer_is_a_gap_even_at_a_known_location(self):
        run = "trace-only"
        journal = CollectedJournal(self.out / "collector.db", run_id=run)
        web = WorkerJournal(self.out / "web.db", run_id=run, producer="web")
        packet = self.out / "web.json"
        web.seal(self.subject, packet)
        journal.ingest(packet, producer="web")
        trace = {**self.subject.metadata(), "setrya_run": run, "setrya_producer": "archiver",
                 "setrya_sequence": 1, "store": "notes", "copy_key": "summary", "source": "doc-7", "action": "stored",
                 "observed_at": "2026-11-21T12:00:00+00:00", "details_json": "{}"}
        journal.ingest_trace(trace, producer="archiver")
        r = self.drill(journal=journal, expected_producers=("web",), run_id=run)
        self.assertEqual(r["technical_verdict"], "NOT TESTED")
        self.assertIn("Unexpected producer archiver has observations but no durable journal.", r["gaps"])

    def test_required_store_cannot_be_external_or_borrow_another_subjects_claim(self):
        for claim in (replace(self.item, store="notes"), replace(self.item, subject_ref="0" * 64)):
            with self.assertRaises(InvalidReport):
                self.drill(external_items=[claim])
        self.assertFalse(self.app.erased)

    def test_missing_store_evidence_and_adapter_errors_cannot_be_waived(self):
        inspect = self.app.inspect
        def missing(s, store, key):
            if store == "notes" and self.app.erased:
                return None
            return inspect(s, store, key)
        self.app.inspect = missing
        r = self.drill(external_items=[self.item])
        self.assertEqual(r["technical_verdict"], "NOT TESTED")
        self.app = Application()
        def failed(*_):
            raise ConnectionError("private connection details")
        self.app.release_and_drain = failed
        r = self.drill("error", external_items=[self.item])
        self.assertEqual((r["technical_verdict"], exit_code(r)), ("ERROR", 2))
        self.assertNotIn("private connection details", json.dumps(r))

    def test_replay_rejects_different_case_or_altered_observations(self):
        self.drill()
        d = self.out / "run"
        e = json.loads((d / EVIDENCE).read_bytes())
        e["final"]["answers"][2]["caller"] = "reviewer"
        with self.assertRaises(InvalidReport):
            derive(e, "0" * 64)
        (d / RECEIPT).write_text((d / RECEIPT).read_text().replace('"version": 3', '"version": true'))
        with self.assertRaises(InvalidReport):
            replay(d)
