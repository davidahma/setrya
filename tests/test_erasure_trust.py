"""Independent checks of the evidence required for a subject erasure receipt."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fixtures.erasure_v2.runner import run_erasure
from setrya.erasure import CopyJournal, Drain, Hold, Subject
from setrya.erasure.model import Disposition, ErasureCase, Read
from setrya.erasure.receipt import replay


class Shelf:
    proof = "direct"

    def __init__(self):
        self.contents = {"entry"}
        self.after_delete = lambda: None

    def find(self, subject):
        return self.contents.copy()

    def verify_absent(self, subject):
        return self.find(subject)

    def delete(self, subject):
        self.contents.clear()
        self.after_delete()


class App:
    def __init__(self, shelf):
        self.shelf = shelf
        self.on_drain = lambda: None
        self.on_reopen = lambda: None

    def hold(self, subject):
        return Hold("refresh-worker")

    def checkpoint(self, subject):
        return Hold("refresh-worker")

    def release_and_drain(self, subject, attempt):
        self.on_drain()
        return Drain(attempt, True)

    def read(self, subject, caller, question):
        self.on_reopen()
        return Read(caller, "mailbox", question, bool(self.shelf.contents), True)


class ErasureTrustTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.subject = Subject.derive(b"u" * 32, "trial", "original", "restricted")
        self.case = ErasureCase("mailbox", "which items are present?", ("owner", "deputy"))
        self.stores = {name: Shelf() for name in ("cache", "primary")}
        self.app = App(self.stores["primary"])
        self.journal = CopyJournal(self.root / "journal.db")
        self.path = self.root / "inventory.toml"
        self.path.write_text("version=1\n" + "".join(
            f'[[systems]]\nname="{name}"\nadapter="{name}"\nrole="local"\n'
            'owner="engineer"\napproved_labels=["restricted"]\nretention_days=0\n' for name in self.stores
        ))

    def drill(self, dispositions=()):
        receipt = run_erasure(self.subject, journal=self.journal, inventory=self.path, adapters=self.stores,
                              application=self.app, case=self.case, output=self.root / "result",
                              dispositions=dispositions)
        self.assertEqual(replay(self.root / "result"), receipt)
        return receipt

    def test_second_delete_cannot_hide_a_repopulated_cache_before_worker_cleanup(self):
        cache = self.stores["cache"]
        self.stores["primary"].after_delete = lambda: cache.contents.add("entry")
        self.app.on_drain = cache.contents.clear
        receipt = self.drill()
        self.assertEqual(receipt["verdict"], "FAIL", receipt)
        self.assertIn(("cache", "still present"), [(c["store"], c["status"]) for c in receipt["copies"]])

    def test_reopening_chat_cannot_hide_a_repopulated_cache_before_worker_cleanup(self):
        cache = self.stores["cache"]
        reads = 0

        def reopen():
            nonlocal reads
            reads += 1
            if reads == 4:
                cache.contents.add("entry")

        self.app.on_reopen = reopen
        self.app.on_drain = cache.contents.clear
        receipt = self.drill()
        self.assertEqual(receipt["verdict"], "FAIL", receipt)

    def test_a_direct_adapter_cannot_supply_two_systems_evidence(self):
        self.stores["cache"] = self.stores["primary"]
        with self.assertRaisesRegex(ValueError, "two inventory systems"):
            self.drill()
        self.assertTrue(self.stores["primary"].contents)

    def test_owner_disposition_bound_to_other_subject_rejected_before_mutation(self):
        claim = Disposition(store="cache", key="entry", owner="engineer", evidence="ticket-24",
                            subject_ref=replace(self.subject, ref="0" * 64).ref)
        with self.assertRaisesRegex(ValueError, "different subject"):
            self.drill([claim])
        self.assertTrue(self.stores["primary"].contents)
        self.assertFalse((self.root / "result").exists())

    def test_missing_owner_exhibit_location_preserves_observed_failure(self):
        claim = Disposition(store="cache", key="unobserved", owner="engineer", evidence="ticket-24",
                            subject_ref=self.subject.ref)
        self.app.on_drain = lambda: self.stores["primary"].contents.add("entry")
        receipt = self.drill([claim])
        self.assertEqual(receipt["verdict"], "FAIL", receipt)
        self.assertTrue(any("no observed copy" in gap for gap in receipt["gaps"]))

    def test_earlier_restricted_label_survives_a_later_relabeled_event(self):
        self.journal.record(replace(self.subject, classification="case-only"), store="cache", key="entry",
                            source="case", action="stored")
        self.journal.record(self.subject, store="cache", key="entry", source="case", action="retrieved")
        receipt = self.drill()
        self.assertEqual(receipt["verdict"], "FAIL", receipt)
        row = next(c for c in receipt["copies"] if c["store"] == "cache")
        self.assertEqual(row["classifications"], ["case-only", "restricted"])
