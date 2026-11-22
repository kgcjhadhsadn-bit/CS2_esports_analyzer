"""Decision, bankroll and execution contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cs2bot.domain.enums import BetStatus
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.utils.time import ensure_utc


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
    day_start: datetime | None = None  # UTC instant of the local (Warsaw) midnight used above
    week_start: datetime | None = None  # UTC instant of the local Monday 00:00 used above
    reserved_stakes: Decimal = Decimal("0")  # open stakes not yet debited in the ledger
    bets_placed_today: int = 0
    kill_switch: bool = False
    paused: bool = False
    pause_reason: str | None = None


class BetDecision(BaseModel):
    priced: PricedOutcome
    match_id: int
    accepted: bool
    reasons: list[str] = Field(default_factory=list)  # rejection reasons only (empty when accepted)
    notes: list[str] = Field(default_factory=list)  # informational, e.g. which staking cap was binding
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


def bet_status_after_placement(status: PlacementStatus) -> BetStatus:
    """How a placement attempt changes ``bets.status``.

    * ``PLACED`` -> ``BetStatus.PLACED`` (money at risk).
    * ``DRY_RUN_OK`` -> ``BetStatus.DRY_RUN`` (simulated; never touches the ledger or the limits).
    * ``UNCERTAIN`` -> ``BetStatus.PLACING``: we could not confirm whether the bookmaker accepted the bet,
      so it stays open and counts as exposure until reconciliation against the bet history resolves it.
      It is never retried automatically.
    * everything else -> ``BetStatus.FAILED`` (no bet exists at the bookmaker; no money at risk).
    """
    if status is PlacementStatus.PLACED:
        return BetStatus.PLACED
    if status is PlacementStatus.DRY_RUN_OK:
        return BetStatus.DRY_RUN
    if status is PlacementStatus.UNCERTAIN:
        return BetStatus.PLACING
    return BetStatus.FAILED


class BookmakerBetStatus(StrEnum):
    """Status of a bet as shown in the bookmaker's own bet history ("Moje zakłady")."""

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
