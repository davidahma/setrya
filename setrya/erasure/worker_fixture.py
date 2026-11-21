"""Connect a synchronous application job and its serving endpoint to a held-worker drill."""

from concurrent.futures import Future
from math import isfinite
from threading import Event, Thread
from uuid import uuid4


class _Cancelled(Exception):
    """The held job was retired at its pre-write checkpoint."""


class WorkerFixture:
    """Run a disposable job with a real pre-write checkpoint.

    snapshot_job(subject, before_write) calls before_write() after capturing its
    input, immediately before its write. It returns after the write and all
    downstream processing or refreshes that the serving path depends on.
    """

    def __init__(self, snapshot_job, read_chat, *, timeout=10):
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("use a finite, positive worker timeout")
        self.snapshot_job = snapshot_job
        self.read = read_chat
        self.timeout = timeout
        self._ready = Event()
        self._paused = Event()
        self._cancel = Event()
        self._release = Event()
        self._attempt = uuid4().hex
        self._future = Future()
        self._thread = None
        self._subject = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if not self._release.is_set():
            # Teardown owns cleanup, never authorization to publish a held write.
            self._cancel.set()
            self._release.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout)
            if self._thread.is_alive():
                raise TimeoutError("disposable worker did not exit after release")

    def _before_write(self):
        if self._paused.is_set():
            raise RuntimeError("one attempt must reach exactly one pre-write checkpoint")
        self._paused.set()
        self._ready.set()
        if not self._release.wait(self.timeout):
            raise TimeoutError("erasure drill never released the held worker")
        if self._cancel.is_set():
            raise _Cancelled()

    def _run(self, subject):
        try:
            self.snapshot_job(subject, self._before_write)
        except _Cancelled:
            self._future.set_result(None)
        except BaseException as exc:
            self._future.set_exception(exc)
        else:
            self._future.set_result(None)
        finally:
            self._ready.set()

    def hold(self, subject):
        from . import Hold

        if self._thread is not None:
            raise RuntimeError("this fixture has already dispatched a worker")
        self._subject = subject
        self._thread = Thread(target=self._run, args=(subject,), name="setrya-erasure-worker", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.timeout):
            # The caller has no acknowledged attempt it could explicitly retire.
            # A job reaching its checkpoint after this timeout must discard its write.
            self._cancel.set()
            self._release.set()
            raise TimeoutError("worker did not reach its pre-write checkpoint")
        if self._future.done():
            self._future.result()
            raise RuntimeError("worker returned without a live pre-write checkpoint")
        return Hold(self._attempt)

    def checkpoint(self, subject):
        from . import Hold

        if subject == self._subject and self._paused.is_set() and not self._release.is_set() and not self._future.done():
            return Hold(self._attempt)
        return None

    def release_and_drain(self, subject, attempt):
        from . import Drain

        if attempt != self._attempt or self.checkpoint(subject) is None:
            raise RuntimeError("cannot release an unconfirmed worker")
        self._release.set()
        self._future.result(timeout=self.timeout)
        return Drain(attempt, True)

    def cancel_and_drain(self, subject, attempt):
        """Discard the captured write at the checkpoint and await that same worker."""
        from . import Drain

        if attempt != self._attempt or self.checkpoint(subject) is None:
            raise RuntimeError("cannot cancel an unconfirmed worker")
        self._cancel.set()
        self._release.set()
        self._future.result(timeout=self.timeout)
        return Drain(attempt, True)
