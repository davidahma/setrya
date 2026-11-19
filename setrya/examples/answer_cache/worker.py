"""A live Celery worker with an isolated filesystem transport and one bound cache-fill task."""
from pathlib import Path
import sys

from celery import Celery
from celery.signals import worker_ready

from setrya.integrations.celery import CeleryCheckpoint
from .contract import CHECKPOINT, PRIVATE
from .store import Store

TASK = "setrya_fixture.fill_answer"


def broker(directory, run_id):
    app = Celery("setrya_answer", broker="filesystem://", set_as_current=False)
    spool = str(directory / "messages")
    app.conf.update(
        broker_transport_options={"data_folder_in": spool, "data_folder_out": spool,
                                  "control_folder": str(directory / "control"), "polling_interval": 0.1},
        task_default_queue="answer-" + run_id,
        accept_content=["json"], task_serializer="json", task_ignore_result=True,
        worker_enable_remote_control=False, worker_send_task_events=False,
    )
    return app


def serve(directory, run_id, mode):
    app = broker(directory, run_id)
    store = Store(run_id, mode)

    @app.task(name=TASK, bind=True, ignore_result=True)
    def fill(task, caller, question, pause):
        old = store.capture(caller, question)
        if pause and (not old.granted or PRIVATE not in old.sources):
            raise RuntimeError("worker did not capture the private source")
        store.record_capture(old, task.request.id)
        if pause:
            CeleryCheckpoint(directory / "gate.sqlite").hold(
                task, run_id=run_id, document="policy", revision=old.revision, name=CHECKPOINT, timeout=30)
        store.fill(old, task.request.id)

    @worker_ready.connect(weak=False)
    def ready(**kwargs):
        (directory / "ready").touch()

    return app.worker_main(["worker", "--pool=solo", "--concurrency=1",
                            "--hostname=cache-" + run_id + "@localhost", "--loglevel=WARNING",
                            "--without-gossip", "--without-mingle", "--without-heartbeat"])


if __name__ == "__main__":
    raise SystemExit(serve(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
