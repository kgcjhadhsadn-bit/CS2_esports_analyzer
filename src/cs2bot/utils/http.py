"""HTTP helpers: polite rate limiting, retries for transient failures, client construction."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

DEFAULT_USER_AGENT = "cs2bot/0.1 (+personal research project)"


class RateLimiter:
    """Thread-safe limiter enforcing a minimum (optionally jittered) gap between calls.

    ``clock`` / ``sleep`` / ``rng`` are injectable so tests run instantly and deterministically.
    """

    def __init__(
        self,
        min_interval_s: float,
        max_interval_s: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")
        self.min_interval_s = min_interval_s
        self.max_interval_s = max(max_interval_s or min_interval_s, min_interval_s)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._next_allowed: float | None = None

    @classmethod
    def per_hour(cls, max_requests: int) -> RateLimiter:
        return cls(3600.0 / max(1, max_requests))

    def wait(self) -> float:
        """Block until the next call is allowed; returns seconds slept."""
        with self._lock:
            now = self._clock()
            sleep_for = 0.0 if self._next_allowed is None else max(0.0, self._next_allowed - now)
            gap = self._rng.uniform(self.min_interval_s, self.max_interval_s)
            self._next_allowed = now + sleep_for + gap
        if sleep_for > 0:
            self._sleep(sleep_for)
        return sleep_for


def is_transient_http_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 425, 429, 500, 502, 503, 504)
    return False


def with_retries[T](fn: Callable[[], T], *, attempts: int = 4, max_wait_s: float = 60.0) -> T:
    """Call ``fn`` retrying transient HTTP errors with exponential backoff + jitter."""

    @retry(
        retry=retry_if_exception(is_transient_http_error),
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1.0, max=max_wait_s),
        reraise=True,
    )
    def _call() -> T:
        return fn()

    return _call()


def build_http_client(
    *,
    base_url: str = "",
    timeout_s: float = 30.0,
    proxy: str | None = None,
    user_agent: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    hdrs = {"User-Agent": user_agent or DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.8,pl;q=0.6"}
    if headers:
        hdrs.update(headers)
    return httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_s),
        headers=hdrs,
        proxy=proxy,
        follow_redirects=True,
    )
