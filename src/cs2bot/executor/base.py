"""Executor contracts: protocols, the bookmaker bet-history record and executor errors.

``BetExecutor`` is the minimal interface the integration layer needs to place bets.
``AccountExecutor`` adds the account operations (manual login window, login state, bet history)
that :class:`~cs2bot.executor.betclic.BetclicExecutor`, :class:`~cs2bot.executor.fake.FakeExecutor`
and :class:`~cs2bot.executor.threaded.ThreadBoundExecutor` all implement.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from cs2bot.domain.betting import (
    BetOrder,
    BookmakerBetRecord,
    BookmakerBetStatus,
    ExecutorHealth,
    PlacementResult,
)
from cs2bot.domain.control import LoginAttempt

HaltCheck = Callable[[], tuple[bool, str | None]]
"""``() -> (halted, reason)``; same shape as :func:`cs2bot.db.state.is_trading_halted` minus the session."""


@runtime_checkable
class BetExecutor(Protocol):
    """Places single pre-match bets on the bookmaker (see docs/ARCHITECTURE.md, P9)."""

    def place(self, order: BetOrder) -> PlacementResult: ...

    def get_balance(self) -> Decimal | None: ...

    def health_check(self) -> ExecutorHealth: ...

    def close(self) -> None: ...


@runtime_checkable
class AccountExecutor(BetExecutor, Protocol):
    """A :class:`BetExecutor` that also exposes the account operations used by the CLI/dashboard."""

    def is_logged_in(self) -> bool: ...

    def open_login_window(self, timeout_s: float = 600.0) -> bool: ...

    def fetch_bet_history(self, since: datetime) -> list[BookmakerBetRecord]: ...


@runtime_checkable
class AutoLoginExecutor(Protocol):
    """An executor that can log in by itself with saved credentials (cloud autonomy).

    ``auto_login`` fills AND submits the login form; it stops at an SMS / 2FA step (``TWO_FA_REQUIRED``)
    and never automates a code. Implemented by ``BetclicExecutor``, ``ThreadBoundExecutor`` and
    ``FakeExecutor``.
    """

    def auto_login(self, timeout_s: float | None = None) -> LoginAttempt: ...


class ExecutorError(RuntimeError):
    """Base class of executor errors."""


class ExecutorClosedError(ExecutorError):
    """The executor was closed; create a new one."""


class ExecutorThreadError(ExecutorError):
    """Playwright's sync API is bound to the thread that started the browser."""


class BrowserStartError(ExecutorError):
    """Chromium (persistent profile) could not be started."""


class LoginRequiredError(ExecutorError):
    """The Betclic session expired; the owner must log in again (``open_login_window``)."""


__all__ = [
    "AccountExecutor",
    "AutoLoginExecutor",
    "BetExecutor",
    "BookmakerBetRecord",
    "BookmakerBetStatus",
    "HaltCheck",
]
