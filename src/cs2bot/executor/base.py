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
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from cs2bot.domain.betting import BetOrder, ExecutorHealth, PlacementResult
from cs2bot.utils.time import ensure_utc

HaltCheck = Callable[[], tuple[bool, str | None]]
"""``() -> (halted, reason)``; same shape as :func:`cs2bot.db.state.is_trading_halted` minus the session."""


class BookmakerBetStatus(StrEnum):
    """Status of a bet as shown in Betclic's "Moje zakłady" (bet history)."""

    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"
    CASHOUT = "cashout"
    UNKNOWN = "unknown"


class BookmakerBetRecord(BaseModel):
    """One row of the bookmaker's bet history (best effort, selector-driven scraping).

    Used for reconciliation (``UNCERTAIN`` placements, balance checks) and settlement. Money and odds
    are ``Decimal``; ``placed_at`` is UTC-aware (``None`` when the date could not be parsed).
    ``payout`` is the amount returned to the balance (winnings incl. stake, refund or cash-out);
    ``None`` while the bet is open or when it is not shown.
    """

    model_config = ConfigDict(frozen=True)

    external_bet_id: str | None
    placed_at: datetime | None = None
    event_name: str = ""
    market_name: str = ""
    selection_name: str = ""
    odds: Decimal | None = None
    stake: Decimal | None = None
    status: BookmakerBetStatus = BookmakerBetStatus.UNKNOWN
    payout: Decimal | None = None
    raw_text: str = ""

    @field_validator("placed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("placed_at must be timezone-aware")
        return ensure_utc(value)


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
