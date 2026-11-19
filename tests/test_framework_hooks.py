from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from uuid import uuid4

from setrya.adapter import Checkpoint
from setrya.drill import InputError
from setrya.integrations.celery import CeleryCheckpoint
from setrya.integrations.langchain import observe_documents, retrieve_with_evidence, tag_documents
from setrya.store_signals import passage_metadata


@skipUnless(find_spec("langchain_core"), "requires the answer-cache extra")
class LangChainTests(TestCase):
    def test_ingestion_keeps_application_fields_and_retrieval_preserves_unknown_ranks(self):
        from langchain_core.documents import Document
        from langchain_core.retrievers import BaseRetriever

        chunks = [Document(page_content="answer", id=str(i), metadata={"page": i}) for i in (1, 2)]
        first, second = tag_documents(chunks, document="policy", revision="r1")
        self.assertEqual(chunks[0].metadata, {"page": 1})
        self.assertEqual(first.metadata, {"page": 1, **passage_metadata("policy", "r1", "0")})
        self.assertEqual((first.page_content, first.id), ("answer", "1"))
        inconsistent = first.model_copy(update={"metadata": {**first.metadata, "revision": "r2"}})
        calls = []

        class Retriever(BaseRetriever):
            def _get_relevant_documents(self, query, *, flag=None):
                calls.append((query, flag))
                return [second, inconsistent, chunks[0], first]

        docs, observation = retrieve_with_evidence(Retriever(), "question", route="answers", scope="reader", flag="value")
        self.assertEqual(calls, [("question", "value")])
        self.assertEqual(docs, [second, inconsistent, chunks[0], first])
        self.assertEqual(observation.passages, (second.metadata["setrya_marker"], None, None, first.metadata["setrya_marker"]))
        self.assertEqual((observation.route, observation.scope, observation.view), ("answers", "reader", None))
        self.assertNotIn("page_content", repr(observation))
        self.assertEqual(observe_documents([], route="answers", scope="reader").passages, ())

    def test_adopter_chunk_keys_and_conflicts(self):
        from langchain_core.documents import Document

        source = Document(page_content="visible", id="custom-id")
        docs = tag_documents([source], document="policy", revision="A", chunk_id=lambda doc, _: doc.id)
        self.assertEqual(docs[0].metadata, passage_metadata("policy", "A", "custom-id"))
        with self.assertRaisesRegex(InputError, "distinct ID"):
            tag_documents([source, source], document="policy", revision="A", chunk_id=lambda doc, _: doc.id)
        with self.assertRaisesRegex(InputError, "conflicts"):
            tag_documents(docs, document="policy", revision="B")


class CeleryCheckpointTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "gate.sqlite"
        self.gate = CeleryCheckpoint.create(self.path, run_id="run", task_id="chosen-task")

    def hold(self, timeout=3, run_id="run", **request_fields):
        request = dict(id="chosen-task", retries=0, delivery_info={}, is_eager=False, called_directly=False)
        task = SimpleNamespace(request=SimpleNamespace(**(request | request_fields)))
        CeleryCheckpoint(self.path).hold(task, run_id=run_id, document="policy", revision="A",
                                         name="after-read", timeout=timeout)

    def test_early_wrong_and_expired_deliveries_cannot_claim_a_checkpoint(self):
        with self.assertRaises(FileExistsError):
            CeleryCheckpoint.create(self.path, run_id="other", task_id="other-task")
        with self.assertRaises(InputError):
            self.gate.release()
        for args in ({"id": "other-task"}, {"run_id": "other"}, {"is_eager": True}, {"called_directly": True},
                     {"retries": 1}, {"delivery_info": {"redelivered": True}}, {"timeout": float("nan")}):
            with self.subTest(args=args), self.assertRaises(InputError):
                self.hold(**args)
            self.assertIsNone(self.gate.checkpoint())
        with self.assertRaises(TimeoutError):
            self.hold(timeout=0.02)
        self.assertIsNone(self.gate.checkpoint())
        with self.assertRaises(InputError):
            self.gate.release()
        with self.assertRaises(InputError):
            self.hold()

    def test_release_matches_live_identity_once(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.hold)
            until = monotonic() + 2
            while not self.gate.checkpoint() and monotonic() < until:
                sleep(0.01)
            self.assertEqual(self.gate.checkpoint(), Checkpoint("run", "chosen-task", "policy", "A", "after-read"))
            self.assertFalse(worker.done())
            self.gate.release()
            self.assertIsNone(worker.result(timeout=2))
        self.assertIsNone(self.gate.checkpoint())
        with self.assertRaises(InputError):
            self.gate.release()

    @skipUnless(find_spec("celery"), "requires the answer-cache extra")
    def test_bound_task_reaches_hook_through_real_celery_worker(self):
        from celery import Celery
        from celery.contrib.testing.worker import start_worker

        queue = "setrya-" + uuid4().hex
        app = Celery("setrya-hook-test", broker="memory://", backend="cache+memory://", set_as_current=False)
        self.addCleanup(app.close)
        app.conf.task_default_queue = queue

        @app.task(bind=True, name=queue + ".capture", shared=False)
        def capture(task, path):
            CeleryCheckpoint(path).hold(task, run_id="run", document="policy", revision="A", name="after-read", timeout=10)
            return "released"

        with start_worker(app, pool="solo", perform_ping_check=False):
            job = capture.apply_async(args=[str(self.path)], task_id="chosen-task")
            until = monotonic() + 8
            while not self.gate.checkpoint() and monotonic() < until:
                sleep(0.01)
            self.assertEqual(self.gate.checkpoint(), Checkpoint("run", job.id, "policy", "A", "after-read"))
            self.assertFalse(job.ready())
            self.gate.release()
            self.assertEqual(job.get(timeout=5), "released")
