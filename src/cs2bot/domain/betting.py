"""Decision, bankroll and execution contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import OutcomeProbability


class PricedOutcome(BaseModel):
    """Model probability joined with a bookmaker price."""

    outcome: OutcomeProbability
    quote: OddsQuote
    effective_odds: Decimal  # odds after stake tax (e.g. 0.88 * odds)
    ev: float  # expected profit per 1 unit staked, after tax, pushes/voids refunded
    kelly_full: float  # full-Kelly fraction of bankroll (can be <= 0)


class BankrollState(BaseModel):
    """Snapshot of money/risk state the staking rules need (computed from the ledger + bets)."""

    balance: Decimal  # cash available at the bookmaker
    bankroll_reference: Decimal  # bankroll used for sizing (balance + open stakes)
    day_start_bankroll: Decimal
    week_start_bankroll: Decimal
    realized_pnl_today: Decimal  # settled P&L since local midnight (negative = loss)
    realized_pnl_week: Decimal  # settled P&L since local Monday 00:00
    open_stakes_today: Decimal  # stakes of unsettled bets placed today
    open_stakes_week: Decimal
    exposure_by_match: dict[int, Decimal] = Field(default_factory=dict)  # match id -> open stakes
    bets_placed_today: int = 0
    kill_switch: bool = False
    paused: bool = False
    pause_reason: str | None = None


class BetDecision(BaseModel):
    priced: PricedOutcome
    match_id: int
    accepted: bool
    reasons: list[str] = Field(default_factory=list)  # why rejected (or "ok" notes)
    stake: Decimal = Decimal("0")
    kelly_fraction_used: float = 0.0
    decided_at: datetime


class PlacementStatus(StrEnum):
    PLACED = "placed"
    DRY_RUN_OK = "dry_run_ok"
    ODDS_MOVED = "odds_moved"
    MARKET_UNAVAILABLE = "market_unavailable"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    LOGIN_REQUIRED = "login_required"
    SELECTOR_ERROR = "selector_error"
    KILL_SWITCH = "kill_switch"
    REJECTED_BY_BOOKMAKER = "rejected_by_bookmaker"
    UNCERTAIN = "uncertain"  # could not confirm whether the bet went through - needs reconciliation
    FAILED = "failed"


class BetOrder(BaseModel):
    """Everything the executor needs to place one bet. Created after the ``bets`` row exists."""

    bet_id: int
    idempotency_key: str
    external_event_id: str
    event_url: str | None = None
    market_external_id: str = ""
    market_raw_name: str
    selection_external_id: str = ""
    selection_raw_name: str
    requested_odds: Decimal
    min_acceptable_odds: Decimal
    stake: Decimal
    dry_run: bool = True


class PlacementResult(BaseModel):
    status: PlacementStatus
    placed_odds: Decimal | None = None
    external_bet_id: str | None = None
    balance_after: Decimal | None = None
    screenshot_path: str | None = None
    message: str = ""
    at: datetime


class ExecutorHealth(BaseModel):
    ok: bool
    logged_in: bool | None = None
    balance: Decimal | None = None
    selectors_ok: dict[str, bool] = Field(default_factory=dict)
    message: str = ""
    checked_at: datetime
