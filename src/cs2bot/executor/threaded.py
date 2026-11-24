"""Share one executor between threads (scheduler jobs, FastAPI handlers, CLI).

Playwright's sync API only works on the thread that started the browser and never inside a running
asyncio loop. :class:`ThreadBoundExecutor` owns ONE worker thread; the wrapped executor is created on
that thread by ``factory`` (lazily, on the first call) and every method call is forwarded to it and
executed there, serialised. From async code use ``await asyncio.to_thread(wrapper.place, order)``.

A long ``open_login_window`` (the owner typing an SMS code) blocks queued ``place`` calls until it
returns - intentionally, the bot cannot bet while logged out anyway. ``close()`` ends such a wait first
(``abort_login`` on the wrapped executor, when it has one), so a shutdown is not held up by it.

``place`` always waits for its result (``call_timeout_s`` does not apply): giving up on a placement that
keeps running in the worker would report a failure for a bet that may still be placed. If the wrapped
executor cannot even be created (broken selector file, bad tuning), ``place`` returns a ``FAILED``
result - nothing was clicked - instead of raising; the other methods raise the error. A key that the
optional ``clicked_check`` reports as already clicked (``create_betclic_executor`` reads the placement
journal) is answered with that check's result (``UNCERTAIN``) instead: never "no bet" for a bet that
may exist.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import TypeVar

from cs2bot.domain.betting import BetOrder, ExecutorHealth, PlacementResult, PlacementStatus
from cs2bot.domain.control import LoginAttempt, LoginStatus
from cs2bot.executor.base import (
    EXECUTOR_UNAVAILABLE_PREFIX,
    AccountExecutor,
    BookmakerBetRecord,
    ExecutorClosedError,
)
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import utcnow

log = get_logger(__name__)

T = TypeVar("T")


class ThreadBoundExecutor:
    """Thread-safe :class:`~cs2bot.executor.base.AccountExecutor` proxy running calls on one worker thread."""

    def __init__(
        self,
        factory: Callable[[], AccountExecutor],
        *,
        thread_name: str = "betclic-executor",
        call_timeout_s: float | None = None,
        clicked_check: Callable[[BetOrder], PlacementResult | None] | None = None,
    ) -> None:
        self._factory = factory
        # Consulted only when the wrapped executor cannot be created: a key whose final button was
        # already clicked (placement journal) must be answered UNCERTAIN, never FAILED ("no bet").
        self._clicked_check = clicked_check
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)
        self._inner: AccountExecutor | None = None
        self._closed = False
        self._state_lock = threading.Lock()
        # Seconds to wait for get_balance / health_check / is_logged_in / fetch_bet_history (None = no
        # limit). Never applied to place() (see the module docstring) nor to open_login_window().
        self.call_timeout_s = call_timeout_s

    @property
    def started(self) -> bool:
        """Whether the wrapped executor has been created yet."""
        return self._inner is not None

    def _call(self, fn: Callable[[AccountExecutor], T], *, timeout_s: float | None = None) -> T:
        with self._state_lock:
            if self._closed:
                raise ExecutorClosedError("this ThreadBoundExecutor was closed")

            def run() -> T:
                if self._inner is None:
                    self._inner = self._factory()
                return fn(self._inner)

            future = self._pool.submit(run)
        return future.result(timeout=timeout_s)

    # ------------------------------------------------------------------ forwarded API

    def _ensure_inner(self) -> None:
        """Create the wrapped executor on the worker thread (raises what the factory raises)."""
        self._call(lambda _ex: None)

    def place(self, order: BetOrder) -> PlacementResult:
        try:
            self._ensure_inner()
        except ExecutorClosedError:
            raise
        except Exception as exc:
            # Nothing touched the browser: a definite "not placed" the caller can record as FAILED -
            # unless the key was clicked before (an earlier run): that answer stays UNCERTAIN.
            detail = str(exc).strip().splitlines()[0][:300] if str(exc).strip() else ""
            log.error("the bet executor could not be created: %s: %s", type(exc).__name__, detail)
            if self._clicked_check is not None:
                try:
                    refusal = self._clicked_check(order)
                except Exception as check_exc:  # the check is a safety net; FAILED below is still true
                    log.error("the already-clicked check failed: %s", type(check_exc).__name__)
                    refusal = None
                if refusal is not None:
                    return refusal
            return PlacementResult(
                status=PlacementStatus.FAILED,
                message=(
                    f"{EXECUTOR_UNAVAILABLE_PREFIX}the bet executor could not be created "
                    f"({type(exc).__name__}: {detail})"
                ),
                at=utcnow(),
            )
        return self._call(lambda ex: ex.place(order))

    def get_balance(self) -> Decimal | None:
        return self._call(lambda ex: ex.get_balance(), timeout_s=self.call_timeout_s)

    def health_check(self) -> ExecutorHealth:
        return self._call(lambda ex: ex.health_check(), timeout_s=self.call_timeout_s)

    def is_logged_in(self) -> bool:
        return self._call(lambda ex: ex.is_logged_in(), timeout_s=self.call_timeout_s)

    def open_login_window(self, timeout_s: float = 600.0) -> bool:
        return self._call(lambda ex: ex.open_login_window(timeout_s))

    def fetch_bet_history(self, since: datetime) -> list[BookmakerBetRecord]:
        return self._call(lambda ex: ex.fetch_bet_history(since), timeout_s=self.call_timeout_s)

    def auto_login(self, timeout_s: float | None = None) -> LoginAttempt:
        """Forward the automatic login (cloud autonomy); ``FAILED`` if the wrapped executor has none."""

        def run(ex: AccountExecutor) -> LoginAttempt:
            fn = getattr(ex, "auto_login", None)
            if not callable(fn):
                return LoginAttempt(
                    status=LoginStatus.FAILED,
                    message=f"{type(ex).__name__} has no auto_login",
                    at=utcnow(),
                )
            result = fn(timeout_s)
            assert isinstance(result, LoginAttempt)
            return result

        return self._call(run)

    def close(self) -> None:
        """Close the wrapped executor on its own thread, then stop the worker (idempotent)."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            # A waiting open_login_window would hold the worker (and the shutdown) for up to its
            # timeout: ask it to stop. abort_login is the one method that is safe from this thread.
            abort = getattr(self._inner, "abort_login", None)
            if callable(abort):
                try:
                    abort()
                except Exception:
                    log.exception("could not abort a waiting login window")

            def shutdown() -> None:
                if self._inner is not None:
                    try:
                        self._inner.close()
                    except Exception:
                        log.exception("error while closing the wrapped executor")

            future = self._pool.submit(shutdown)
        try:
            future.result()
        finally:
            self._pool.shutdown(wait=True)

    def __enter__(self) -> ThreadBoundExecutor:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
