"""Adapter authors can import the same suite that runs on the bundled stores and queue."""
from contextlib import chdir
from dataclasses import replace
from importlib.util import find_spec
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from setrya.adapter import Checkpoint, Publication, Receipt
from setrya.cli import EXAMPLE, PGVECTOR
from setrya.conformance import ContractError, assert_queue_contract, assert_store_contract
from setrya.drill import Drill
from setrya.examples.support_search.adapter import SupportAdapter
from setrya.examples.support_search.app import App


def check_fixture(path):
    fixed = Drill.load(path)
    with TemporaryDirectory() as directory:
        out = Path(directory) / "results"
        reports = assert_store_contract(guarded=fixed, broken=replace(fixed, options={"mode": "broken"}),
                                        opaque=replace(fixed, options={"mode": "opaque"}), output=out)
        return set(reports), {p.name for p in out.iterdir()}


class AdapterContractTests(unittest.TestCase):
    def test_adapter_cannot_redirect_saved_conformance_reports_by_changing_directory(self):
        drill = Drill.load(EXAMPLE / "python.toml")
        with TemporaryDirectory() as tmp, chdir(tmp):
            expected = Path.cwd() / "results"
            sibling = Path.cwd() / "app"
            sibling_output = sibling / "results"
            sibling_output.mkdir(parents=True)
            close = SupportAdapter.close

            def close_in_application(adapter):
                close(adapter)
                os.chdir(sibling)

            # The bundled worker starts a separate Python process from the fixture's cwd.
            with (patch.dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
                  patch.object(SupportAdapter, "close", close_in_application)):
                assert_store_contract(guarded=drill, broken=replace(drill, options={"mode": "broken"}),
                                      opaque=replace(drill, options={"mode": "opaque"}), output="results")
            self.assertEqual({p.name for p in expected.iterdir()}, {"broken.json", "guarded.json", "opaque.json"})
            self.assertEqual(list(sibling_output.iterdir()), [])

    def test_local_store(self):
        self.assertEqual(check_fixture(EXAMPLE / "python.toml"),
                         ({"broken", "guarded", "opaque"}, {"broken.json", "guarded.json", "opaque.json"}))

    def test_contract_rejects_an_unrelated_negative_control(self):
        drill = Drill.load(EXAMPLE / "python.toml")
        with self.assertRaisesRegex(ContractError, "opaque:"):
            assert_store_contract(guarded=drill, broken=replace(drill, options={"mode": "broken"}), opaque=drill)

    def test_stale_read_before_release_does_not_qualify_the_broken_worker(self):
        use_cache = App._use_cache

        def stale_while_held(app, terms, state):
            if app.mode == "broken" and app.published and not app.released and "return" in terms:
                app.refund_reads += 1
                return app.refund_reads == 2
            return use_cache(app, terms, state)

        # This fixture goes stale before release and repeats the violation after commit.
        with patch.object(App, "_use_cache", stale_while_held):
            with self.assertRaisesRegex(ContractError, "decisive forbidden response"):
                check_fixture(EXAMPLE / "python.toml")

    def test_missing_queue_identity_is_rejected_before_dispatch(self):
        def unexpected():
            self.fail("invalid expectations reached an application callback")
        args = dict(dispatch=unexpected, checkpoint=unexpected, publish=unexpected,
                    release=unexpected, receipt=unexpected, change=Publication("run", "policy", "B"),
                    old_revision="A", checkpoint_name="after-read", outcome="rejected")
        for invalid in ({"change": None}, {"change": Publication("", "policy", "B")},
                        {"change": Publication("run", "", "B")}, {"change": Publication("run", "policy", "")},
                        {"old_revision": ""}, {"checkpoint_name": ""}):
            with self.subTest(invalid=invalid), self.assertRaises((ContractError, ValueError)):
                assert_queue_contract(**(args | invalid))

    def test_queue_does_not_release_early_or_accept_an_unrelated_receipt(self):
        change = Publication("run", "policy", "B")
        hold = Checkpoint("run", "job", "policy", "A", "after-read")
        for condition in ("early", "different-task"):
            with self.subTest(condition=condition):
                releases = []
                def terminal():
                    if condition == "early" or releases:
                        return Receipt("run", "other", "policy", "A", "rejected")
                    return None
                with self.assertRaises(ContractError):
                    assert_queue_contract(dispatch=lambda: "job", checkpoint=lambda: hold, publish=lambda: change,
                                          release=lambda: releases.append(True), receipt=terminal, change=change,
                                          old_revision="A", checkpoint_name="after-read", outcome="rejected")
                self.assertEqual(bool(releases), condition == "different-task")


@unittest.skipUnless(os.environ.get("SETRYA_PGVECTOR_DSN") and find_spec("psycopg"), "requires disposable pgvector")
class PgvectorContractTests(unittest.TestCase):
    def test_native_pgvector_adapter(self):
        self.assertEqual(check_fixture(PGVECTOR / "drill.toml")[0], {"broken", "guarded", "opaque"})

    @unittest.skipUnless(find_spec("celery") and find_spec("langchain_core"), "requires the answer-cache extra")
    def test_native_celery_handoff(self):
        from setrya.examples.answer_cache.app import Application

        run_id = uuid4().hex
        with TemporaryDirectory() as directory, Application(run_id, "guarded", Path(directory)) as app:
            app.seed()
            assert_queue_contract(dispatch=app.start_old, checkpoint=app.checkpoint, publish=app.revoke,
                                  release=app.release, receipt=app.receipt, change=Publication(run_id, "policy", "revoked"),
                                  old_revision="granted", checkpoint_name="after-retrieval", outcome="rejected", timeout=30)
