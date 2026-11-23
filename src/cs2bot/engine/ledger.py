"""Bankroll ledger: every money movement at the bookmaker, with a running balance.

Each ``bankroll_ledger`` row stores a signed ``amount`` (+ money in, - money out) and the
``balance_after`` it produced; the current balance is the ``balance_after`` of the most recently
*written* row (highest id), so back-dated entries (``at=``) never break the running balance.

Money conventions (see :mod:`cs2bot.engine.ev` for the tax research):

* ``bets.stake`` is the GROSS stake: :func:`record_stake` debits all of it (the 12 % stake tax is
  part of it), ``bets.tax = stake x stake tax rate``;
* a winning bet credits ``ev.net_payout`` (stake tax and the 10 % winnings tax above 2,280 PLN
  already deducted) via :func:`record_payout`;
* a void / pushed bet refunds the full gross stake (:func:`record_refund`), or ``stake - stake tax``
  when ``betclic.void_refunds_stake_tax`` is off (pass ``amount=`` from :func:`settle_amounts`);
* dry-run bets never touch the ledger (every ``record_*`` returns ``None`` for them);
* entries are idempotent per bet and kind: recording the same stake / payout / refund twice
  returns the existing row instead of moving money twice;
* a bet is either paid out or refunded, never both: the second call raises
  :class:`LedgerConflictError` instead of crediting the balance twice.

:func:`settle_bet` applies a settlement outcome to a bet in one step (status, payout, pnl,
``settled_at`` and the ledger entries) and is the recommended entry point for the settlement service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, Settings
from cs2bot.db import state
from cs2bot.db.models import BankrollLedger, Bet
from cs2bot.domain.enums import BetStatus, LedgerKind
from cs2bot.domain.results import SettlementOutcome
from cs2bot.engine.ev import TaxModel, TaxSource, payout_breakdown, refund_amount, stake_tax
from cs2bot.utils.logging import get_logger
from cs2bot.utils.money import D, quantize_money
from cs2bot.utils.time import utcnow

logger = get_logger(__name__)

SettleOutcome = Literal["won", "lost", "void", "push"]
_ZERO = Decimal("0.00")
#: ``bets.extra`` key holding the stake tax rate charged at placement (string, e.g. "0.12").
STAKE_TAX_RATE_KEY = "stake_tax_rate"


class LedgerConflictError(ValueError):
    """The requested entry contradicts what the ledger already holds for this bet (e.g. a payout for
    a bet that was already refunded). Nothing is written; resolve it by hand (``record_adjustment``)."""


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of comparing the ledger with the balance observed at the bookmaker."""

    observed_balance: Decimal
    ledger_balance: Decimal  # ledger balance before the reconciliation entry
    difference: Decimal  # observed - ledger
    tolerance: Decimal
    flagged: bool  # |difference| > tolerance: needs the owner's attention
    entry_id: int | None  # RECONCILIATION entry written to bring the ledger in line (None if equal)
    at: datetime


def is_dry_run(bet: Bet) -> bool:
    return bool(bet.dry_run) or bet.status is BetStatus.DRY_RUN


def _bankroll_settings(session: Session, settings: Settings | BankrollSettings) -> BankrollSettings:
    if isinstance(settings, BankrollSettings):
        return settings
    return state.effective_bankroll_settings(session, settings)


def current_balance(session: Session) -> Decimal:
    """Balance after the most recently written ledger entry (0.00 for an empty ledger)."""
    row = session.scalar(select(BankrollLedger).order_by(BankrollLedger.id.desc()).limit(1))
    return quantize_money(D(row.balance_after)) if row is not None else _ZERO


def ensure_initialized(
    session: Session, settings: Settings | BankrollSettings, *, at: datetime | None = None
) -> BankrollLedger | None:
    """If the ledger is empty, write a DEPOSIT of ``bankroll.starting_bankroll``; else do nothing.

    With a full :class:`Settings`, dashboard overrides of ``starting_bankroll`` are honoured.
    Returns the created entry or ``None``.
    """
    if session.scalar(select(BankrollLedger.id).limit(1)) is not None:
        return None
    amount = quantize_money(D(_bankroll_settings(session, settings).starting_bankroll))
    entry = BankrollLedger(
        ts=at or utcnow(),
        kind=LedgerKind.DEPOSIT,
        amount=amount,
        balance_after=amount,
        note="initial bankroll (bankroll.starting_bankroll)",
    )
    session.add(entry)
    session.flush()
    logger.info("bankroll ledger initialised with a deposit of %s", amount)
    return entry


def _append(
    session: Session,
    *,
    kind: LedgerKind,
    amount: Decimal,
    bet_id: int | None = None,
    note: str | None = None,
    at: datetime | None = None,
) -> BankrollLedger:
    value = quantize_money(D(amount))
    balance = current_balance(session) + value
    if balance < 0:
        logger.warning("ledger balance goes negative (%s) after %s %s - reconcile soon", balance, kind, value)
    entry = BankrollLedger(
        ts=at or utcnow(), kind=kind, amount=value, balance_after=balance, bet_id=bet_id, note=note
    )
    session.add(entry)
    session.flush()
    return entry


def _bet_entry(session: Session, bet_id: int, kind: LedgerKind) -> BankrollLedger | None:
    return session.scalar(
        select(BankrollLedger)
        .where(BankrollLedger.bet_id == bet_id, BankrollLedger.kind == kind)
        .order_by(BankrollLedger.id)
        .limit(1)
    )


def _persisted_id(session: Session, bet: Bet) -> int:
    if bet.id is None:
        session.flush()
    if bet.id is None:  # pragma: no cover - flush always assigns the id of an added bet
        raise ValueError("the bet must be added to the session before touching the ledger")
    return bet.id


def record_stake(session: Session, bet: Bet, *, at: datetime | None = None) -> BankrollLedger | None:
    """Debit the gross stake of a real bet (idempotent). Dry runs: no entry, returns ``None``."""
    if is_dry_run(bet):
        return None
    bet_id = _persisted_id(session, bet)
    existing = _bet_entry(session, bet_id, LedgerKind.STAKE)
    if existing is not None:
        return existing
    stake = quantize_money(D(bet.stake))
    if stake <= 0:
        raise ValueError(f"bet {bet_id} has a non-positive stake {stake}")
    return _append(
        session,
        kind=LedgerKind.STAKE,
        amount=-stake,
        bet_id=bet_id,
        note=f"stake {bet.spec_key}#{bet.selection_key}",
        at=at or bet.placed_at,
    )


def record_payout(
    session: Session, bet: Bet, payout: Decimal, *, at: datetime | None = None
) -> BankrollLedger | None:
    """Credit the payout of a won bet (idempotent). Zero payouts and dry runs write nothing.

    If the stake of the bet was never debited, it is recorded first so the ledger stays consistent.
    """
    if is_dry_run(bet):
        return None
    amount = quantize_money(D(payout))
    if amount < 0:
        raise ValueError("payout must be >= 0")
    if amount == 0:
        return None
    bet_id = _persisted_id(session, bet)
    existing = _bet_entry(session, bet_id, LedgerKind.PAYOUT)
    if existing is not None:
        if D(existing.amount) != amount:
            logger.warning(
                "bet %s: payout %s already recorded, ignoring a second payout of %s",
                bet_id,
                existing.amount,
                amount,
            )
        return existing
    if _bet_entry(session, bet_id, LedgerKind.REFUND) is not None:
        raise LedgerConflictError(f"bet {bet_id} was already refunded - it cannot also be paid out")
    if _bet_entry(session, bet_id, LedgerKind.STAKE) is None:
        logger.warning("bet %s settled without a recorded stake - recording the stake first", bet_id)
        record_stake(session, bet)
    return _append(
        session,
        kind=LedgerKind.PAYOUT,
        amount=amount,
        bet_id=bet_id,
        note=f"payout {bet.spec_key}#{bet.selection_key}",
        at=at or bet.settled_at,
    )


def record_refund(
    session: Session, bet: Bet, *, amount: Decimal | None = None, at: datetime | None = None
) -> BankrollLedger | None:
    """Refund the debited stake of a void / pushed / failed bet (idempotent).

    ``amount`` defaults to the whole debited stake (a bet that never existed at the bookmaker, or a
    void with full refunds); pass the refund from :func:`settle_amounts` when the bookmaker keeps the
    stake tax on voided bets. Nothing is written for dry runs or when the stake was never debited (no
    money left the balance). Raises :class:`LedgerConflictError` if the bet was already paid out.
    """
    if is_dry_run(bet):
        return None
    bet_id = _persisted_id(session, bet)
    stake_entry = _bet_entry(session, bet_id, LedgerKind.STAKE)
    if stake_entry is None:
        return None
    existing = _bet_entry(session, bet_id, LedgerKind.REFUND)
    if existing is not None:
        return existing
    if _bet_entry(session, bet_id, LedgerKind.PAYOUT) is not None:
        raise LedgerConflictError(f"bet {bet_id} was already paid out - it cannot also be refunded")
    debited = -D(stake_entry.amount)
    value = debited if amount is None else quantize_money(D(amount))
    if not _ZERO <= value <= debited:
        raise ValueError(f"refund {value} for bet {bet_id} is outside [0, debited stake {debited}]")
    if value == 0:
        return None
    return _append(
        session,
        kind=LedgerKind.REFUND,
        amount=value,
        bet_id=bet_id,
        note=f"refund {bet.spec_key}#{bet.selection_key}",
        at=at or bet.settled_at,
    )


def record_adjustment(
    session: Session, amount: Decimal, note: str, *, at: datetime | None = None
) -> BankrollLedger:
    """Manual signed correction of the balance (bonus credited, fee, correction ...)."""
    return _append(session, kind=LedgerKind.ADJUSTMENT, amount=D(amount), note=note, at=at)


def record_deposit(
    session: Session, amount: Decimal, note: str = "deposit", *, at: datetime | None = None
) -> BankrollLedger:
    if D(amount) <= 0:
        raise ValueError("deposit amount must be positive")
    return _append(session, kind=LedgerKind.DEPOSIT, amount=D(amount), note=note, at=at)


def record_withdrawal(
    session: Session, amount: Decimal, note: str = "withdrawal", *, at: datetime | None = None
) -> BankrollLedger:
    if D(amount) <= 0:
        raise ValueError("withdrawal amount must be positive")
    return _append(session, kind=LedgerKind.WITHDRAWAL, amount=-D(amount), note=note, at=at)


def reconcile(
    session: Session,
    observed_balance: Decimal,
    *,
    tolerance: Decimal,
    actor: str = "system",
    at: datetime | None = None,
) -> ReconcileResult:
    """Bring the ledger in line with the balance shown at the bookmaker.

    Any non-zero difference is written as a RECONCILIATION entry (so small rounding drift never
    accumulates); a difference larger than ``tolerance`` is additionally *flagged*: logged as a
    warning and written to the audit log. The result is also stored under
    ``system_state["last_reconciliation"]`` for the dashboard.
    """
    when = at or utcnow()
    observed = quantize_money(D(observed_balance))
    tol = abs(D(tolerance))
    ledger_balance = current_balance(session)
    diff = observed - ledger_balance
    flagged = abs(diff) > tol
    entry_id: int | None = None
    if diff != 0:
        entry = _append(
            session,
            kind=LedgerKind.RECONCILIATION,
            amount=diff,
            note=f"reconciliation: bookmaker {observed}, ledger {ledger_balance}",
            at=when,
        )
        entry_id = entry.id
    details = {
        "at": when.isoformat(),
        "observed_balance": str(observed),
        "ledger_balance": str(ledger_balance),
        "difference": str(diff),
        "tolerance": str(tol),
        "flagged": flagged,
    }
    if flagged:
        logger.warning(
            "balance mismatch: bookmaker %s vs ledger %s (difference %s > tolerance %s)",
            observed,
            ledger_balance,
            diff,
            tol,
        )
        state.audit(session, actor=actor, action="reconciliation_mismatch", details=details)
    state.set_state(session, state.LAST_RECONCILIATION, details, actor=actor)
    return ReconcileResult(
        observed_balance=observed,
        ledger_balance=ledger_balance,
        difference=diff,
        tolerance=tol,
        flagged=flagged,
        entry_id=entry_id,
        at=when,
    )


def bet_stake_tax(bet: Bet, settings: TaxSource) -> Decimal:
    """Stake tax actually charged on a bet.

    Priority: ``bets.extra["stake_tax_rate"]`` recorded at placement, then a positive ``bets.tax``,
    else the current settings (``bets.tax`` of 0 is ambiguous - unset or promo - so a promo bet
    should record ``stake_tax_rate = "0"``; :func:`bet_money_fields` does that).
    """
    stake = quantize_money(D(bet.stake))
    rate = (bet.extra or {}).get(STAKE_TAX_RATE_KEY)
    if rate is not None:
        return quantize_money(stake * D(rate))
    if bet.tax is not None and D(bet.tax) > 0:
        return quantize_money(D(bet.tax))
    return stake_tax(stake, settings)


def bet_money_fields(stake: Decimal, odds: Decimal, settings: TaxSource) -> dict[str, object]:
    """Column values for a new ``bets`` row: ``tax``, ``potential_payout`` and the ``extra`` entry
    recording the stake tax rate, e.g. ``Bet(stake=s, **bet_money_fields(s, odds, settings), ...)``
    (merge ``extra`` with any other extra fields you set)."""
    model = TaxModel.from_settings(settings)
    breakdown = payout_breakdown(stake, odds, model)
    return {
        "tax": breakdown.stake_tax,
        "potential_payout": breakdown.net_payout,
        "extra": {STAKE_TAX_RATE_KEY: str(model.stake_tax_rate)},
    }


def _outcome_kind(outcome: SettlementOutcome | SettleOutcome | str) -> str:
    kind = (outcome.value if isinstance(outcome, SettlementOutcome) else str(outcome)).strip().lower()
    if kind not in ("won", "lost", "void", "push"):
        raise ValueError(f"unknown settlement outcome {outcome!r} (expected won / lost / void / push)")
    return kind


def settle_amounts(
    bet: Bet, outcome: SettlementOutcome | SettleOutcome | str, settings: TaxSource
) -> tuple[Decimal, Decimal]:
    """``(payout, pnl)`` of a settled bet, consistent with :func:`cs2bot.engine.ev.net_payout`.

    * ``won``: payout = (stake - stake tax) x odds minus the winnings tax; pnl = payout - stake;
    * ``lost``: (0, -stake);
    * ``void`` / ``push``: the gross stake is refunded: (stake, 0) - or, when
      ``betclic.void_refunds_stake_tax`` is off, (stake - stake tax, -stake tax).

    Odds are ``placed_odds`` (falling back to ``requested_odds``). ``SettlementOutcome.UNKNOWN`` (or
    any other value) raises ``ValueError``: an undecided bet has no amounts.
    """
    stake = quantize_money(D(bet.stake))
    kind = _outcome_kind(outcome)
    if kind == "won":
        odds = bet.placed_odds if bet.placed_odds is not None else bet.requested_odds
        breakdown = payout_breakdown(stake, D(odds), settings, tax=bet_stake_tax(bet, settings))
        return breakdown.net_payout, breakdown.net_payout - stake
    if kind == "lost":
        return _ZERO, -stake
    refund = refund_amount(stake, settings, tax=bet_stake_tax(bet, settings))
    return refund, refund - stake


_SETTLED_STATUS: dict[str, BetStatus] = {
    "won": BetStatus.WON,
    "lost": BetStatus.LOST,
    "void": BetStatus.VOID,
    "push": BetStatus.VOID,
}
#: ``bets.extra`` key holding the settlement outcome applied by :func:`settle_bet` ("won" / "lost" /
#: "void" / "push"; ``bets.status`` cannot tell a push from a void).
SETTLED_OUTCOME_KEY = "settled_outcome"


def settle_bet(
    session: Session,
    bet: Bet,
    outcome: SettlementOutcome | SettleOutcome | str,
    settings: TaxSource,
    *,
    at: datetime | None = None,
) -> tuple[Decimal, Decimal]:
    """Apply a settlement outcome to a bet: status, ``payout``, ``pnl``, ``settled_at`` and the
    ledger entries, in one step. Returns ``(payout, pnl)``.

    * real bets: WON -> PAYOUT entry; LOST -> nothing (the stake was debited at placement, and is
      debited now if it never was); VOID / PUSH -> REFUND entry and status ``VOID``;
    * dry-run bets keep status ``DRY_RUN`` and never touch the ledger; the hypothetical ``payout`` /
      ``pnl`` / ``settled_at`` are stored for the dashboard (``compute_state`` ignores them);
    * idempotent: settling an already settled bet with the same outcome changes nothing; a different
      outcome raises :class:`LedgerConflictError` (re-grading must be done by hand);
    * only bets the bookmaker holds can be settled: PENDING / FAILED / CANCELLED bets raise
      ``ValueError`` (PLACING - an unconfirmed placement - must be reconciled to PLACED first).

    Flushes, never commits. ``SettlementOutcome.UNKNOWN`` raises ``ValueError``.
    """
    kind = _outcome_kind(outcome)
    when = at or utcnow()
    if when.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    payout, pnl = settle_amounts(bet, kind, settings)
    dry = is_dry_run(bet)
    previous = (bet.extra or {}).get(SETTLED_OUTCOME_KEY)
    if bet.status.is_settled or (dry and previous is not None):
        same = previous == kind or (previous is None and bet.status is _SETTLED_STATUS[kind])
        if not same:
            raise LedgerConflictError(
                f"bet {bet.id} is already settled as {previous or bet.status.value}; refusing to "
                f"re-settle it as {kind}"
            )
        return (
            quantize_money(D(bet.payout)) if bet.payout is not None else payout,
            quantize_money(D(bet.pnl)) if bet.pnl is not None else pnl,
        )
    if not dry and bet.status is not BetStatus.PLACED:
        raise ValueError(f"bet {bet.id} has status {bet.status.value}; only PLACED bets can be settled")
    bet.payout, bet.pnl, bet.settled_at = payout, pnl, when
    bet.extra = {**(bet.extra or {}), SETTLED_OUTCOME_KEY: kind}
    if not dry:
        if kind in ("void", "push"):
            if _bet_entry(session, _persisted_id(session, bet), LedgerKind.STAKE) is None:
                record_stake(session, bet)
            record_refund(session, bet, amount=payout, at=when)
        elif kind == "won":
            record_payout(session, bet, payout, at=when)
        else:
            record_stake(session, bet)  # no-op when the stake was debited at placement
        bet.status = _SETTLED_STATUS[kind]
    session.flush()
    return payout, pnl
