"""A synthetic answer endpoint whose response contains only the answer text."""

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from secrets import token_hex
from threading import Thread

from celery import Celery
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from redis import Redis

from setrya.adapter import Publication, Receipt
from setrya.integrations.celery_langchain import RedisCeleryCheckpoint, record_served
from setrya.integrations.langchain import tag_documents


URL = os.environ["SETRYA_REDIS_URL"]
QUEUE = os.environ["SETRYA_FRAMEWORK_QUEUE"]
worker = Celery("setrya-framework", broker=URL, set_as_current=False)
worker.conf.update(task_default_queue=QUEUE, task_ignore_result=True, accept_content=["json"],
                   task_serializer="json", broker_connection_retry_on_startup=True)
PRIVATE = tag_documents([Document(page_content="The staff refund window is 45 days.")],
                        document="internal-policy", revision="1")[0]
PUBLIC = tag_documents([Document(page_content="The public refund window is 14 days.")],
                       document="public-policy", revision="1")[0]
TASK_NAME = "setrya.tests.framework.fill_answer"


class PolicyRetriever(BaseRetriever):
    access_revision: str

    def _get_relevant_documents(self, query):
        if query != "What is the refund window?":
            return []
        return [PRIVATE] if self.access_revision == "granted" else [PUBLIC]


@worker.task(bind=True, name=TASK_NAME, shared=False)
def fill_answer(task, caller, question):
    with Redis.from_url(URL, decode_responses=True) as db:
        gate = RedisCeleryCheckpoint.for_task(db, task)
        key = f"setrya-tests:policy:{gate.run_id}"
        revision = db.hget(key, "revision")
        documents = PolicyRetriever(access_revision=revision).invoke(question)
        doc = documents[0]
        gate.hold(document="policy", revision=revision, name="after-retrieval", caller=caller,
                  question=question, answer=doc.page_content, documents=documents)
        # Fence and write at one application boundary, then report its real decision.
        commit = db.eval("""
            if redis.call('HGET', KEYS[1], 'mode') ~= 'broken' and
               redis.call('HGET', KEYS[1], 'revision') ~= ARGV[1] then return 0 end
            redis.call('HSET', KEYS[1], ARGV[2], ARGV[3])
            return redis.call('HINCRBY', KEYS[1], 'position', 1)
        """, 1, key, revision, caller, json.dumps({
            "text": doc.page_content, "sources": [doc.metadata["setrya_marker"]], "writer": task.request.id}))
        outcome = "committed" if commit else "rejected"
        gate.finish(Receipt(gate.run_id, task.request.id, "policy", revision, outcome,
                            gate.run_id if commit else None, commit or None))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        service = self.server.app
        caller = service.sessions.get(self.headers.get("Authorization"))
        if not caller or self.path != "/answers":
            self.send_error(403)
            return
        question = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["question"]
        raw, position = service.test_redis.hmget(service.key, caller, "position")
        answer = (json.loads(raw) if raw else
                  {"text": PUBLIC.page_content, "sources": [PUBLIC.metadata["setrya_marker"]], "writer": None})
        ticket = self.headers.get("X-Setrya-Request")
        if ticket and service.mode != "unobserved":
            record_served(service.test_redis, ticket, caller=caller, route="policy-answer", question=question,
                          answer=answer["text"], sources=answer["sources"], cache_hit=raw is not None,
                          cache_writer=answer["writer"], generation=service.run_id, commit=int(position))
        body = json.dumps({"text": answer["text"]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Application:
    def __init__(self, run_id):
        self.run_id = run_id
        self.mode = os.environ["SETRYA_FRAMEWORK_TEST_MODE"]
        self.key = f"setrya-tests:policy:{run_id}"
        self.test_redis = Redis.from_url(URL, decode_responses=True)
        self.fill_answer = fill_answer
        self.sessions = {"Bearer " + token_hex(16): user for user in ("case-reader", "case-editor")}

    def __enter__(self):
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.app = self
        self.thread = Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()
        self.test_redis.delete(self.key)
        self.test_redis.close()

    def seed(self):
        entry = json.dumps({"text": PRIVATE.page_content, "sources": [PRIVATE.metadata["setrya_marker"]], "writer": "seed"})
        self.test_redis.hset(self.key, mapping={"revision": "granted", "position": 1, "mode": self.mode,
                                                "case-reader": entry, "case-editor": entry})
        self.test_redis.expire(self.key, 180)

    def revoke(self):
        with self.test_redis.pipeline() as pipe:
            pipe.hset(self.key, "revision", "revoked")
            pipe.hdel(self.key, "case-reader")
            pipe.execute()
        return Publication(self.run_id, "policy", self.test_redis.hget(self.key, "revision"))

    def send_answer(self, caller, question, request_id):
        token = next(k for k, user in self.sessions.items() if user == caller)
        conn = HTTPConnection(*self.http.server_address, timeout=3)
        try:
            conn.request("POST", "/answers", json.dumps({"question": question}), {
                "Authorization": token, "X-Setrya-Request": request_id, "Content-Type": "application/json"})
            response = conn.getresponse()
            payload = json.load(response)
            assert set(payload) == {"text"}
            return response.status, payload["text"]
        finally:
            conn.close()
