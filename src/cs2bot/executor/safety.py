"""Safety helpers: the trading-halt check and human-like pacing.

``db_halt_check`` adapts :func:`cs2bot.db.state.is_trading_halted` (kill switch / pause) to the
``HaltCheck`` callable the executor calls before acting and again right before the final click. It opens
a fresh session per call (so a kill switch flipped in the dashboard is seen immediately) and fails SAFE:
if the database cannot be read, trading counts as halted.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.db.state import is_trading_halted
from cs2bot.executor.base import HaltCheck
from cs2bot.utils.logging import get_logger

log = get_logger(__name__)


def never_halted() -> tuple[bool, str | None]:
    """A ``HaltCheck`` that never halts (tests / manual smoke runs only)."""
    return False, None


def db_halt_check(session_factory: sessionmaker[Session]) -> HaltCheck:
    """``HaltCheck`` reading the kill switch / pause flag from the database (fail safe)."""

    def _check() -> tuple[bool, str | None]:
        try:
            with session_factory() as session:
                return is_trading_halted(session)
        except Exception as exc:
            log.error("halt check failed (%s) - treating trading as halted", type(exc).__name__)
            return True, f"halt check failed ({type(exc).__name__}); treating trading as halted"

    return _check


class HumanPacer:
    """Random pauses between browser actions (``settings.executor.action_delay_min_s .. max_s``)."""

    def __init__(
        self,
        min_s: float,
        max_s: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        typing_delay_ms: tuple[int, int] = (60, 180),
    ) -> None:
        if min_s < 0 or max_s < 0:
            raise ValueError("delays must be >= 0")
        self.min_s = min(min_s, max_s)
        self.max_s = max(min_s, max_s)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._typing = (min(typing_delay_ms), max(typing_delay_ms))

    def pause(self, scale: float = 1.0) -> float:
        """Sleep a random time in ``[min_s, max_s] * scale``; returns the seconds slept."""
        delay = self._rng.uniform(self.min_s, self.max_s) * max(0.0, scale)
        if delay > 0:
            self._sleep(delay)
        return delay

    def sleep(self, seconds: float) -> None:
        """Plain (injectable) sleep, e.g. for the gap between consecutive bets."""
        if seconds > 0:
            self._sleep(seconds)

    def typing_delay_ms(self) -> float:
        """Delay between key presses when typing the stake."""
        return self._rng.uniform(*self._typing)
