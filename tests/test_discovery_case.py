"""Policy, source-scoped lineage and receipt bindings for discovery case v1."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from setrya.discovery import case as discovery
from setrya.discovery.macie import load_finding
from setrya.discovery.mapping import bind_finding, load_mapping
from setrya.erasure import Subject
from setrya.erasure.v3 import Answer, Artifact, SharedCase, Stored
from setrya.offboarding.inputs import InvalidReport


class Application:
    def __init__(self, source="7", separate_root=False):
        self.source = source
        self.separate_root = separate_root
        self.erased = False
        self.reads = 0

    def read(self, subject, caller, conversation, question):
        self.reads += 1
        return Answer(caller, conversation, "/read", question, not self.erased and question == "target?",
                      question == "control?")

    def inspect(self, subject, store, key):
        if store == "source":
            return Stored(key, not self.erased, not self.erased, False, None if self.erased else self.source)
        return Stored(key, True, not self.erased, True, "2" if self.erased else "1")

    def erase(self, subject):
        raise AssertionError("A finding must not invoke a subject-wide erase.")

    def erase_approved(self, subject, source, actions):
        if source != {"store": "source", "key": "main", "revision": self.source, "action": "delete"}:
            raise ValueError("source changed before mutation")
        if not any(row == {"store": "cache", "key": "shared", "action": "rewrite"} for row in actions):
            raise ValueError("shared rewrite was not approved")
        self.erased = True
        return True

    def lineage(self, subject):
        root = {"store": "source", "key": "main", "revision": self.source}
        other = {**root, "key": "other"}
        return {"roots": [root, other] if self.separate_root else [root],
                "derivations": [{"artifact": {"store": "cache", "key": "shared", "revision": "1"},
                                 "inputs": [other if self.separate_root else root]}]}


class DiscoveryCaseTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.subject = Subject("a" * 64, "restricted")
        case = SharedCase("shared", "/read", ("caller-1", "caller-2"), "target?", "control?",
                          (Artifact("source", "main", "delete"), Artifact("cache", "shared", "rewrite")),
                          require_lineage=True)
        self.case = case
        location = Path(__file__).resolve().parents[1] / "setrya" / "discovery"
        mapping = load_mapping(location / "mapping-example.json")
        self.binding = bind_finding(load_finding(location / "macie-example.json"), mapping)
        self.binding.update(conversation=case.conversation, callers=list(case.callers), route=case.route,
                            questions=case.spec()["questions"], subject_ref=self.subject.ref,
                            source={"store": "source", "key": "main", "action": "delete", "revision": "7"},
                            services=[{"id": "primary", "role": "source", "owner": "ingestion",
                                       "store": "source", "key": "main", "action": "delete"},
                                      {"id": "shared-answer", "role": "cache", "owner": "search",
                                       "store": "cache", "key": "shared", "action": "rewrite"},
                                      {"id": "route", "role": "endpoint", "owner": "ai", "route": "/read"}])
        self.app = Application()
        self.options = dict(subject=self.subject, case=case, application=self.app, mode="deletion",
                            environment=self.binding["environment"], revision=self.binding["revision"])

    def test_unread_service_lists_owner_and_external_exhibit_is_separate(self):
        self.binding["services"].append({"id": "other", "role": "cache", "owner": "ops",
                                        "store": "other", "key": "shared", "action": "rewrite"})
        self.binding["services"].append({"id": "model", "role": "provider", "owner": "privacy",
                                        "store": "model", "key": "request", "action": "external"})
        folder = self.root / "gap"
        result = discovery.run_case(self.binding, **self.options, output=folder)
        self.assertEqual(result, discovery.replay(folder, expected_binding=self.binding))
        self.assertEqual(discovery.exit_code(result), 3)
        rows = {r["id"]: r for r in result["services"]}
        self.assertEqual((rows["other"]["status"], rows["other"]["owner"], rows["other"]["evidence"]),
                         ("NOT TESTED", "ops", []))
        self.assertIn("No direct check for other", discovery.summary(result))
        self.assertEqual(rows["model"]["status"], "OPEN")
        self.assertEqual(rows["model"]["invocation"], {"status": "UNOBSERVED", "evidence": []})
        self.assertTrue(all(rows["shared-answer"]["evidence"]))
        self.assertTrue(self.app.erased)
        connected = deepcopy(self.binding)
        connected["services"].pop(-2)
        result = discovery.run_case(connected, **{**self.options, "application": Application()},
                                    output=self.root / "connected")
        self.assertEqual((result["technical_verdict"], result["review_status"]), ("PASS", "OPEN_ITEMS"))
        self.assertEqual(discovery.exit_code(result, case_review=True), 3)

    def test_uninspected_whole_key_delete_withholds_mutation(self):
        self.binding["services"].append({"id": "raw-export", "role": "store", "owner": "analysts",
                                        "store": "exports", "key": "mixed", "action": "delete"})
        folder = self.root / "unknown-control"
        result = discovery.run_case(self.binding, **self.options, output=folder)
        self.assertFalse(self.app.erased)
        self.assertEqual(result["technical_verdict"], "NOT TESTED")
        rows = {r["id"]: r for r in result["services"]}
        self.assertEqual(rows["raw-export"]["baseline"], "UNREAD")
        self.assertEqual((rows["shared-answer"]["baseline"], rows["shared-answer"]["status"]),
                         ("CONFIRMED", "NOT TESTED"))
        text = discovery.summary(discovery.replay(folder))
        self.assertIn("Cannot delete exports/mixed", text)
        self.assertIn("BASELINE CONFIRMED  shared-answer", text)
        self.assertIn("NOT TESTED  raw-export", text)

    def test_source_lookup_is_subject_bound_and_keeps_all_descendants_and_shared_inputs(self):
        from setrya.erasure.journal import CopyJournal
        from setrya.erasure.lineage import VersionLineage, VersionRef
        from dataclasses import asdict

        db = VersionLineage(CopyJournal(self.root / "journal.sqlite"), "tenant-a")
        root = VersionRef("source", "main", "7")
        other = VersionRef("source", "second", "3")
        derived = VersionRef("cache", "shared", "1")
        db.record(root, subjects=[self.subject])
        db.record(other, subjects=[self.subject])
        db.record(derived, inputs=[root, other])
        db.record(VersionRef("cache", "outside", "1"), inputs=[other])
        whole = db.affected(self.subject)
        self.assertEqual(len(whole["roots"]), 2)
        self.assertEqual(len(whole["derivations"]), 2)
        scoped = db.affected(self.subject, source=root)
        self.assertEqual(scoped["roots"], [asdict(root)])
        self.assertEqual(scoped["derivations"], [{"artifact": asdict(derived), "inputs": [asdict(root), asdict(other)]}])
        for subject, version in ((self.subject, VersionRef("source", "main", "8")),
                                 (Subject("b" * 64, "restricted"), root)):
            self.assertEqual(db.affected(subject, source=version), {"roots": [], "derivations": []})
        self.app.lineage = lambda subject: db.affected(subject, source=root)
        result = discovery.run_case(self.binding, **self.options, output=self.root / "single-finding")
        self.assertEqual(result["technical_verdict"], "PASS")
        db.record(VersionRef("unlisted", "also-derived", "4"), inputs=[derived])
        other_app = Application()
        other_app.lineage = self.app.lineage
        result = discovery.run_case(self.binding, **{**self.options, "application": other_app},
                                    output=self.root / "unlisted")
        self.assertEqual(result["technical_verdict"], "NOT TESTED")
        self.assertIn("unlisted/also-derived", discovery.summary(result))

    def test_sdk_correlation_requires_one_matching_call_and_source(self):
        source = self.binding["source"]
        self.binding["services"].append({"id": "provider", "role": "provider", "owner": "case-owner",
                                        "store": "provider", "key": "one-call", "action": "external"})
        root = {k: source[k] for k in ("store", "key", "revision")}
        destination = {"store": "provider", "key": "one-call", "revision": "1"}
        edge = {"artifact": destination, "inputs": [root]}
        e = {"lineage": {"roots": [root], "derivations": [edge]}}
        attempted = {"store": "provider", "key": "one-call", "action": "sent", "classification": "restricted",
                     "source": "main", "details": {"evidence_level": "configuration", "provider": "anthropic",
                         "operation": "messages.create", "sources": ["main@7"], "stage": "attempted"}}
        returned = {**attempted, "details": {**attempted["details"], "stage": "returned"}}
        def status(events, version=2):
            proof = discovery._provider_invocations({**e, "journal": events}, self.binding, packet_version=version)
            return proof[("provider", "one-call")]["status"]
        responses = [{**ev, "details": {**ev["details"], "provider": "openai", "operation": "responses.create"}}
                     for ev in (attempted, returned)]
        self.assertEqual(status(responses, version=1), "UNOBSERVED")
        self.assertEqual(status(responses, version=2), "SDK_RETURNED")
        self.assertEqual(status([attempted, returned]), "SDK_RETURNED")
        self.assertEqual(status([attempted, returned, attempted]), "UNOBSERVED")
        self.assertEqual(status([{**attempted, "details": {**attempted["details"], "operation": "langchain.start"}}]),
                         "UNOBSERVED")
        self.assertEqual(status([attempted, {**returned, "source": "another-input"}]), "UNOBSERVED")
        edge["inputs"].append({**root, "store": "second-store"})
        self.assertEqual(status([attempted, returned]), "UNOBSERVED")

    def test_stale_source_and_other_lineage_root_prevent_mutation(self):
        for index, app in enumerate((Application(source="99"), Application(separate_root=True))):
            with self.subTest(index=index):
                folder = self.root / str(index)
                result = discovery.run_case(self.binding, **{**self.options, "application": app}, output=folder)
                self.assertFalse(app.erased)
                self.assertEqual(result["technical_verdict"], "NOT TESTED")
                self.assertEqual(discovery.replay(folder), result)
                self.assertTrue(any("source" in g.lower() for g in result["gaps"]))

    def test_stale_source_cannot_dispatch_a_worker_that_writes_on_release(self):
        class Worker(Application):
            held = False
            released = False

            def hold(self, subject):
                from setrya.erasure.model import Hold
                self.held = True
                return Hold("captured")

            def checkpoint(self, subject):
                return None

            def release_and_drain(self, subject, attempt):
                self.released = True

            def cancel_and_drain(self, subject, attempt):
                raise AssertionError("no worker should have been dispatched")

        for index, app in enumerate((Worker(source="99"), Worker(separate_root=True))):
            folder = self.root / str(index)
            result = discovery.run_case(self.binding, **{**self.options, "mode": "late-write", "application": app},
                                        output=folder)
            self.assertEqual(result["technical_verdict"], "NOT TESTED")
            self.assertFalse(app.erased or app.held or app.released)
            self.assertEqual(discovery.replay(folder), result)

    def test_timed_out_or_abandoned_fixture_worker_never_publishes_during_cleanup(self):
        from threading import Event
        from unittest.mock import patch
        from setrya.erasure.worker_fixture import WorkerFixture

        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                writes = []
                start = Event()

                def delayed(subject, before_write):
                    start.wait()
                    before_write()
                    writes.append(subject.ref)

                with WorkerFixture(delayed, None) as worker:
                    if timeout:
                        with patch.object(worker._ready, "wait", return_value=False):
                            with self.assertRaises(TimeoutError):
                                worker.hold(self.subject)
                        start.set()
                    else:
                        start.set()
                        worker.hold(self.subject)
                self.assertEqual(writes, [])

    def test_source_changed_while_worker_was_held_cancels_its_pending_write(self):
        from setrya.erasure.worker_fixture import WorkerFixture

        writes = []
        class Drift(Application):
            def hold(self, subject):
                result = worker.hold(subject)
                self.source = "new-source-version"
                return result

            def checkpoint(self, subject):
                return worker.checkpoint(subject)

            def release_and_drain(self, subject, attempt):
                return worker.release_and_drain(subject, attempt)

            def cancel_and_drain(self, subject, attempt):
                return worker.cancel_and_drain(subject, attempt)

        def capture(subject, before_write):
            before_write()
            writes.append(subject.ref)

        app = Drift()
        with WorkerFixture(capture, None) as worker:
            result = discovery.run_case(self.binding, **{**self.options, "mode": "late-write", "application": app},
                                        output=self.root / "drift")
        self.assertEqual(result["technical_verdict"], "NOT TESTED")
        self.assertFalse(app.erased)
        self.assertFalse(writes)
        self.assertEqual(result["race"], "incomplete")

    def test_refuse_identity_scope_or_action_substitution_before_any_callbacks(self):
        for name, alt in (("subject", Subject("b" * 64, "restricted")),
                          ("environment", "different"), ("revision", "c" * 40),
                          ("case", replace(self.case, target_question="other?")),
                          ("case", replace(self.case, artifacts=(Artifact("source", "main", "delete"),
                                                                 Artifact("cache", "shared", "delete")))),
                          ("case", replace(self.case, scan_stores=("unmapped-store",)))):
            with self.subTest(name=name, alt=alt):
                with self.assertRaises(InvalidReport):
                    discovery.run_case(self.binding, **{**self.options, name: alt}, output=self.root / "invalid")
        self.assertFalse(self.app.erased)
        self.assertEqual(self.app.reads, 0)
        self.assertFalse((self.root / "invalid").exists())

    def test_fresh_challenge_rejects_substituted_receipt_even_for_the_same_case(self):
        a, b = self.root / "a", self.root / "b"
        first = discovery.run_case(self.binding, **self.options, output=a)
        second = discovery.run_case(self.binding, **{**self.options, "application": Application()}, output=b)
        self.assertNotEqual(first["run"], second["run"])
        self.assertEqual(discovery.replay(a)["technical_verdict"], "PASS")
        other = {**self.binding, "revision": "d" * 40}
        with self.assertRaisesRegex(InvalidReport, "independently supplied"):
            discovery.replay(a, expected_binding=other)
        for file in (a / discovery.DRILL).iterdir():
            (b / discovery.DRILL / file.name).write_bytes(file.read_bytes())
        with self.assertRaisesRegex(InvalidReport, "not produced for this discovery"):
            discovery.replay(b)
        # Recompute after a manifest edit, even with the original passing result still present.
        packet_file = a / discovery.INPUT
        packet = json.loads(packet_file.read_bytes())
        packet["binding"]["case_id"] = "unrelated"
        packet_file.write_text(json.dumps(packet))
        with self.assertRaisesRegex(InvalidReport, "not produced for this discovery"):
            discovery.replay(a)

    def test_atomic_source_revision_refusal_or_missing_acknowledgment_cannot_pass(self):
        for index, ack in enumerate((False, None)):
            app = Application()
            app.erase_approved = lambda *args: ack
            folder = self.root / str(index)
            result = discovery.run_case(self.binding, **{**self.options, "application": app}, output=folder)
            self.assertNotEqual(result["technical_verdict"], "PASS")
            self.assertFalse(app.erased)
            self.assertEqual(discovery.replay(folder), result)
            if ack is False:
                self.assertEqual(result["technical_verdict"], "NOT TESTED")
                self.assertFalse(result["failures"])

    def test_retention_replays_without_any_fixture_and_never_reports_erasure(self):
        self.binding["source"]["action"] = "retain"
        self.binding["services"][0].update(action="retain", evidence="approved-hold", follow_up="2027-03-01")
        folder = self.root / "retained"
        result = discovery.record_retention(self.binding, output=folder)
        self.assertEqual(result["technical_verdict"], "NOT TESTED")
        self.assertEqual(discovery.replay(folder), result)
        self.assertIsNone(result["erasure_receipt"])
        self.assertEqual((result["version"], result["profile"]), (3, "discovery"))
        self.assertTrue(result["open_items"])
        self.assertEqual(result["evidence"]["file"], discovery.INPUT)
        self.assertEqual(result["services"][0]["exhibit"], "approved-hold")
        self.assertEqual(result["services"][0]["status"], "RETAINED")
        self.assertFalse((folder / discovery.DRILL).exists())
        with self.assertRaisesRegex(InvalidReport, "record_retention"):
            discovery.run_case(self.binding, **self.options, output=self.root / "bad")
        self.assertFalse(self.app.erased)

    def test_ambiguous_evidence_cannot_be_replayed_or_returned_to_a_case(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from setrya.cli import main

        folder = self.root / "run"
        result = discovery.run_case(self.binding, **self.options, output=folder)
        self.assertEqual(result["technical_verdict"], "PASS")
        for directory in (folder, folder / discovery.DRILL):
            with self.subTest(directory=directory):
                second = directory / "scenario.json"
                second.write_text("{}")
                export = self.root / "ticket.json"
                bundle = self.root / "signature.json"
                for args in (["replay", folder], ["discovery", "replay", folder],
                             ["discovery", "export", folder, "--output", export],
                             ["discovery", "sign", "--receipt", folder / discovery.RESULT, "--bundle", bundle]):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        self.assertEqual(main(list(map(str, args))), 2)
                self.assertFalse(export.exists())
                self.assertFalse(bundle.exists())
                second.unlink()
        self.assertEqual(discovery.replay(folder), result)

    def test_live_cli_replays_the_exact_run_after_fixture_teardown(self):
        from contextlib import contextmanager, redirect_stderr, redirect_stdout
        from io import StringIO
        import shutil
        from unittest.mock import patch
        from setrya.discovery.cli import main

        previous = self.root / "previous"
        discovery.run_case(self.binding, **self.options, output=previous)
        output = self.root / "case"

        @contextmanager
        def rewinds_fixture(binding):
            settings = {**self.options, "application": Application()}
            settings.pop("mode")
            yield settings
            shutil.rmtree(output)
            shutil.copytree(previous, output)

        fixture = self.root / "fixture.py"
        fixture.touch()
        with (patch("setrya.discovery.cli._binding", return_value=self.binding),
              patch("setrya.discovery.cli.runpy.run_path", return_value={"open_fixture": rewinds_fixture}),
              redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err):
            code = main(["run", "--finding", "finding.json", "--mapping", "mapping.json",
                         "--fixture", str(fixture), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertNotIn("PASS", out.getvalue())
        self.assertIn("no longer matches", err.getvalue())
