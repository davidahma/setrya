"""Receipt contract, coverage, and preservation of native verdicts."""
import json
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
import unittest

from setrya.erasure import CopyJournal, Disposition, Drain, ErasureCase, Hold, Read, Subject, run_erasure
from setrya.erasure.receipt import EVIDENCE, RECEIPT, replay
from setrya.offboarding.inputs import InvalidReport


class Store:
    proof = "direct"

    def __init__(self):
        self.keys = {"chunk"}

    def find(self, subject):
        return self.keys.copy()

    def delete(self, subject):
        self.keys.clear()

    verify_absent = find


class App:
    def __init__(self, store):
        self.store = store

    def hold(self, subject):
        return Hold("worker-1")

    checkpoint = hold

    def release_and_drain(self, subject, attempt):
        return Drain(attempt, True)

    def read(self, subject, caller, question):
        return Read(caller, "chat", question, "chunk" in self.store.keys, True)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.subject = Subject.derive(bytes(range(32)), "team", "test", "private")
        self.inventory = self.root / "inventory.toml"
        self.inventory.write_text('version = 1\n[[systems]]\nname = "source"\nowner = "ops"\nrole = "primary"\n'
                                  'approved_labels = ["private"]\nretention_days = 0\nadapter = "src"\n')
        self.journal = CopyJournal(self.root / "log.db")
        self.store = Store()
        self.app = App(self.store)

    def check(self, **kwargs):
        receipt = run_erasure(self.subject, journal=self.journal, inventory=self.inventory, adapters={"src": self.store},
                              application=self.app, case=ErasureCase("chat", "sample", ("a", "b")),
                              output=self.root / "run", **kwargs)
        self.assertEqual(replay(self.root / "run"), receipt)
        return receipt

    def test_discovery_without_a_journal_entry_and_portability(self):
        r = self.check()
        self.assertEqual((r["verdict"], r["copies"][0]["status"]), ("PASS", "verified gone"))
        copied = copytree(self.root / "run", self.root / "detached")
        evidence = copied / EVIDENCE
        evidence.write_bytes(evidence.read_bytes() + b" ")
        with self.assertRaises(InvalidReport):
            replay(copied)
        original = self.root / "run" / RECEIPT
        r["version"] = True
        original.write_text(json.dumps(r))
        with self.assertRaises(InvalidReport):
            replay(self.root / "run")

    def test_journal_key_never_found_by_direct_lookup(self):
        self.journal.record(self.subject, store="source", key="other", source="input", action="stored")
        r = self.check()
        self.assertEqual((r["verdict"], r["unknown_copies"]), ("NOT TESTED", 1))

    def test_unknown_destination_and_forbidden_handling(self):
        self.journal.record(self.subject, store="export", key="k", source="chunk", action="sent")
        self.inventory.write_text(self.inventory.read_text().replace('["private"]', '["routine"]'))
        r = self.check()
        self.assertEqual((r["verdict"], r["unknown_copies"]), ("FAIL", 1))
        self.assertTrue(any("export" in gap for gap in r["gaps"]))

    def test_dispositions_stay_open(self):
        with self.inventory.open("a") as out:
            out.write('[[systems]]\nname = "external"\nowner = "ops"\nrole = "provider"\n'
                      'approved_labels = ["private"]\nretention_days = 30\nadapter = "none"\n')
        claims = []
        for key, until in (("log", None), ("backup", "2099-01-01"), ("expired", "2000-01-01")):
            self.journal.record(self.subject, store="external", key=key, source="chunk", action="sent")
            claims.append(Disposition("external", key, "ops", "case-1", subject_ref=self.subject.ref, until=until))
        r = self.check(dispositions=claims)
        states = {c["key"]: c["status"] for c in r["copies"]}
        self.assertEqual(states, {"log": "attested", "backup": "retained until", "expired": "unknown",
                                  "chunk": "verified gone"})
        self.assertEqual((r["verdict"], r["unknown_copies"]), ("NOT TESTED", 1))
