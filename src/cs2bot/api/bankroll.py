"""Bankroll view: balance, exposure, loss limits, equity curve and statistics.

Two populations are supported: real-money bets (balance from the ledger) and paper bets
(``dry_run`` rows; balance = effective starting bankroll + realized paper P&L).

The numbers mirror ``cs2bot.engine.bankroll`` (which the API must not import) so the dashboard shows
what the staking engine actually enforces:

* ``balance`` = ``balance_after`` of the most recently *written* ledger row (highest id - back-dated
  entries never change the running balance);
* ``bankroll_reference`` = balance + stakes of open bets already debited in the ledger (cash plus
  money at risk); ``available_balance`` = balance - stakes of open bets not debited yet (pending /
  unconfirmed placements);
* period-start bankroll = ``bankroll_reference - realized P&L of the period`` (deposits, withdrawals
  and corrections made during the period count as if made at its start), with local
  (``settings.timezone``) day / week boundaries;
* loss limit = ``fraction x period-start bankroll`` rounded DOWN to the grosz; it is *reached* when
  the realized loss of the period is at least the limit; ``stake_allowance`` = limit - (realized loss
  + ALL open stakes) is what the engine's daily / weekly guard leaves for new stakes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from cs2bot.api.analytics import (
    EXCLUDED_STATUSES,
    MONEY_OUT_STATUSES,
    SETTLED_STATUSES,
    ZERO,
    BetFact,
    bet_stats,
    downsample,
    effective_status,
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
from cs2bot.domain.enums import BetStatus, LedgerKind
from cs2bot.utils.logging import get_logger
from cs2bot.utils.money import CENT, D
from cs2bot.utils.time import ensure_utc, local_day_start, local_week_start

log = get_logger(__name__)


def effective_bankroll(session: Session, settings: Settings) -> BankrollSettings:
    """Effective bankroll settings; falls back to the defaults if stored overrides are invalid."""
    try:
        return state.effective_bankroll_settings(session, settings)
    except (ValidationError, ValueError, TypeError):
        log.warning("stored bankroll overrides are invalid - using the defaults")
        return settings.bankroll


def paper_clause() -> ColumnElement[bool]:
    """SQL condition "this is a paper bet" (``dry_run`` flag or status, like the engine's ``is_dry_run``)."""
    return or_(Bet.dry_run.is_(True), Bet.status == BetStatus.DRY_RUN)


def load_bet_facts(session: Session, *, dry_run: bool | None) -> list[BetFact]:
    """All bets of a population (``None`` = both) with the decision's EV / probability.

    A settled bet without ``settled_at`` (an integration bug) is dated by ``updated_at``, like the
    engine does for the loss limits, instead of silently dropping out of the P&L.
    """
    stmt = select(Bet, BetDecisionRow.ev, BetDecisionRow.p_win).outerjoin(
        BetDecisionRow, Bet.decision_id == BetDecisionRow.id
    )
    if dry_run is not None:
        stmt = stmt.where(paper_clause() if dry_run else ~paper_clause())
    out: list[BetFact] = []
    for bet, ev, p_win in session.execute(stmt):
        pnl = to_decimal(bet.pnl)
        settled_at = utc(bet.settled_at)
        status = effective_status(
            bet.status, extra=bet.extra, pnl=pnl, settled_at=settled_at, dry_run=bool(bet.dry_run)
        )
        placed_at, created_at = utc(bet.placed_at), utc(bet.created_at)
        stake = to_decimal(bet.stake) or ZERO
        if settled_at is None and status in SETTLED_STATUSES:
            settled_at = utc(bet.updated_at) or placed_at or created_at
        if pnl is None and status is BetStatus.WON:
            # like the engine's loss-limit code: a won bet without pnl is worth payout - stake
            payout = to_decimal(bet.payout)
            if payout is not None:
                pnl = payout - stake
        out.append(
            BetFact(
                id=bet.id,
                match_id=bet.match_id,
                market_type=market_type_of(bet.spec_key),
                status=status,
                dry_run=bool(bet.dry_run) or bet.status is BetStatus.DRY_RUN,
                stake=stake,
                pnl=pnl,
                odds=to_decimal(bet.placed_odds) or to_decimal(bet.requested_odds),
                clv=bet.clv,
                ev=float(ev) if ev is not None else None,
                p_win=float(p_win) if p_win is not None else None,
                placed_at=placed_at,
                settled_at=settled_at,
                created_at=created_at,
            )
        )
    return out


class LedgerView:
    """The bankroll ledger: current balance, debited stakes and a time-ordered balance curve."""

    def __init__(self, rows: Sequence[BankrollLedger]) -> None:
        self.rows = sorted(rows, key=lambda r: r.id)  # booking order

    @classmethod
    def load(cls, session: Session) -> LedgerView:
        return cls(list(session.scalars(select(BankrollLedger))))

    def current(self) -> Decimal | None:
        """Balance after the most recently written row (the engine's ``ledger.current_balance``)."""
        return to_decimal(self.rows[-1].balance_after) if self.rows else None

    def debited_bet_ids(self) -> set[int]:
        """Bets whose stake has been debited (a STAKE row exists)."""
        return {r.bet_id for r in self.rows if r.kind is LedgerKind.STAKE and r.bet_id is not None}

    def deposits(self) -> Decimal:
        return sum((to_decimal(r.amount) or ZERO for r in self.rows if r.kind is LedgerKind.DEPOSIT), ZERO)

    def curve(self) -> list[EquityPoint]:
        """Balance over time: rows in time order with the running balance recomputed in that order.

        ``balance_after`` is a running balance in *booking* order; a back-dated row (a stake booked at
        the time the bet was really placed, a settlement dated at the match end) would make a curve
        of raw ``balance_after`` values jump back and forth. The recomputed curve is anchored so its
        last point is the current balance; without back-dated rows it equals ``balance_after``.
        """
        if not self.rows:
            return []
        ordered = sorted(self.rows, key=lambda r: (ensure_utc(r.ts), r.id))
        amounts = [to_decimal(r.amount) or ZERO for r in ordered]
        running = (self.current() or ZERO) - sum(amounts, ZERO)
        points: list[EquityPoint] = []
        for row, amount in zip(ordered, amounts, strict=True):
            running += amount
            points.append(
                EquityPoint(
                    ts=ensure_utc(row.ts),
                    balance=money_str0(running),
                    kind=row.kind.value,
                    amount=money_str0(amount),
                )
            )
        return points


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
    reference: Decimal,
    facts: Sequence[BetFact],
) -> LossLimitOut:
    """One loss limit, computed like ``engine.bankroll.loss_limit_status`` (see the module doc)."""
    realized = _realized_between(facts, start, None)
    start_bankroll = reference - realized
    at_risk = sum((f.stake for f in facts if f.is_open), ZERO)
    limit_amount = (
        (start_bankroll * D(fraction)).quantize(CENT, rounding=ROUND_DOWN) if start_bankroll > 0 else ZERO
    )
    loss = max(ZERO, -realized)
    reached = loss > 0 and loss >= limit_amount
    return LossLimitOut(
        period=period,
        period_start=start,
        start_bankroll=money_str0(start_bankroll),
        limit_fraction=fraction,
        limit_amount=money_str0(limit_amount),
        realized_pnl=money_str0(realized),
        open_stakes=money_str0(at_risk),
        worst_case_pnl=money_str0(realized - at_risk),
        remaining=money_str0(max(ZERO, limit_amount - loss)),
        stake_allowance=money_str0(max(ZERO, limit_amount - loss - at_risk)),
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

    # ``in_play``: stakes of open bets that already left the balance. Paper bets: every open bet.
    # Real bets: those with a STAKE ledger row (pending / unconfirmed placements are still part of
    # the balance: they are exposure, and they reduce ``available_balance``).
    open_total = stats.open_stakes
    balance_source: Literal["ledger", "starting_bankroll", "paper"]
    if dry_run:
        in_play = open_total
        balance = starting + stats.realized_pnl - in_play
        balance_source = "paper"
    elif ledger.rows:
        debited = ledger.debited_bet_ids()
        in_play = sum((f.stake for f in facts if f.is_open and f.id in debited), ZERO)
        balance = ledger.current() or ZERO
        balance_source = "ledger"
    else:  # no ledger yet (the engine writes the opening deposit on its first run)
        in_play = sum((f.stake for f in facts if f.is_open and f.status in MONEY_OUT_STATUSES), ZERO)
        balance = starting + stats.realized_pnl - in_play
        balance_source = "starting_bankroll"
    reference = balance + in_play
    available = balance - (open_total - in_play)

    day_start = local_day_start(now, tz)
    week_start = local_week_start(now, tz)
    daily = _limit("day", day_start, bk.daily_loss_limit_fraction, reference, facts)
    weekly = _limit("week", week_start, bk.weekly_loss_limit_fraction, reference, facts)

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
        equity = ledger.curve()
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
        available_balance=money_str0(available),
        bankroll_reference=money_str0(reference),
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
