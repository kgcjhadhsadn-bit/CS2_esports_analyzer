"""Bankroll view: balance, exposure, loss limits, equity curve and statistics.

Two populations are supported: real-money bets (``dry_run = false``; balance from the ledger) and
paper bets (``dry_run = true``; balance = effective starting bankroll + realized paper P&L).
Loss limits use the *effective* bankroll settings (``.env`` defaults + dashboard overrides) and
local (``settings.timezone``) day / week boundaries, like the staking engine.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.api.analytics import (
    EXCLUDED_STATUSES,
    MONEY_OUT_STATUSES,
    ZERO,
    BetFact,
    bet_stats,
    downsample,
    max_drawdown,
    pnl_by_period,
    settled_in_order,
)
from cs2bot.api.formatting import market_type_of, money_str0, odds_str, round_or_none, to_decimal, utc
from cs2bot.api.schemas import (
    BankrollOut,
    BankrollStats,
    EquityPoint,
    ExposureOut,
    LossLimitOut,
    PeriodPnl,
    PnlPoint,
)
from cs2bot.config import BankrollSettings, Settings
from cs2bot.db import state
from cs2bot.db.models import BankrollLedger, Bet, BetDecisionRow
from cs2bot.domain.enums import LedgerKind
from cs2bot.utils.logging import get_logger
from cs2bot.utils.money import D, quantize_money
from cs2bot.utils.time import ensure_utc, local_day_start, local_week_start

log = get_logger(__name__)


def effective_bankroll(session: Session, settings: Settings) -> BankrollSettings:
    """Effective bankroll settings; falls back to the defaults if stored overrides are invalid."""
    try:
        return state.effective_bankroll_settings(session, settings)
    except (ValidationError, ValueError, TypeError):
        log.warning("stored bankroll overrides are invalid - using the defaults")
        return settings.bankroll


def load_bet_facts(session: Session, *, dry_run: bool | None) -> list[BetFact]:
    """All bets of a population (``None`` = both) with the decision's EV / probability."""
    stmt = select(Bet, BetDecisionRow.ev, BetDecisionRow.p_win).outerjoin(
        BetDecisionRow, Bet.decision_id == BetDecisionRow.id
    )
    if dry_run is not None:
        stmt = stmt.where(Bet.dry_run.is_(dry_run))
    out: list[BetFact] = []
    for bet, ev, p_win in session.execute(stmt):
        out.append(
            BetFact(
                id=bet.id,
                match_id=bet.match_id,
                market_type=market_type_of(bet.spec_key),
                status=bet.status,
                dry_run=bool(bet.dry_run),
                stake=to_decimal(bet.stake) or ZERO,
                pnl=to_decimal(bet.pnl),
                odds=to_decimal(bet.placed_odds) or to_decimal(bet.requested_odds),
                clv=bet.clv,
                ev=float(ev) if ev is not None else None,
                p_win=float(p_win) if p_win is not None else None,
                placed_at=utc(bet.placed_at),
                settled_at=utc(bet.settled_at),
                created_at=utc(bet.created_at),
            )
        )
    return out


class LedgerView:
    """Ledger rows in booking order with point-in-time balance lookups."""

    def __init__(self, rows: Sequence[BankrollLedger]) -> None:
        self.rows = sorted(rows, key=lambda r: (ensure_utc(r.ts), r.id))

    @classmethod
    def load(cls, session: Session) -> LedgerView:
        return cls(list(session.scalars(select(BankrollLedger))))

    def current(self) -> Decimal | None:
        return to_decimal(self.rows[-1].balance_after) if self.rows else None

    def balance_at(self, t: datetime) -> Decimal | None:
        """Balance just before instant ``t`` (``None`` without ledger rows)."""
        if not self.rows:
            return None
        before = [r for r in self.rows if ensure_utc(r.ts) < t]
        if before:
            return to_decimal(before[-1].balance_after) or ZERO
        first = self.rows[0]
        return (to_decimal(first.balance_after) or ZERO) - (to_decimal(first.amount) or ZERO)

    def deposits(self) -> Decimal:
        return sum((to_decimal(r.amount) or ZERO for r in self.rows if r.kind is LedgerKind.DEPOSIT), ZERO)


def _open_money_at(facts: Sequence[BetFact], t: datetime) -> Decimal:
    """Stakes that had left the balance and were not yet settled at instant ``t``."""
    total = ZERO
    for f in facts:
        if f.status not in MONEY_OUT_STATUSES or f.placed_at is None or f.placed_at >= t:
            continue
        if f.settled_at is None or f.settled_at >= t:
            total += f.stake
    return total


def _realized_between(facts: Sequence[BetFact], start: datetime | None, end: datetime | None) -> Decimal:
    total = ZERO
    for f in facts:
        if not f.is_settled or f.settled_at is None:
            continue
        if start is not None and f.settled_at < start:
            continue
        if end is not None and f.settled_at >= end:
            continue
        total += f.realized_pnl
    return total


def _limit(
    period: Literal["day", "week"],
    start: datetime,
    fraction: float,
    start_bankroll: Decimal,
    facts: Sequence[BetFact],
) -> LossLimitOut:
    realized = _realized_between(facts, start, None)
    open_in_period = sum(
        (f.stake for f in facts if f.is_open and f.opened_at is not None and f.opened_at >= start), ZERO
    )
    limit_amount = quantize_money(start_bankroll * D(fraction)) if start_bankroll > 0 else ZERO
    loss = max(ZERO, -realized)
    reached = realized < 0 and loss >= limit_amount
    return LossLimitOut(
        period=period,
        period_start=start,
        start_bankroll=money_str0(start_bankroll),
        limit_fraction=fraction,
        limit_amount=money_str0(limit_amount),
        realized_pnl=money_str0(realized),
        open_stakes=money_str0(open_in_period),
        worst_case_pnl=money_str0(realized - open_in_period),
        remaining=money_str0(max(ZERO, limit_amount - loss)),
        used_fraction=round(float(loss / limit_amount), 6) if limit_amount > 0 else (1.0 if reached else 0.0),
        reached=reached,
    )


def _period_rows(facts: Sequence[BetFact], tz: str, granularity: str) -> list[PeriodPnl]:
    return [
        PeriodPnl(
            period=row.period,
            start=row.start,
            bets=row.stats.settled_bets,
            won=row.stats.won,
            lost=row.stats.lost,
            void=row.stats.void,
            staked=money_str0(row.stats.turnover),
            pnl=money_str0(row.stats.realized_pnl),
            yield_rate=round_or_none(row.stats.yield_rate),
        )
        for row in pnl_by_period(facts, tz, granularity)
    ]


def compute_bankroll(
    session: Session, settings: Settings, *, dry_run: bool, now: datetime, max_points: int = 2000
) -> BankrollOut:
    bk = effective_bankroll(session, settings)
    tz = settings.timezone
    facts = [f for f in load_bet_facts(session, dry_run=dry_run) if f.status not in EXCLUDED_STATUSES]
    stats = bet_stats(facts)
    ledger = LedgerView.load(session) if not dry_run else LedgerView([])
    starting = to_decimal(bk.starting_bankroll) or ZERO

    # Stakes of open bets that already left the balance (paper bets: every open bet). Pending
    # real bets have not been sent yet, so they are exposure but still part of the balance.
    in_play = sum((f.stake for f in facts if f.is_open and (dry_run or f.status in MONEY_OUT_STATUSES)), ZERO)
    balance_source: Literal["ledger", "starting_bankroll", "paper"]
    if dry_run:
        balance = starting + stats.realized_pnl - in_play
        balance_source = "paper"
    elif ledger.rows:
        balance = ledger.current() or ZERO
        balance_source = "ledger"
    else:
        balance = starting + stats.realized_pnl - in_play
        balance_source = "starting_bankroll"

    def bankroll_at(t: datetime) -> Decimal:
        if ledger.rows:
            return (ledger.balance_at(t) or ZERO) + _open_money_at(facts, t)
        return starting + _realized_between(facts, None, t)

    day_start = local_day_start(now, tz)
    week_start = local_week_start(now, tz)
    daily = _limit("day", day_start, bk.daily_loss_limit_fraction, bankroll_at(day_start), facts)
    weekly = _limit("week", week_start, bk.weekly_loss_limit_fraction, bankroll_at(week_start), facts)

    capital_base = ledger.deposits() if ledger.rows else ZERO
    if capital_base <= 0:
        capital_base = starting
    settled = settled_in_order(facts)
    dd_abs, dd_pct = max_drawdown((f.realized_pnl for f in settled), capital_base)

    exposure: dict[int | None, list[BetFact]] = defaultdict(list)
    for f in facts:
        if f.is_open:
            exposure[f.match_id].append(f)

    pnl_curve: list[PnlPoint] = []
    cumulative = ZERO
    for f in settled:
        if f.settled_at is None:
            continue
        cumulative += f.realized_pnl
        pnl_curve.append(PnlPoint(ts=f.settled_at, cumulative_pnl=money_str0(cumulative)))

    equity: list[EquityPoint]
    if ledger.rows:
        equity = [
            EquityPoint(
                ts=ensure_utc(r.ts),
                balance=money_str0(r.balance_after),
                kind=r.kind.value,
                amount=money_str0(r.amount),
            )
            for r in ledger.rows
        ]
    else:
        equity = []
        first_seen = min((f.opened_at for f in facts if f.opened_at is not None), default=None)
        if first_seen is not None:
            equity.append(EquityPoint(ts=first_seen, balance=money_str0(starting), kind="start"))
        running = starting
        for f in settled:
            if f.settled_at is None:
                continue
            running += f.realized_pnl
            equity.append(
                EquityPoint(
                    ts=f.settled_at,
                    balance=money_str0(running),
                    kind="settlement",
                    amount=money_str0(f.realized_pnl),
                )
            )

    halted, halt_reason = state.is_trading_halted(session)
    return BankrollOut(
        mode="dry_run" if dry_run else "real",
        currency=bk.currency,
        as_of=now,
        balance=money_str0(balance),
        balance_source=balance_source,
        open_exposure=money_str0(stats.open_stakes),
        open_bets=stats.open_bets,
        bankroll_reference=money_str0(balance + in_play),
        exposure_by_match=[
            ExposureOut(match_id=mid, stake=money_str0(sum((f.stake for f in rows), ZERO)), bets=len(rows))
            for mid, rows in sorted(exposure.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ],
        daily=daily,
        weekly=weekly,
        trading_halted=halted,
        halt_reason=halt_reason,
        stats=BankrollStats(
            bets=stats.bets,
            open_bets=stats.open_bets,
            settled_bets=stats.settled_bets,
            won=stats.won,
            lost=stats.lost,
            void=stats.void,
            turnover=money_str0(stats.turnover),
            realized_pnl=money_str0(stats.realized_pnl),
            capital_base=money_str0(capital_base),
            roi=round_or_none(float(stats.realized_pnl / capital_base)) if capital_base > 0 else None,
            yield_rate=round_or_none(stats.yield_rate),
            hit_rate=round_or_none(stats.hit_rate),
            avg_odds=odds_str(stats.avg_odds),
            avg_clv=round_or_none(stats.avg_clv),
            clv_bets=stats.clv_bets,
            max_drawdown=money_str0(dd_abs),
            max_drawdown_pct=round_or_none(dd_pct),
        ),
        equity_curve=downsample(equity, max_points),
        pnl_curve=downsample(pnl_curve, max_points),
        pnl_by_day=_period_rows(facts, tz, "day"),
        pnl_by_week=_period_rows(facts, tz, "week"),
        pnl_by_month=_period_rows(facts, tz, "month"),
        settings=bk.model_dump(mode="json"),
    )
