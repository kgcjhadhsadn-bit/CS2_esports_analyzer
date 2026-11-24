"""One-call construction of the production executor for the integration layer."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings
from cs2bot.domain.betting import BetOrder, PlacementResult, PlacementStatus
from cs2bot.executor.betclic import BetclicExecutor
from cs2bot.executor.journal import JOURNAL_FILENAME, JournalError, PlacementJournal
from cs2bot.executor.safety import db_halt_check
from cs2bot.executor.selectors import SelectorSet
from cs2bot.executor.session import PlaywrightFactory
from cs2bot.executor.threaded import ThreadBoundExecutor
from cs2bot.executor.tuning import ExecutorTuning
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import utcnow


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

    If the executor cannot be created (e.g. a broken selector file), ``place`` answers ``FAILED`` -
    except for a key whose final button the placement journal shows as clicked: ``UNCERTAIN``.
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

    return ThreadBoundExecutor(_build, clicked_check=journal_clicked_check(settings, tuning))


def journal_clicked_check(
    settings: Settings, tuning: ExecutorTuning
) -> Callable[[BetOrder], PlacementResult | None]:
    """``order -> UNCERTAIN result | None`` from the placement journal of the Betclic browser profile.

    Used by :class:`ThreadBoundExecutor` when the real executor cannot be created: the same refusal
    :class:`BetclicExecutor` gives first thing in ``place`` for a key that was clicked with a ``PLACED``
    / ``UNCERTAIN`` outcome (or right before a crash). Dry runs never click and are never refused;
    ``None`` also when the journal cannot be read (the caller's ``FAILED`` stands, as in the executor).
    Read-only: it never writes or compacts the journal.
    """
    path = resolve_path(settings.betclic.browser_profile_dir) / JOURNAL_FILENAME

    def _check(order: BetOrder) -> PlacementResult | None:
        if order.dry_run or settings.executor.dry_run:
            return None
        journal = PlacementJournal(path, retention_days=tuning.journal_retention_days)
        try:
            entry = journal.blocking_entry(order.idempotency_key)
        except JournalError:
            return None
        if entry is None:
            return None
        outcome = entry.status or "unknown - the bot stopped right after the click"
        return PlacementResult(
            status=PlacementStatus.UNCERTAIN,
            message=(
                "refusing to place twice: the place button was already clicked for idempotency key "
                f"{order.idempotency_key!r} (journal: clicked at {entry.at:%Y-%m-%d %H:%M:%S}Z, result "
                f"{outcome}); the executor could not be started either - reconcile via bet history"
            ),
            at=utcnow(),
        )

    return _check
