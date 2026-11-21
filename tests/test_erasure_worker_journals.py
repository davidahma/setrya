"""Independent producer processes and a retried, partially observed handoff."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from setrya.erasure.journal_merge import CollectedJournal, WorkerJournal
from setrya.erasure.subject import Subject
from setrya.offboarding.inputs import InvalidReport


class DistributedJournals(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.subject = Subject("1" * 64, "restricted")
        self.run = "release-42"
        self.collector = CollectedJournal(self.root / "merged.db", run_id=self.run)

    def emit(self, producer):
        subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from setrya.erasure.journal_merge import WorkerJournal
from setrya.erasure.subject import Subject
root, run, producer = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
subject = Subject("1" * 64, "restricted")
journal = WorkerJournal(root/(producer+".db"), run_id=run, producer=producer)
journal.record(subject, store=producer, key="chart-7", source="doc-7", action="stored")
journal.seal(subject, root/(producer+".json"))
""", str(self.root), self.run, producer], check=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        return self.root / (producer + ".json")

    def coverage(self, subject=None):
        return self.collector.coverage(subject or self.subject, run_id=self.run, expected_producers=["web", "worker"])

    def test_merge_workers_retry_and_trace_cannot_hide_missing_producer(self):
        web, worker = self.emit("web"), self.emit("worker")
        self.collector.ingest(web, producer="web")
        row = json.loads(worker.read_text())["events"][0]
        trace = {**self.subject.metadata(), "setrya_run": self.run, "setrya_producer": "worker",
                 "setrya_sequence": row["sequence"], "store": "worker", "copy_key": row["key"], "source": row["source"],
                 "action": row["action"], "observed_at": row["observed_at"], "details_json": "{}"}
        self.collector.ingest_trace(trace, producer="worker")
        self.assertEqual(self.coverage()["missing"], ["worker"])
        self.assertEqual(len(self.collector.history(self.subject)), 2)
        for _ in range(2):
            self.collector.ingest(worker, producer="worker")
            self.collector.ingest(web, producer="web")
        self.assertEqual(self.coverage()["missing"], [])
        self.assertEqual(self.coverage(Subject("2" * 64, "restricted"))["received"], [])
        self.assertEqual(len(self.collector.history(self.subject)), 2)
        self.collector.ingest_trace(trace, producer="worker")
        with self.assertRaisesRegex(InvalidReport, "Trace event absent"):
            self.collector.ingest_trace({**trace, "setrya_sequence": 8}, producer="worker")
        with self.assertRaisesRegex(InvalidReport, "different observations"):
            self.collector.ingest_trace({**trace, "copy_key": "changed"}, producer="worker")

    def test_reject_run_producer_and_post_seal_write(self):
        packet = self.emit("web")
        with self.assertRaises(InvalidReport):
            self.collector.ingest(packet, producer="worker")
        collector = CollectedJournal(self.root / "other.db", run_id="another")
        with self.assertRaises(InvalidReport):
            collector.ingest(packet, producer="web")
        with self.assertRaises(InvalidReport):
            WorkerJournal(self.root / "web.db", run_id="other", producer="web")
        worker = WorkerJournal(self.root / "web.db", run_id=self.run, producer="web")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "sealed"):
            worker.record(self.subject, store="web", key="late", source="doc-7", action="stored")

    def test_malformed_packet_cannot_partially_import_and_empty_producer_can_close(self):
        packet = self.emit("web")
        row = json.loads(packet.read_text())
        row["events"].append({**row["events"][0], "sequence": 0})
        packet.write_text(json.dumps(row))
        with self.assertRaises(InvalidReport):
            self.collector.ingest(packet, producer="web")
        self.assertEqual(self.collector.history(self.subject), ())
        self.assertEqual(self.coverage()["missing"], ["web", "worker"])
        empty = self.root / "idle.json"
        WorkerJournal(self.root / "worker.db", run_id=self.run, producer="worker").seal(self.subject, empty)
        self.collector.ingest(empty, producer="worker")
        self.assertEqual(self.coverage()["missing"], ["web"])
