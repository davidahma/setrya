import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.drill import InputError
from setrya.erasure.canary import SQLiteCanaryScanner
from setrya.erasure.investigation import (
    ALICE_KEY, BOB_KEY, CALLERS, CONTROL_FACT, CONTROL_QUESTION, CONVERSATION, Investigation,
    TARGET_FACT, TARGET_QUESTION, run_case,
)
from setrya.erasure.journal import CopyJournal
from setrya.erasure.lineage import VersionLineage, VersionRef
from setrya.erasure.subject import Subject


def identities():
    return [Subject.derive(bytes(range(32)), "fixture", name, "restricted") for name in ("person-a", "person-b")]


class LineageAndScannerTests(unittest.TestCase):
    def test_lineage_is_versioned_idempotent_and_scoped(self):
        with TemporaryDirectory() as folder:
            log = CopyJournal(Path(folder) / "log.sqlite")
            lineage = VersionLineage(log, "one")
            other = VersionLineage(log, "two")
            alice, bob = identities()
            a = VersionRef("source", "a", "1")
            b = VersionRef("source", "b", "1")
            note = VersionRef("notes", "case", "2")
            answer = VersionRef("answers", "case", "3")
            new = VersionRef("notes", "case", "4")
            lineage.record(a, subjects=[alice])
            lineage.record(b, subjects=[bob])
            lineage.record(note, inputs=[a, b])
            lineage.record(note, inputs=[b, a])
            lineage.record(answer, inputs=[note])
            lineage.record(new, inputs=[b])
            with self.assertRaises(InputError):
                lineage.record(note, inputs=[b])
            with self.assertRaises(InputError):
                other.record(note, inputs=[a])
            roots, edges = (lineage.affected(alice)[name] for name in ("roots", "derivations"))
            self.assertEqual([x["key"] for x in roots], ["a"])
            self.assertEqual([edge["artifact"]["revision"] for edge in edges], ["2", "3"])
            self.assertEqual({row["key"] for row in edges[0]["inputs"]}, {"a", "b"})
            self.assertEqual(other.affected(alice)["derivations"], [])

    def test_scanner_uses_literal_text_and_labels_unsupported_schemas(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE stored(id TEXT, body TEXT, scope TEXT)")
        db.executemany("INSERT INTO stored VALUES(?,?,?)", [
            ("a", "xTEST_%MARK: only here", "one"), ("b", "TEST_xyzMARK", "one"), ("c", "TEST_%MARK", "two")])
        db.commit()
        scanner = SQLiteCanaryScanner(db, table="stored", key="id", content="body", scope_column="scope")
        self.assertEqual(scanner.find(scope="one", marker="TEST_%MARK"), {"a"})
        db.execute("DELETE FROM stored WHERE id='a'")
        with self.assertRaisesRegex(InputError, "commit"):
            scanner.find(scope="one", marker="TEST_%MARK")
        db.rollback()
        self.assertEqual(scanner.find(scope="one", marker="TEST_%MARK"), {"a"})
        with db:
            db.execute("INSERT INTO stored VALUES ('binary', ?, 'one')", (b"binary data",))
        with self.assertRaisesRegex(InputError, "text canary column"):
            scanner.find(scope="one", marker="TEST_%MARK")
        wrong = SQLiteCanaryScanner(db, table="stored", key="id", content="missing", scope_column="scope")
        with self.assertRaises(sqlite3.OperationalError):
            wrong.find(scope="one", marker="TEST_%MARK")


def _langgraph_installed():
    try:
        import langgraph.checkpoint.sqlite
        import langgraph.graph
    except ImportError:
        return False
    return True


@unittest.skipUnless(_langgraph_installed(), "install the erasure extra for the shared investigation")
class InvestigationTests(unittest.TestCase):
    def test_erase_retains_same_conversation_and_scans_only_owned_workspace(self):
        with TemporaryDirectory() as folder:
            alice, bob = identities()
            log = CopyJournal(Path(folder) / "log.sqlite")
            with Investigation(folder, alice, bob, log, guarded=True) as app:
                app.seed()
                snapshot, = app.scan(alice, "snapshots")
                self.assertNotIn(snapshot, {copy.key for copy in log.copies(alice)})
                self.assertNotIn(snapshot, json.dumps(app.lineage(alice)))
                old = app.inspect(alice, "checkpoints", CONVERSATION)
                self.assertTrue(old.target_present and old.control_present)
                self.assertIn(TARGET_FACT, app.reopen()["summary"])
                self.assertNotIn(app.marker, app.reopen()["summary"])
                self.assertEqual(app.reopen()["conversation"], CONVERSATION)
                with self.assertRaises(ValueError):
                    app.erase(bob)
                app.erase(alice)
                self.assertFalse(app.inspect(alice, "snapshots", snapshot).exists)
                self.assertEqual(app.scan(alice, "snapshots"), set())
                self.assertEqual(app.db.execute("SELECT id FROM snapshots").fetchall(), [("other-workspace",)])
                self.assertFalse(app.inspect(alice, "source", ALICE_KEY).exists)
                self.assertTrue(app.inspect(alice, "source", BOB_KEY).control_present)
                new = app.inspect(alice, "checkpoints", CONVERSATION)
                self.assertNotEqual(old.revision, new.revision)
                self.assertFalse(new.target_present)
                self.assertTrue(new.control_present)
                for caller in CALLERS:
                    first = app.read(alice, caller, CONVERSATION, TARGET_QUESTION)
                    second = app.read(alice, caller, CONVERSATION, CONTROL_QUESTION)
                    self.assertEqual(first.conversation, CONVERSATION)
                    self.assertEqual(second.caller, caller)
                    self.assertFalse(first.target_present)
                    self.assertTrue(second.control_present)
                self.assertIn(CONTROL_FACT, app.reopen()["summary"])

    def test_an_exact_canary_match_cannot_authorize_deletion_of_mixed_export(self):
        with TemporaryDirectory() as folder:
            alice, bob = identities()
            with Investigation(folder, alice, bob, CopyJournal(Path(folder) / "log.sqlite"), guarded=True) as app:
                app.seed()
                with app.db:
                    app.db.execute("INSERT INTO snapshots VALUES (?,?,?,?)",
                                   ("mixed", CONVERSATION, "disposable-export", app.marker + CONTROL_FACT))
                app.erase(alice)
                self.assertEqual(app.scan(alice, "snapshots"), {"mixed"})
                self.assertTrue(app.inspect(alice, "snapshots", "mixed").control_present)

    def test_app_rebuild_must_remove_historical_snapshots(self):
        with TemporaryDirectory() as folder:
            alice, bob = identities()
            with Investigation(folder, alice, bob, CopyJournal(Path(folder) / "log.sqlite"), guarded=True) as app:
                app.seed()
                with patch.object(app.saver, "delete_thread"):
                    app.erase(alice)
                self.assertFalse(app.read(alice, CALLERS[0], CONVERSATION, TARGET_QUESTION).target_present)
                self.assertTrue(app.inspect(alice, "checkpoints", CONVERSATION).target_present)

    def test_replayable_modes_record_the_decisive_shared_store_and_endpoint_observations(self):
        from setrya.erasure.v3_receipt import derive
        from hashlib import sha256

        with TemporaryDirectory() as folder:
            for mode, guarded, expected in (("late-write", False, "FAIL"), ("late-write", True, "PASS"),
                                             ("deletion", True, "PASS")):
                with self.subTest(mode=mode, guarded=guarded):
                    out = Path(folder) / f"{mode}-{guarded}"
                    receipt = run_case(out, mode=mode, guarded=guarded)
                    self.assertEqual(receipt["technical_verdict"], expected, receipt)
                    raw = (out / "erasure-evidence.json").read_bytes()
                    ev = json.loads(raw)
                    self.assertEqual(derive(ev, sha256(raw).hexdigest()), receipt)
                    self.assertEqual(ev["case"]["conversation"], CONVERSATION)
                    self.assertEqual({q["caller"] for q in ev["final"]["answers"] if q}, set(CALLERS))
                    self.assertEqual([r["target_present"] for r in ev["final"]["answers"]],
                                     [not guarded, False, not guarded, False])
                    self.assertEqual([r["control_present"] for r in ev["final"]["answers"]], [False, True, False, True])
                    self.assertEqual(len(ev["baseline"]["scans"]["snapshots"]), 1)
                    self.assertFalse(ev["final"]["scans"]["snapshots"])
                    self.assertNotIn(TARGET_FACT.encode(), raw)
                    self.assertNotIn(CONTROL_FACT.encode(), raw)
                    if guarded:
                        self.assertEqual(receipt["review_status"], "OPEN_ITEMS")
                    self.assertEqual(receipt["race"], "exercised" if mode == "late-write" else "not exercised")
