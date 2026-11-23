"""One-call construction of the production executor for the integration layer."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings
from cs2bot.executor.betclic import BetclicExecutor
from cs2bot.executor.safety import db_halt_check
from cs2bot.executor.selectors import SelectorSet
from cs2bot.executor.session import PlaywrightFactory
from cs2bot.executor.threaded import ThreadBoundExecutor
from cs2bot.executor.tuning import ExecutorTuning


def create_betclic_executor(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    selectors: SelectorSet | None = None,
    tuning: ExecutorTuning | None = None,
    playwright_factory: PlaywrightFactory | None = None,
) -> ThreadBoundExecutor:
    """A thread-safe Betclic executor whose halt check reads the kill switch / pause flag from the DB.

    The browser starts lazily on the first call (on the wrapper's own worker thread), so creating it is
    cheap; call ``close()`` on shutdown to close Chromium and release the profile lock. The selector file
    is loaded (and validated) on that first call too.

    Timeouts, pacing and the profile-lock ages come from ``settings.executor``
    (:meth:`ExecutorTuning.from_settings`) unless an explicit ``tuning`` is given. Inconsistent values
    raise ``ValueError`` here, at start-up, rather than at the first bet.
    """
    halt_check = db_halt_check(session_factory)
    tuning = tuning if tuning is not None else ExecutorTuning.from_settings(settings)

    def _build() -> BetclicExecutor:
        return BetclicExecutor(
            settings,
            selectors=selectors,
            halt_check=halt_check,
            playwright_factory=playwright_factory,
            tuning=tuning,
        )

    return ThreadBoundExecutor(_build)
