"""Bankroll state, stake sizing (fractional Kelly + hard caps) and loss limits.

State (:func:`compute_state`), from the ledger and the ``bets`` table; dry-run bets are excluded
from all money and limits:

* ``balance``: the latest ledger balance minus the stakes of open bets that have not been debited in
  the ledger yet (``reserved_stakes``; normally zero: a stake is debited when the bet is placed),
  i.e. the cash that is still free to stake. The ledger is initialised with a DEPOSIT of
  ``starting_bankroll`` if empty.
* open stakes: bets in PENDING / PLACING / PLACED; ``bankroll_reference = balance + open stakes``
  (cash plus money at risk) is the bankroll that stakes and limits are sized from.
* realized P&L today / this week: ``pnl`` of bets settled since the local (Europe/Warsaw) midnight /
  Monday 00:00 (``cs2bot.utils.time.local_day_start`` / ``local_week_start``, DST-aware; the UTC
  instants are returned as ``day_start`` / ``week_start``). A settled bet without ``settled_at``
  (an integration bug) is dated by its ``updated_at`` rather than silently ignored.
* ``day_start_bankroll = bankroll_reference - realized_pnl_today`` (same for the week). Deposits,
  withdrawals and balance corrections made during the period are deliberately treated as if they
  had happened at the start of the period: they are part of the capital the limit protects. This
  makes the very first day work (the initial deposit is the day-start bankroll, not 0) and it is
  conservative after a withdrawal (the limit shrinks with the bankroll).

Stake sizing (:func:`size_stake`), PLAN.md section 6:

``stake = kelly_fraction x kelly_full x bankroll_reference``, then capped by

* ``max_stake_fraction x bankroll_reference``;
* the match exposure cap ``max_match_exposure_fraction x bankroll_reference - open stakes on the
  match`` (markets on the same match are correlated);
* the daily guard ``daily_loss_limit_fraction x day_start_bankroll - (realized loss today + ALL
  open stakes)`` - even if every open bet loses today, the day's loss stays within the limit.
  Open stakes placed on earlier days count too (:func:`total_open_stakes`): bets are placed up to
  ``max_hours_before_start`` ahead, so yesterday's open bets usually settle today, and counting only
  "stakes placed today" would let the worst-case loss of one day reach a multiple of the limit
  (place the full allowance before midnight and again after it). Realized profits never enlarge the
  allowance (loss = ``max(0, -pnl)``);
* the weekly guard (same with the week numbers);
* ``max_bets_per_day`` and the free ``balance``;
* the 10 % winnings tax: when the stake would push the winning above 2,280 PLN, the stake is held
  at the tax-free limit unless the larger, taxed stake has a higher expected log-growth;

rounded DOWN to ``stake_step`` (never above any cap); below Betclic's ``min_stake`` it becomes 0 with
a reason. A halted bot (kill switch / pause) never gets a stake.

The caps only hold if every bet is sized against a state that already contains the previous bets:
either recompute the state after each bet row is created, or size a whole batch with
:func:`size_decisions`, which reserves each stake in a running copy of the state.

Loss limits (:func:`check_loss_limits`): when the realized loss of the day (week) reaches
``daily_loss_limit_fraction x day_start_bankroll`` (weekly likewise) the bot is paused with a clear
reason. Resuming is manual (dashboard); a limit pauses only once per period, so a manual resume is
not undone by the next scheduler tick (the daily/weekly guards still block new stakes until the
period rolls over).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, BetclicSettings, Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import BankrollLedger, Bet
from cs2bot.domain.betting import BankrollState, BetDecision
from cs2bot.domain.enums import BetStatus, LedgerKind
from cs2bot.engine import ledger
from cs2bot.engine.ev import (
    TaxModel,
    big_win_stake_limit,
    effective_odds,
    expected_log_growth,
    kelly_for_net_odds,
)
from cs2bot.utils.logging import get_logger
from cs2bot.utils.money import CENT, D, quantize_money, round_down_to_step
from cs2bot.utils.time import WARSAW, local_day_start, local_week_start, utcnow

logger = get_logger(__name__)

OPEN_STATUSES: frozenset[BetStatus] = frozenset({BetStatus.PENDING, BetStatus.PLACING, BetStatus.PLACED})
SETTLED_STATUSES: frozenset[BetStatus] = frozenset({BetStatus.WON, BetStatus.LOST, BetStatus.VOID})
#: Statuses that use up one of the ``max_bets_per_day`` slots.
COUNTED_STATUSES: frozenset[BetStatus] = OPEN_STATUSES | SETTLED_STATUSES
#: ``system_state`` key remembering which period's loss limit already paused the bot.
LOSS_LIMIT_STATE_KEY = "loss_limit_pauses"

_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class LossLimitStatus:
    """Where the bankroll stands against the daily / weekly loss limits (dashboard + pausing)."""

    daily_limit: Decimal
    weekly_limit: Decimal
    realized_loss_today: Decimal  # max(0, -realized_pnl_today)
    realized_loss_week: Decimal
    daily_allowance: Decimal  # daily_limit - (realized loss today + all open stakes)
    weekly_allowance: Decimal  # weekly_limit - (realized loss this week + all open stakes)
    daily_hit: bool
    weekly_hit: bool
    open_stakes: Decimal = _ZERO  # all stakes at risk right now (see total_open_stakes)


# ----------------------------------------------------------------------------------------------
# state
# ----------------------------------------------------------------------------------------------


def _bet_time(bet: Bet) -> datetime | None:
    return bet.placed_at if bet.placed_at is not None else bet.created_at


def _settled_time(bet: Bet) -> datetime | None:
    """When a settled bet counts for the loss limits: ``settled_at``, else ``updated_at`` (see module
    doc) and, as a last resort, when it was placed / created."""
    if bet.settled_at is not None:
        return bet.settled_at
    return bet.updated_at if bet.updated_at is not None else _bet_time(bet)


def total_open_stakes(state: BankrollState) -> Decimal:
    """Every stake currently at risk, whenever it was placed (the worst-case loss still to come).

    ``compute_state`` guarantees ``bankroll_reference = balance + open stakes``; the other fields are
    included so that a hand-built or partially filled state is never read as *less* exposure.
    """
    by_match = sum(state.exposure_by_match.values(), _ZERO)
    return max(
        state.bankroll_reference - state.balance,
        by_match,
        state.open_stakes_week,
        state.open_stakes_today,
        _ZERO,
    )


def _settled_pnl(bet: Bet, settings: Settings) -> Decimal:
    if bet.pnl is not None:
        return quantize_money(D(bet.pnl))
    outcome = {BetStatus.WON: "won", BetStatus.LOST: "lost", BetStatus.VOID: "void"}[bet.status]
    if bet.status is BetStatus.WON and bet.payout is not None:
        return quantize_money(D(bet.payout)) - quantize_money(D(bet.stake))
    return ledger.settle_amounts(bet, outcome, settings)[1]


def compute_state(
    session: Session,
    settings: Settings,
    *,
    now: datetime,
    bankroll_settings: BankrollSettings | None = None,
) -> BankrollState:
    """Snapshot of balance, exposure, realized P&L and safety flags at ``now`` (see module doc).

    ``bankroll_settings`` defaults to the effective settings (dashboard overrides applied); it is
    only used to initialise an empty ledger. May write the initial DEPOSIT (flushes, no commit).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = settings.timezone
    day_start = local_day_start(now, tz)
    week_start = local_week_start(now, tz)
    bankroll = bankroll_settings or db_state.effective_bankroll_settings(session, settings)
    ledger.ensure_initialized(session, bankroll, at=now)
    ledger_balance = ledger.current_balance(session)

    # Coarse SQL filter (one day of slack keeps it independent of how the DB compares timestamps),
    # exact filtering in Python with aware datetimes below.
    slack = timedelta(days=1)
    bets = list(
        session.scalars(
            select(Bet).where(
                Bet.dry_run.is_(False),
                Bet.status != BetStatus.DRY_RUN,
                or_(
                    Bet.status.in_(list(OPEN_STATUSES)),
                    Bet.settled_at >= week_start - slack,
                    and_(
                        Bet.status.in_(list(SETTLED_STATUSES)),
                        Bet.settled_at.is_(None),
                        Bet.updated_at >= week_start - slack,
                    ),
                    Bet.placed_at >= day_start - slack,
                    Bet.created_at >= day_start - slack,
                ),
            )
        )
    )
    open_bets = [b for b in bets if b.status in OPEN_STATUSES]
    debited: set[int] = set()
    if open_bets:
        debited = set(
            session.scalars(
                select(BankrollLedger.bet_id).where(
                    BankrollLedger.kind == LedgerKind.STAKE,
                    BankrollLedger.bet_id.in_([b.id for b in open_bets]),
                )
            )
        )
    reserved = sum((D(b.stake) for b in open_bets if b.id not in debited), _ZERO)
    open_total = sum((D(b.stake) for b in open_bets), _ZERO)
    balance = quantize_money(ledger_balance - reserved)
    bankroll_reference = quantize_money(balance + open_total)

    pnl_today = _ZERO
    pnl_week = _ZERO
    undated: list[int] = []
    for bet in bets:
        if bet.status not in SETTLED_STATUSES:
            continue
        if bet.settled_at is None:
            undated.append(bet.id)
        settled = _settled_time(bet)
        if settled is None or settled >= week_start:  # unknown time: assume it is recent (conservative)
            pnl = _settled_pnl(bet, settings)
            pnl_week += pnl
            if settled is None or settled >= day_start:
                pnl_today += pnl
    if undated:
        logger.warning(
            "settled bets without settled_at (dated by updated_at for the loss limits): %s", undated[:20]
        )

    open_today = _ZERO
    open_week = _ZERO
    exposure: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    for bet in open_bets:
        stake = D(bet.stake)
        if bet.match_id is not None:
            exposure[bet.match_id] += stake
        when = _bet_time(bet)
        if when is None or when >= week_start:  # unknown time: assume it is recent (conservative)
            open_week += stake
            if when is None or when >= day_start:
                open_today += stake

    placed_today = 0
    for bet in bets:
        when = _bet_time(bet)
        if bet.status in COUNTED_STATUSES and (when is None or when >= day_start):
            placed_today += 1

    kill = db_state.is_kill_switch_on(session)
    paused, pause_reason = db_state.get_pause(session)
    return BankrollState(
        balance=balance,
        bankroll_reference=bankroll_reference,
        day_start_bankroll=quantize_money(bankroll_reference - pnl_today),
        week_start_bankroll=quantize_money(bankroll_reference - pnl_week),
        realized_pnl_today=quantize_money(pnl_today),
        realized_pnl_week=quantize_money(pnl_week),
        open_stakes_today=quantize_money(open_today),
        open_stakes_week=quantize_money(open_week),
        exposure_by_match={k: quantize_money(v) for k, v in exposure.items()},
        day_start=day_start,
        week_start=week_start,
        reserved_stakes=quantize_money(reserved),
        bets_placed_today=placed_today,
        kill_switch=kill,
        paused=paused,
        pause_reason=pause_reason if paused else None,
    )


# ----------------------------------------------------------------------------------------------
# loss limits
# ----------------------------------------------------------------------------------------------


def loss_limit_status(state: BankrollState, settings: BankrollSettings) -> LossLimitStatus:
    """Daily / weekly loss limits, realized losses and the remaining guard allowance."""
    daily_limit = quantize_money(D(settings.daily_loss_limit_fraction) * state.day_start_bankroll)
    weekly_limit = quantize_money(D(settings.weekly_loss_limit_fraction) * state.week_start_bankroll)
    loss_today = max(_ZERO, -state.realized_pnl_today)
    loss_week = max(_ZERO, -state.realized_pnl_week)
    at_risk = total_open_stakes(state)
    return LossLimitStatus(
        daily_limit=daily_limit,
        weekly_limit=weekly_limit,
        realized_loss_today=loss_today,
        realized_loss_week=loss_week,
        daily_allowance=daily_limit - (loss_today + at_risk),
        weekly_allowance=weekly_limit - (loss_week + at_risk),
        daily_hit=loss_today > 0 and loss_today >= daily_limit,
        weekly_hit=loss_week > 0 and loss_week >= weekly_limit,
        open_stakes=at_risk,
    )


def check_loss_limits(
    session: Session,
    state: BankrollState,
    settings: Settings | BankrollSettings,
    *,
    actor: str = "system",
    now: datetime | None = None,
) -> bool:
    """Pause the bot (``db.state.set_paused``) when a realized loss limit is reached.

    Returns True if a daily or weekly limit is currently reached. The pause is written once per
    period (remembered in ``system_state["loss_limit_pauses"]``) so that the owner's manual resume
    is respected; this function never resumes. The current day / week are ``state.day_start`` /
    ``state.week_start`` (set by :func:`compute_state`); for a state without them they are derived
    from ``now`` (default: utcnow) and the timezone of a full :class:`Settings` (default
    Europe/Warsaw).
    """
    if isinstance(settings, Settings):
        bankroll = db_state.effective_bankroll_settings(session, settings)
        tz: str | ZoneInfo = settings.timezone
    else:
        bankroll, tz = settings, WARSAW
    when = now or utcnow()
    day_id = (state.day_start or local_day_start(when, tz)).isoformat()
    week_id = (state.week_start or local_week_start(when, tz)).isoformat()
    status = loss_limit_status(state, bankroll)
    hits: list[tuple[str, str, str]] = []  # (period kind, period id, message)
    if status.daily_hit:
        hits.append(
            (
                "daily",
                day_id,
                f"daily loss limit reached: realized loss today {status.realized_loss_today} PLN "
                f">= {status.daily_limit} PLN ({bankroll.daily_loss_limit_fraction:.0%} of the "
                f"day-start bankroll {state.day_start_bankroll} PLN)",
            )
        )
    if status.weekly_hit:
        hits.append(
            (
                "weekly",
                week_id,
                f"weekly loss limit reached: realized loss this week {status.realized_loss_week} PLN "
                f">= {status.weekly_limit} PLN ({bankroll.weekly_loss_limit_fraction:.0%} of the "
                f"week-start bankroll {state.week_start_bankroll} PLN)",
            )
        )
    if not hits:
        return False
    memo = db_state.get_state(session, LOSS_LIMIT_STATE_KEY)
    fresh = [hit for hit in hits if memo.get(hit[0]) != hit[1]]
    if fresh:
        reason = "; ".join(msg for _, _, msg in hits) + " - resume manually from the dashboard"
        db_state.set_paused(session, True, actor=actor, reason=reason)
        memo.update({kind: period for kind, period, _ in fresh})
        db_state.set_state(session, LOSS_LIMIT_STATE_KEY, memo, actor=actor)
        logger.warning("trading paused: %s", reason)
    return True


# ----------------------------------------------------------------------------------------------
# stake sizing
# ----------------------------------------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"{quantize_money(value)} PLN"


def size_stake(
    decision: BetDecision,
    state: BankrollState,
    settings: BankrollSettings,
    betclic: BetclicSettings,
) -> tuple[Decimal, list[str]]:
    """Stake for an accepted decision and the reasons behind it (module doc for the rules).

    Returns ``(stake, messages)``. A zero stake comes with the reasons why nothing can be staked
    (prefixed with a code like ``"halted: ..."``); a positive stake comes with informational notes
    about the caps that bound it (``"capped: ..."``). :func:`apply_stake` files them under
    ``BetDecision.reasons`` and ``BetDecision.notes`` respectively.

    Pure function of its arguments: the caller must make sure ``state`` already contains every bet
    created so far (see the module docstring and :func:`size_decisions`), and must still check
    ``db.state.is_trading_halted`` immediately before placing the bet.
    """
    reasons: list[str] = []
    if not decision.accepted:
        reasons.append("not_accepted: the decision was rejected by the decision rules")
    if state.kill_switch:
        reasons.append("halted: kill switch is on")
    if state.paused:
        reasons.append(f"halted: paused ({state.pause_reason or 'no reason given'})")
    if state.bets_placed_today >= settings.max_bets_per_day:
        reasons.append(
            f"bets_per_day: {state.bets_placed_today} bets placed today (max {settings.max_bets_per_day})"
        )
    kelly_full = decision.priced.kelly_full
    if not math.isfinite(kelly_full) or kelly_full <= 0:
        reasons.append(f"no_edge: full Kelly fraction {kelly_full:.4f} <= 0")
    bankroll = state.bankroll_reference
    if bankroll <= 0:
        reasons.append(f"no_bankroll: bankroll reference is {_money(bankroll)}")
    if reasons:
        return _ZERO, reasons

    status = loss_limit_status(state, settings)
    kelly_stake = D(settings.kelly_fraction) * D(kelly_full) * bankroll
    caps: list[tuple[str, Decimal, str]] = [
        (
            "max_stake",
            D(settings.max_stake_fraction) * bankroll,
            f"max stake {settings.max_stake_fraction:.1%} of bankroll {_money(bankroll)}",
        ),
        (
            "match_exposure",
            D(settings.max_match_exposure_fraction) * bankroll
            - state.exposure_by_match.get(decision.match_id, _ZERO),
            f"match exposure cap {settings.max_match_exposure_fraction:.1%} of bankroll, "
            f"{_money(state.exposure_by_match.get(decision.match_id, _ZERO))} already on match "
            f"{decision.match_id}",
        ),
        (
            "daily_loss_guard",
            status.daily_allowance,
            f"daily loss guard: limit {_money(status.daily_limit)}, realized loss "
            f"{_money(status.realized_loss_today)}, open stakes {_money(status.open_stakes)}",
        ),
        (
            "weekly_loss_guard",
            status.weekly_allowance,
            f"weekly loss guard: limit {_money(status.weekly_limit)}, realized loss "
            f"{_money(status.realized_loss_week)}, open stakes {_money(status.open_stakes)}",
        ),
        ("balance", state.balance, f"free balance {_money(state.balance)}"),
    ]
    exhausted = [f"{name}: nothing left to stake ({detail})" for name, cap, detail in caps if cap <= 0]
    if exhausted:
        return _ZERO, exhausted

    stake = kelly_stake
    notes: list[str] = []
    for name, cap, detail in caps:
        if cap < stake:
            stake = cap
            notes.append(f"capped: {name} -> {_money(cap)} ({detail})")

    stake, tax_note = _respect_winnings_tax(decision, stake, bankroll, settings, betclic)
    if tax_note:
        notes.append(tax_note)

    ceiling = stake
    stake = round_down_to_step(stake, settings.stake_step)
    while stake > ceiling:  # only possible with a step finer than a grosz: never round above a cap
        stake -= CENT
    if stake < betclic.min_stake:
        detail = f"sized stake {_money(stake)} < Betclic minimum {_money(betclic.min_stake)}"
        if notes:
            detail += " after " + "; ".join(notes)
        return _ZERO, [f"below_min_stake: {detail}"]
    return stake, notes


def _respect_winnings_tax(
    decision: BetDecision,
    stake: Decimal,
    bankroll_reference: Decimal,
    settings: BankrollSettings,
    betclic: BetclicSettings,
) -> tuple[Decimal, str | None]:
    """Hold the stake at the 2,280 PLN tax-free winning limit unless crossing it pays off.

    Above the threshold 10 % of the whole winning is withheld, so the net odds drop from
    ``b = o_eff - 1`` to ``b' = o_eff (1 - r) - 1`` and a stake just above the limit is strictly
    worse than the limit itself. Candidates: the limit (net odds ``b``) and the fractional-Kelly
    stake for ``b'`` (if it lies above the limit); the one with the higher expected log-growth of
    the bankroll wins.
    """
    model = TaxModel.from_settings(betclic)
    odds = decision.priced.quote.odds
    limit = big_win_stake_limit(odds, model)
    if limit is None or stake <= limit:
        return stake, None
    outcome = decision.priced.outcome
    p_win, p_lose = outcome.p_win, outcome.p_lose
    refunds = {
        "p_refund": max(0.0, outcome.p_push) + max(0.0, outcome.p_void),
        "refund_loss": float(model.refund_loss_rate),
    }
    bankroll = float(bankroll_reference)
    o_eff = float(effective_odds(odds, tax_rate=model.stake_tax_rate))
    b_free = o_eff - 1.0
    b_taxed = o_eff * (1.0 - float(model.big_win_rate)) - 1.0
    taxed_full_kelly = max(0.0, kelly_for_net_odds(p_win, p_lose, b_taxed, **refunds))
    # same fractional Kelly and the same caps (``stake`` already includes every cap)
    taxed_stake = min(D(settings.kelly_fraction * taxed_full_kelly) * bankroll_reference, stake)
    growth_limit = expected_log_growth(p_win, p_lose, b_free, float(limit) / bankroll, **refunds)
    if taxed_stake > limit:
        growth_taxed = expected_log_growth(p_win, p_lose, b_taxed, float(taxed_stake) / bankroll, **refunds)
        if growth_taxed > growth_limit:
            return taxed_stake, (
                f"winnings tax: staking {_money(taxed_stake)} although the winning exceeds "
                f"{_money(model.big_win_threshold)} ({model.big_win_rate:.0%} withheld) - higher "
                f"expected log-growth than the tax-free limit {_money(limit)}"
            )
    return limit, (
        f"capped: winnings tax -> {_money(limit)} keeps the winning at or below "
        f"{_money(model.big_win_threshold)} (above it {model.big_win_rate:.0%} of the whole "
        "winning is withheld)"
    )


def apply_stake(decision: BetDecision, state: BankrollState, stake: Decimal, notes: list[str]) -> BetDecision:
    """Copy of ``decision`` with the result of :func:`size_stake` applied.

    A positive stake keeps the decision accepted and files ``notes`` under ``BetDecision.notes``
    (informational: which caps were binding). A zero stake rejects the decision and appends ``notes``
    - then the reasons why nothing can be staked - to ``BetDecision.reasons``.
    ``kelly_fraction_used`` is the fraction of ``bankroll_reference`` actually staked.
    """
    staked = stake > 0
    used = float(stake / state.bankroll_reference) if state.bankroll_reference > 0 and staked else 0.0
    update: dict[str, object] = {
        "stake": stake if staked else _ZERO,
        "kelly_fraction_used": used,
        "accepted": decision.accepted and staked,
    }
    if staked:
        update["notes"] = [*decision.notes, *notes]
    else:
        update["reasons"] = [*decision.reasons, *notes]
    return decision.model_copy(update=update)


def size_decision(
    decision: BetDecision, state: BankrollState, settings: BankrollSettings, betclic: BetclicSettings
) -> BetDecision:
    """:func:`size_stake` + :func:`apply_stake` in one call.

    A decision the decision rules already rejected is returned unchanged (stake 0, its own reasons).
    """
    if not decision.accepted:
        return decision
    stake, notes = size_stake(decision, state, settings, betclic)
    return apply_stake(decision, state, stake, notes)


def reserve_stake(state: BankrollState, match_id: int, stake: Decimal) -> BankrollState:
    """The state after a new bet of ``stake`` on ``match_id`` has been created (not yet debited).

    The stake leaves the free balance and joins the open stakes, the match exposure and today's bet
    count; ``bankroll_reference`` is unchanged. This is exactly what :func:`compute_state` returns
    once the PENDING ``bets`` row exists, so a batch can be sized without touching the database.
    """
    if stake <= 0:
        return state
    exposure = dict(state.exposure_by_match)
    exposure[match_id] = exposure.get(match_id, _ZERO) + stake
    return state.model_copy(
        update={
            "balance": state.balance - stake,
            "open_stakes_today": state.open_stakes_today + stake,
            "open_stakes_week": state.open_stakes_week + stake,
            "exposure_by_match": exposure,
            "reserved_stakes": state.reserved_stakes + stake,
            "bets_placed_today": state.bets_placed_today + 1,
        }
    )


def size_decisions(
    decisions: Iterable[BetDecision],
    state: BankrollState,
    settings: BankrollSettings,
    betclic: BetclicSettings,
) -> tuple[list[BetDecision], BankrollState]:
    """Size a batch of decisions one after another against a running copy of ``state``.

    Each accepted decision is sized with :func:`size_decision` and its stake is reserved
    (:func:`reserve_stake`) before the next one is sized, so the batch as a whole respects every
    cap, guard and the bets-per-day limit - sizing all decisions against the same snapshot would
    not. Decisions are processed in the given order (put the best ones first). Returns the sized
    decisions (same order) and the projected state after all of them.
    """
    sized: list[BetDecision] = []
    running = state
    for decision in decisions:
        result = size_decision(decision, running, settings, betclic)
        if result.accepted and result.stake > 0:
            running = reserve_stake(running, result.match_id, result.stake)
        sized.append(result)
    return sized, running
