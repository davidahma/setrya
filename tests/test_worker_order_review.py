"""A fix must account for the same read while the other delivery is still held."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from setrya.adapter import ReadView
from setrya.checker import Status, evaluate
from setrya.review_scenarios import compare_scenarios
from setrya.scenario_fixture import Application, _Delivery
from setrya.scenarios import inspect_scenario, run_scenario
from setrya.store_signals import Position
from test_scenario_order import SecondDelivery


class WorkerOrderReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_case(self, lane, kind, expected, factory=Application):
        path = self.root / lane
        run_scenario(kind, "forward", path, factory=factory, revision=("a" if lane == "base" else "b") * 40)
        evidence = inspect_scenario(path)
        self.assertIs(evidence.verdict.status, expected, evidence.verdict)
        return evidence

    def review(self, before, after):
        return compare_scenarios(before, after, "a" * 40, "b" * 40,
                                 before.verdict.status.exit_code, after.verdict.status.exit_code)

    def test_both_held_named_version_reproduces_cross_only_failure(self):
        before = self.run_case("base", "update", Status.FAIL, SecondDelivery)
        self.assertTrue(all(evaluate(r).status is Status.PASS for r in before.members["forward"].reports.values()))
        with patch.object(SecondDelivery, "delivery", Application.delivery):
            after = self.run_case("candidate", "update", Status.PASS, SecondDelivery)
        code, text = self.review(before, after)
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("FIX VERIFIED"))
        for words in ("both held", "delivery 0 → delivery 1", "query sha256", "caller support", "named replacement"):
            self.assertIn(words, text)

    def test_absence_before_release_requires_publication_visibility(self):
        before = self.run_case("base", "revoke", Status.FAIL, SecondDelivery)
        read = _Delivery.search

        def native_view(worker, probe):
            result = read(worker, probe)
            return replace(result, view=ReadView("projection", 7, result.route, result.scope))

        with patch.object(SecondDelivery, "delivery", Application.delivery), patch.object(_Delivery, "search", native_view):
            with patch.object(SecondDelivery, "publication_position", lambda app, published: Position("projection", 7),
                              create=True):
                after = self.run_case("candidate", "revoke", Status.PASS, SecondDelivery)
        self.assertIn("view covers the publication", self.review(before, after)[1])
        self.assertEqual(self.review(before, after)[0], 0)
        manifest = self.root / "candidate/scenario.json"
        data = json.loads(manifest.read_text())
        for position in (None, data["publication_write"] | {"generation": "other"},
                         data["publication_write"] | {"commit": 8}):
            with self.subTest(position=position):
                data["publication_write"] = position
                manifest.write_text(json.dumps(data))
                code, text = self.review(before, inspect_scenario(manifest.parent))
                self.assertEqual(code, 3, text)
                self.assertTrue(text.startswith("INCONCLUSIVE"))
                self.assertNotIn("FIX VERIFIED", text)

    def test_committed_first_worker_requires_its_position_on_the_held_reader(self):
        seed, receipt, search = Application.seed, _Delivery.receipt, _Delivery.search

        def broken(app):
            seed(app)
            app.broken = True

        def recorded_commit(worker):
            terminal = receipt(worker)
            if terminal and worker.message == "job-a":
                return replace(terminal, outcome="committed", generation="projection", commit=7)
            return terminal

        with patch.object(Application, "seed", broken):
            before = self.run_case("base", "update", Status.FAIL)
        for lane, generation, sequence, expected in (
            ("candidate", "projection", 7, 0), ("other-generation", "other", 7, 3),
            ("older-snapshot", "projection", 6, 3),
        ):
            with self.subTest(lane=lane):
                def recorded_view(worker, probe):
                    result = search(worker, probe)
                    name, position = ("projection", 7) if worker.message == "job-a" else (generation, sequence)
                    return replace(result, view=ReadView(name, position, result.route, result.scope))

                with patch.object(_Delivery, "receipt", recorded_commit), patch.object(_Delivery, "search", recorded_view):
                    after = self.run_case(lane, "update", Status.PASS)
                code, text = self.review(before, after)
                self.assertEqual(code, expected, text)
                if expected == 0:
                    self.assertIn("view covers delivery 0's committed write", text)
                else:
                    self.assertNotIn("FIX VERIFIED", text)

    def test_confirmed_required_passage_disappears_only_between_releases(self):
        search, receipt = _Delivery.search, _Delivery.receipt

        def terminal(worker):
            ack = receipt(worker)
            if ack and worker.message == "job-a":
                return replace(ack, outcome="committed", generation="index", commit=5)
            return ack

        for name, passages, generation, position, scope, expected in (
            ("base", (), "index", 5, "support", Status.FAIL),
            ("unknown-rank", (None,), "index", 5, "support", Status.NOT_TESTED),
            ("lagging", (), "index", 4, "support", Status.NOT_TESTED),
            ("another-index", (), "other", 5, "support", Status.NOT_TESTED),
            ("another-caller", (), "index", 5, "preview", Status.NOT_TESTED),
        ):
            with self.subTest(name=name):
                def answer(worker, probe):
                    result = search(worker, probe)
                    watermark = ReadView("index", 5, result.route, result.scope)
                    if (worker.message == "job-b" and not worker.resume.is_set()
                            and worker.app.workers[0].resume.is_set() and probe.rules.id == "subject"):
                        watermark = ReadView(generation, position, result.route, scope)
                        return replace(result, passages=passages, scope=scope, view=watermark)
                    return replace(result, view=watermark)

                with patch.object(_Delivery, "receipt", terminal), patch.object(_Delivery, "search", answer):
                    report = self.run_case(name, "update", expected)
                self.assertTrue(all(evaluate(r).status is Status.PASS for r in report.members["forward"].reports.values()))
                if expected is Status.FAIL:
                    before = report
                    self.assertIn("lacked required subject@B", before.verdict.detail)

        after = self.run_case("candidate", "update", Status.PASS)
        code, text = self.review(before, after)
        self.assertEqual(code, 0, text)
        for phrase in ("FIX VERIFIED", "between releases", "rejected its old write"):
            self.assertIn(phrase, text)

    def test_no_interval_read_or_unrelated_caller_cannot_verify_a_transient_fix(self):
        seed = Application.seed

        def broken(app):
            seed(app)
            app.broken = True

        with patch.object(Application, "seed", broken):
            before = self.run_case("base", "update", Status.FAIL)
        after = self.run_case("candidate", "update", Status.PASS)
        self.assertIn("between releases", self.review(before, after)[1])
        self.assertEqual(self.review(before, after)[0], 0)
        directory = self.root / "candidate"
        manifest, delivery = directory / "scenario.json", directory / "delivery-1.json"
        meta, journal = json.loads(manifest.read_text()), json.loads(delivery.read_text())
        first, second = meta["release_starts"]
        for alteration in ("missing", "wrong caller"):
            with self.subTest(alteration=alteration):
                changed = []
                for event in journal["events"]:
                    if event.get("stage") == "freshness" and first < event["ns"] < second:
                        if alteration == "missing":
                            continue
                        event = event | {"scope": "another-caller"}
                    changed.append(event | {"seq": len(changed) + 1})
                delivery.write_text(json.dumps(journal | {"events": changed}))
                meta["deliveries"][1] = sha256(delivery.read_bytes()).hexdigest()
                manifest.write_text(json.dumps(meta))
                replayed = inspect_scenario(directory)
                self.assertIs(replayed.verdict.status, Status.NOT_TESTED)
                self.assertTrue(all(evaluate(r).status is Status.PASS for r in replayed.members["forward"].reports.values()))
                code, text = self.review(before, replayed)
                self.assertEqual(code, 3, text)
                self.assertNotIn("FIX VERIFIED", text)
