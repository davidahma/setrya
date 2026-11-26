import hashlib
import json
from pathlib import Path
from sqlite3 import connect
from contextlib import closing, redirect_stdout
from io import StringIO
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from setrya.casework import JournalObserver, observe_deletion, stores
from setrya.casework.recheck import recheck
from setrya.cli import main
from setrya.erasure import ExternalItem, run_shared
from setrya.erasure.journal import CopyJournal
from setrya.erasure.subject import Subject
from setrya.examples.workspace import SIMPLE, open_fixture
from setrya.offboarding.inputs import InvalidReport
from setrya.receipts import replay


class ObservedDeletionTests(TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.p = Path(root.name)
        self.journal = self.p / "journal.sqlite"
        self.store = self.p / "store.sqlite"
        self.mapping = self.p / "observer.toml"
        self.frame = self.p / "frame.json"
        self.subject = Subject.derive(b"s" * 32, "tenant", "subject", "restricted")
        self.control = Subject.derive(b"s" * 32, "tenant", "control", "restricted")
        self.log = CopyJournal(self.journal)
        self.log.record(self.subject, store="archive", key="exclusive-key", source="test", action="stored")
        self.frame.write_text(json.dumps({"format": "setrya.completed-requests.v1", "requests": [
            {"id": "N-42", "subject_ref": self.subject.ref, "stores": ["archive"], "completed_at": "2020-01-01T00:00:00Z"}]}))
        self.mapping.write_text(f"""[stores.archive]
kind="sqlite"
path="store.sqlite"
table="notes"
key_column="id"
control_key="keep"
subject_column="ref"
control_subject_ref="{self.control.ref}"
owner="case-owner"
follow_up="2020-01-02"
""")
        with closing(connect(self.store)) as db, db:
            db.execute("CREATE TABLE notes(id TEXT PRIMARY KEY, ref TEXT)")
            db.execute("INSERT INTO notes VALUES(?,?)", ("keep", self.control.ref))

    def observe(self, name, **kwargs):
        return observe_deletion(frame=self.frame, request="N-42", journal=self.journal,
                                inventory_file=self.mapping, output=self.p/name, **kwargs)

    def follow(self, source, name, **kwargs):
        return recheck(self.p/source, output=self.p/name, journal=self.journal, inventory_file=self.mapping, **kwargs)

    def test_issue_and_exchange_require_sequential_observer_snapshots(self):
        from setrya.casework import exchange
        from setrya.casework.profiles import EVIDENCE, OBSERVED, issue

        self.observe("chronology")
        original_evidence = json.loads((self.p / "chronology" / EVIDENCE).read_bytes())
        original_evidence["find"]["journal_read_at"] = "2020-01-01T00:00:01Z"
        original_evidence["find"]["checked_at"] = "2020-01-01T00:00:02Z"
        for lane, timestamp in (
            ("sequential", original_evidence["find"]["checked_at"]),
            ("overlap", original_evidence["find"]["journal_read_at"]),
        ):
            with self.subTest(lane=lane):
                evidence = {**original_evidence, "verify_absent": {
                    **original_evidence["verify_absent"], "journal_read_at": timestamp,
                }}
                folder, output = self.p / lane, self.p / f"{lane}-exchange"
                folder.mkdir()
                options = {"output": output, "system": "test-app", "version": "1"}
                if lane == "overlap":
                    with self.assertRaisesRegex(InvalidReport, "second journal snapshot"):
                        issue(OBSERVED, evidence, folder)
                    self.assertFalse((folder / "erasure-receipt.json").exists())
                    self.assertFalse(output.exists())
                else:
                    original = issue(OBSERVED, evidence, folder)
                    self.assertEqual(replay(folder), original)
                    with patch.object(exchange.signing, "sign_bytes", side_effect=lambda raw, path, **_: path.touch()):
                        manifest = exchange.pack(folder, **options)
                    self.assertEqual(manifest["technical_verdict"], "PASS")

    def test_collector_and_local_events_share_one_observation_and_recheck(self):
        from setrya.erasure.journal_merge import CollectedJournal, WorkerJournal

        self.journal = self.p / "combined.sqlite"
        collector = CollectedJournal(self.journal, run_id="delete-42")
        CopyJournal(self.journal).record(self.subject, store="archive", key="removed",
                                         source="api", action="stored")
        worker = WorkerJournal(self.p / "worker.sqlite", run_id="delete-42", producer="worker")
        worker.record(self.subject, store="archive", key="worker-key", source="memo", action="derived")
        packet = self.p / "worker.json"
        worker.seal(self.subject, packet)
        collector.ingest(packet, producer="worker")
        with closing(connect(self.store)) as db, db:
            db.execute("INSERT INTO notes VALUES (?,?)", ("worker-key", self.subject.ref))
        self.assertEqual(self.observe("no-roster")["technical_verdict"], "NOT TESTED")
        original_mapping = self.mapping.read_text()
        roster = '\n[journal]\nrun_id="delete-42"\nexpected_producers=["worker"]\n'
        self.mapping.write_text(original_mapping + roster)
        original = self.observe("worker-still-present")
        self.assertEqual(original["technical_verdict"], "FAIL")
        self.assertCountEqual([row["status"] for row in original["checks"]], ["ABSENT", "PRESENT"])
        self.assertTrue(all(len(row["collector_sha256"]) == 64 for row in original["checks"]))
        self.assertEqual(replay(self.p / "worker-still-present"), original)
        with closing(connect(self.store)) as db, db:
            db.execute("DELETE FROM notes WHERE id=?", ("worker-key",))
        self.assertEqual(self.follow("worker-still-present", "completed")["items"][0]["status"], "verified")
        self.assertEqual(self.observe("now-absent")["technical_verdict"], "PASS")
        self.mapping.write_text(original_mapping + roster.replace('["worker"]', '["api","worker"]'))
        pending = self.observe("missing-producer")
        self.assertEqual(pending["technical_verdict"], "NOT TESTED")
        self.assertIn("journal-producer-coverage-incomplete", pending["gaps"])
        self.assertEqual(self.follow("worker-still-present", "pending")["items"][0]["status"], "still open")

    def test_collector_only_checks_keys_and_keeps_shared_or_provider_obligations_open(self):
        from setrya.erasure.journal_merge import CollectedJournal, WorkerJournal

        self.journal = self.p / "review.sqlite"
        collector = CollectedJournal(self.journal, run_id="observed")
        worker = WorkerJournal(self.p / "worker.sqlite", run_id="observed", producer="web")
        for subject in (self.subject, self.control):
            worker.record(subject, store="archive", key="shared", source="team", action="stored")
            if subject == self.subject:
                worker.record(subject, store="archive", key="sent", source="remote", action="sent")
                worker.record(subject, store="archive", key="own", source="local", action="stored")
            path = self.p / (subject.ref + ".json")
            worker.seal(subject, path)
            collector.ingest(path, producer="web")
        with self.mapping.open("a") as stream:
            stream.write('\n[journal]\nrun_id="observed"\nexpected_producers=["web"]\n')
        result = self.observe("scope")
        self.assertCountEqual([row["status"] for row in result["checks"]], ["UNKNOWN", "UNKNOWN", "ABSENT"])
        self.assertEqual(result["technical_verdict"], "NOT TESTED")
        self.assertIn("external-or-shared-key", result["gaps"])

    def test_linked_customer_summary_preserves_the_original_request_on_each_recheck(self):
        from setrya.casework.receipt_summary import receipt_summary

        self.observe("initial")
        for source, output in (("initial", "first"), ("first", "second")):
            receipt = self.follow(source, output)
            with self.subTest(generation=output):
                self.assertEqual(receipt["request_id"], "N-42")
                self.assertEqual(replay(self.p / output), receipt)
                self.assertIn("Deletion review for N-42", receipt_summary(self.p / output, audience="customer"))
                with self.assertRaisesRegex(InvalidReport, "differs from the replayed evidence"):
                    receipt_summary(self.p / output, audience="customer", case_id="WRONG")

    def test_independent_reads_replay_offline_and_public_entrypoints(self):
        before = [p.read_bytes() for p in (self.store, self.journal)]
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["observe", "request", "--id", "N-42", "--population", str(self.frame),
                                   "--inventory", str(self.mapping), "--journal", str(self.journal),
                                   "--receipt", str(self.p/"issued")]), 0)
        self.assertEqual(before, [p.read_bytes() for p in (self.store, self.journal)])
        self.store.unlink()
        self.journal.unlink()
        result = replay(self.p/"issued")
        self.assertEqual(result["profile"], "observed-deletion")
        self.assertEqual(result["technical_verdict"], "PASS")
        self.assertEqual([(r["find"],r["verify_absent"]) for r in result["checks"]], [("ABSENT","ABSENT")])
        self.assertNotIn("exclusive-key", json.dumps(result))
        self.assertNotIn("store.sqlite", (self.p/"issued"/"observation-evidence.json").read_text())
        # A nonexistent journal does not get created by the reader.
        missing = self.observe("missing")
        self.assertEqual(missing["technical_verdict"], "NOT TESTED")
        self.assertFalse(self.journal.exists())

    def test_residual_in_either_snapshot_wins_and_changed_scope_is_uncertain(self):
        verify = JournalObserver.verify_absent
        def verify_after_restore(observer, req):
            with closing(connect(self.store)) as db, db:
                db.execute("INSERT INTO notes VALUES(?,?)", ("exclusive-key", self.subject.ref))
            return verify(observer, req)
        with patch.object(JournalObserver, "verify_absent", verify_after_restore):
            result = self.observe("restored")
        self.assertEqual(result["checks"][0]["status"], "PRESENT")
        self.assertEqual(replay(self.p/"restored")["technical_verdict"], "FAIL")

        def verify_after_journal_update(observer, req):
            self.log.record(self.subject, store="archive", key="changed", source="test", action="derived")
            return verify(observer, req)
        with closing(connect(self.store)) as db, db:
            db.execute("DELETE FROM notes WHERE id='exclusive-key'")
        with patch.object(JournalObserver, "verify_absent", verify_after_journal_update):
            result = self.observe("changed")
        self.assertEqual(result["technical_verdict"], "NOT TESTED")
        self.assertIn("journal-scope-changed", result["gaps"])

    def test_due_recheck_needs_direct_absence_and_scanned_subject_absence(self):
        # App-controlled subject and provider share a historical request; a
        # successful SQL read may close only the explicitly bound local item.
        with open_fixture(shared=False) as (subject, app, unused):
            self.log.record(subject, store="archive", key="held", source="test", action="stored")
            items = [
                ExternalItem("archive", "held", "support", "retained", "ticket", "2020-01-02", subject.ref),
                ExternalItem("provider", "held", "privacy", "configuration", "settings", "2020-01-02", subject.ref),
                ExternalItem("archive", "later", "support", "retained", "ticket", "2999-01-02", subject.ref),
            ]
            source = self.p / "original"
            original = run_shared(subject, case=SIMPLE, application=app, output=source, mode="deletion",
                                  external_items=items)
        self.assertEqual(original["technical_verdict"], "PASS")
        fingerprint = hashlib.sha256((source/"erasure-receipt.json").read_bytes()).hexdigest()
        with self.assertRaises(InvalidReport):
            self.follow("original", "wrong-pin", expected_parent_sha256="0"*64)
        self.assertFalse((self.p/"wrong-pin").exists())
        with closing(connect(self.store)) as db, db:
            db.execute("INSERT INTO notes VALUES(?,?)", ("other-id", subject.ref))
        live = self.follow("original", "scan-hit", expected_parent_sha256=fingerprint)
        self.assertEqual(live["technical_verdict"], "FAIL")
        self.assertEqual(live["items"][0]["reason"], "residual-found")
        self.assertEqual(live["parent_sha256"], fingerprint)
        with closing(connect(self.store)) as db, db:
            db.execute("DELETE FROM notes WHERE id='other-id'")
        with patch.object(stores, "sqlite_reader", wraps=stores.sqlite_reader) as snapshot:
            guard = self.follow("original", "clear")
        snapshot.assert_called_once_with(self.store)
        self.assertEqual(guard["technical_verdict"], "PASS")
        self.assertEqual(guard["review_status"], "OPEN_ITEMS")
        self.assertEqual([i["status"] for i in guard["items"]], ["verified", "still open", "still open"])
        self.assertEqual([i["reason"] for i in guard["items"]], ["absent-in-both-reads", "configuration-only", "not-due"])
        code = self.p/"cli"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["recheck", str(source), "--output", str(code)]), 3)
        self.assertEqual(replay(code)["items"][0]["reason"], "unmapped-store")
        chained = self.follow("clear", "next")
        self.assertEqual(len(chained["items"]), 2)
        # The copied parent is sufficient to check the chain completely offline.
        self.store.unlink()
        self.journal.unlink()
        self.assertEqual(replay(self.p/"next"), chained)
        manifest = self.p/"next"/"parent"/"erasure-receipt.json"
        manifest.write_bytes(manifest.read_bytes()+b" ")
        with self.assertRaisesRegex(InvalidReport, "different receipt"):
            replay(self.p/"next")

    def test_recheck_requires_positive_scan_control_and_exclusive_journal_binding(self):
        with closing(connect(self.store)) as db, db:
            db.execute("INSERT INTO notes VALUES(?,?)", ("exclusive-key", self.subject.ref))
        controls = [{"framework":"SOC 2","reference":"C1.2"}]
        original = self.observe("observed", controls=controls)
        self.assertEqual(original["technical_verdict"], "FAIL")
        with closing(connect(self.store)) as db, db:
            db.execute("DELETE FROM notes WHERE id='exclusive-key'")
            db.execute("UPDATE notes SET ref=? WHERE id='keep'", ("mismatched",))
        after = self.follow("observed", "unproven")
        self.assertEqual(after["items"][0]["reason"], "read-or-scan-unavailable")
        self.assertEqual(after["items"][0]["status"], "still open")
        self.assertEqual(after["original_verdict"], "FAIL")
        self.assertEqual(after["controls"], controls)
        with closing(connect(self.store)) as db, db:
            db.execute("INSERT INTO notes VALUES(?,?)", ("exclusive-key", self.subject.ref))
        positive = self.follow("observed", "direct-hit")
        self.assertEqual(positive["items"][0]["reason"], "residual-found")
        with closing(connect(self.store)) as db, db:
            db.execute("DELETE FROM notes WHERE id='exclusive-key'")
            db.execute("UPDATE notes SET ref=? WHERE id='keep'", (self.control.ref,))
        after = self.follow("observed", "verified")
        self.assertEqual(after["items"][0]["status"], "verified")
        self.assertEqual(after["technical_verdict"], "FAIL")  # Prior recorded residual still stands.
        self.log.record(self.control, store="archive", key="exclusive-key", source="test", action="stored")
        after = self.follow("observed", "shared")
        self.assertEqual(after["items"][0]["reason"], "no-exclusive-journal-key")

    def test_newer_local_event_does_not_erase_a_send_or_other_ownership(self):
        with closing(connect(self.store)) as db, db:
            db.execute("INSERT INTO notes VALUES(?,?)", ("exclusive-key", self.subject.ref))
        self.observe("prior")
        with closing(connect(self.store)) as db, db:
            db.execute("DELETE FROM notes WHERE id='exclusive-key'")
        self.log.record(self.subject, store="archive", key="exclusive-key", source="first", action="sent")
        self.log.record(self.subject, store="archive", key="exclusive-key", source="newer", action="stored")
        self.assertEqual(self.follow("prior","sent")["items"][0]["reason"], "no-exclusive-journal-key")

    def test_observer_and_linked_receipt_conform_to_the_published_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("requires jsonschema")
        schema = json.loads((Path(__file__).parents[1]/"setrya/erasure/receipt-v3.schema.json").read_bytes())
        result = self.observe("issued", controls=[{"framework":"DPA","reference":"clause 4.2"}])
        jsonschema.validate(result, schema)
        result = self.follow("issued","followup")
        jsonschema.validate(result, schema)

    def test_live_postgres_read_only_role_and_retained_scan_control(self):
        import os
        import secrets
        dsn = os.environ.get("SETRYA_PGVECTOR_DSN")
        if not dsn:
            self.skipTest("requires disposable PostgreSQL")
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        name = "obs_" + secrets.token_hex(8)
        role = sql.Identifier(name)
        password = secrets.token_hex(24)
        table = sql.Identifier(name, "notes")
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(password)))
            try:
                db.execute(sql.SQL("CREATE SCHEMA {}").format(role))
                db.execute(sql.SQL("CREATE TABLE {}(id text PRIMARY KEY, ref text)").format(table))
                for key, subject in (("keep",self.control),("exclusive-key",self.subject)):
                    db.execute(sql.SQL("INSERT INTO {} VALUES (%s,%s)").format(table), (key,subject.ref))
                db.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(role, role))
                db.execute(sql.SQL("GRANT SELECT(id,ref) ON {} TO {}").format(table, role))
                self.mapping.write_text(self.mapping.read_text().replace('kind="sqlite"\npath="store.sqlite"',
                                            f'kind="postgres"\nschema="{name}"\ndsn_env="SET_OBSERVER_DSN"'))
                with patch.dict(os.environ, SET_OBSERVER_DSN=make_conninfo(dsn,user=name,password=password)):
                    self.assertEqual(self.observe("existing")["technical_verdict"], "FAIL")
                    db.execute(sql.SQL("DELETE FROM {} WHERE id='exclusive-key'").format(table))
                    self.assertEqual(self.follow("existing", "verified")["items"][0]["status"], "verified")
                    db.execute(sql.SQL("GRANT UPDATE(ref) ON {} TO {}").format(table,role))
                    self.assertEqual(self.observe("writable")["technical_verdict"], "NOT TESTED")
                    self.assertEqual(self.follow("existing", "writable-recheck")["items"][0]["status"], "still open")
            finally:
                db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(role))
                db.execute(sql.SQL("DROP ROLE {}").format(role))
