"""A single-thread worker that owns thread-bound resources (Playwright browsers).

Playwright's sync API objects may only be used from the thread that created them. The Betclic executor
(persistent, logged-in browser profile) therefore lives inside one :class:`ThreadWorker`; the scheduler's
betting job and the dashboard's "open Betclic login window" button both *submit* work to it and wait for
the result. Work items run strictly one at a time, which also guarantees the profile is never used
concurrently.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

_STOP = object()


class ThreadWorker[R]:
    """Owns a resource created lazily *inside* the worker thread by ``factory``.

    ``submit(fn)`` schedules ``fn(resource)`` on the worker thread and returns a Future.
    """

    def __init__(
        self,
        factory: Callable[[], R],
        *,
        name: str = "cs2bot-worker",
        closer: Callable[[R], None] | None = None,
    ):
        self._factory = factory
        self._closer = closer
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._resource: R | None = None
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                if self._resource is None:
                    self._resource = self._factory()
                fut.set_result(fn(self._resource))
            except BaseException as exc:
                fut.set_exception(exc)
        if self._resource is not None and self._closer is not None:
            try:
                self._closer(self._resource)
            except Exception:
                log.exception("error while closing worker resource")
        self._resource = None

    def submit(self, fn: Callable[[R], T]) -> Future[T]:
        self.start()
        fut: Future[T] = Future()
        self._queue.put((fn, fut))
        return fut

    def call(self, fn: Callable[[R], T], *, timeout_s: float | None = None) -> T:
        """Submit and wait for the result (re-raises the worker-side exception)."""
        return self.submit(fn).result(timeout=timeout_s)

    def reset(self) -> None:
        """Close and drop the resource (it is re-created on the next submit), e.g. after a browser crash."""

        def _drop(resource: R) -> None:
            if self._closer is not None:
                self._closer(resource)
            self._resource = None

        if self._started:
            self.call(_drop)

    def stop(self, timeout_s: float = 30.0) -> None:
        if self._started:
            self._queue.put(_STOP)
            self._thread.join(timeout=timeout_s)
