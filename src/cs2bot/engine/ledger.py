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

:func:`apply_placement` applies the executor's ``PlacementResult`` to a bet (status through
:func:`cs2bot.domain.betting.bet_status_after_placement`, odds, tax, the STAKE entry) and
:func:`settle_bet` applies a settlement outcome (status, payout, pnl, ``settled_at`` and the ledger
entries); they are the recommended entry points for the betting and settlement services.

Reconciliation (:func:`reconcile`) compares the ledger with the balance shown by the bookmaker. Call
it when the two are expected to agree: after the results of all placements have been applied and
finished bets have been settled. A bet whose placement is still unconfirmed (``PLACING``) has no STAKE
entry yet, so the bookmaker's balance may legitimately be lower by exactly that stake: such a shortfall
is deferred, not booked. Any other difference beyond the tolerance is booked (the bookmaker is the
source of truth for cash), flagged, and pauses trading until the owner has looked at it - every stake
and limit is a fraction of this balance, so unexplained money must never size bets silently.
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
from cs2bot.domain.betting import PlacementResult, bet_status_after_placement
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
#: ``bets.extra`` keys written by :func:`apply_placement`: the executor's ``PlacementStatus`` value and,
#: for an unconfirmed placement, when it was attempted (ISO timestamp, UTC).
PLACEMENT_STATUS_KEY = "placement_status"
PLACEMENT_ATTEMPTED_AT_KEY = "placement_attempted_at"


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
    flagged: bool  # |difference| > tolerance and not deferred: needs the owner's attention
    entry_id: int | None  # RECONCILIATION entry written to bring the ledger in line (None if none)
    at: datetime
    deferred: bool = False  # the shortfall may be an unconfirmed placement: nothing written, retry later
    paused: bool = False  # this call paused trading (flagged mismatch with pause_on_flag)


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
    session: Session,
    settings: Settings | BankrollSettings,
    *,
    at: datetime | None = None,
    opening_balance: Decimal | None = None,
) -> BankrollLedger | None:
    """If the ledger is empty, write the opening DEPOSIT; else do nothing.

    The amount is ``opening_balance`` when given - the balance actually read at the bookmaker
    (``executor.get_balance()``), the right number to start a real account from - and otherwise
    ``bankroll.starting_bankroll`` (with a full :class:`Settings`, dashboard overrides are honoured).
    Starting from the configured value while the real account holds something else is corrected, and
    flagged, by the first :func:`reconcile`. Returns the created entry or ``None``.
    """
    if session.scalar(select(BankrollLedger.id).limit(1)) is not None:
        return None
    if opening_balance is not None:
        observed = D(opening_balance)
        if not observed.is_finite() or observed < 0:
            raise ValueError(f"opening balance must be a non-negative number, got {opening_balance!r}")
        amount = quantize_money(observed)
        note = "initial bankroll (balance observed at the bookmaker)"
    else:
        amount = quantize_money(D(_bankroll_settings(session, settings).starting_bankroll))
        note = "initial bankroll (bankroll.starting_bankroll)"
    entry = BankrollLedger(
        ts=at or utcnow(), kind=LedgerKind.DEPOSIT, amount=amount, balance_after=amount, note=note
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


def _unconfirmed_stakes(session: Session) -> Decimal:
    """Stakes of real bets that may or may not exist at the bookmaker and are not debited in the
    ledger: ``PLACING`` (the executor could not confirm them) and ``PENDING`` (the row was committed
    but no result was recorded - normally not sent yet, after a crash possibly sent)."""
    rows = session.execute(
        select(Bet.id, Bet.stake).where(
            Bet.status.in_([BetStatus.PENDING, BetStatus.PLACING]), Bet.dry_run.is_(False)
        )
    ).all()
    if not rows:
        return _ZERO
    debited = set(
        session.scalars(
            select(BankrollLedger.bet_id).where(
                BankrollLedger.kind == LedgerKind.STAKE, BankrollLedger.bet_id.in_([r[0] for r in rows])
            )
        )
    )
    return sum((quantize_money(D(stake)) for bet_id, stake in rows if bet_id not in debited), _ZERO)


def reconcile(
    session: Session,
    observed_balance: Decimal,
    *,
    tolerance: Decimal,
    actor: str = "system",
    at: datetime | None = None,
    pause_on_flag: bool = True,
) -> ReconcileResult:
    """Bring the ledger in line with the balance shown at the bookmaker.

    * Any non-zero difference is written as a RECONCILIATION entry (so small rounding drift never
      accumulates) - the ledger is the bankroll every stake and limit is sized from, and the bookmaker
      is the source of truth for cash (deposits and withdrawals reach the ledger this way).
    * A difference larger than ``tolerance`` is additionally *flagged*: logged as a warning, written
      to the audit log and - unless ``pause_on_flag`` is off - trading is paused
      (``db.state.set_paused``) with the numbers in the reason. Money appeared or disappeared that the
      bot cannot explain (a deposit or withdrawal, a bet it does not know about, a balance the
      executor misread): sizes and limits are fractions of this number, so the owner confirms it and
      resumes from the dashboard. An existing pause (and its reason) is left untouched.
    * Exception - *deferred*: while placements are unconfirmed (``PLACING`` / ``PENDING`` bets, not
      debited yet) a shortfall of up to their total stake is exactly what an accepted-but-unconfirmed
      bet looks like.
      Nothing is written and nothing is flagged; ``ReconcileResult.deferred`` tells the caller to
      resolve those bets against the bookmaker's bet history first (``apply_placement``) and
      reconcile again. ``bankroll.compute_state`` already keeps their stakes out of the free balance.

    Call it when the ledger is expected to agree with the bookmaker: after every placement result has
    been applied and finished bets have been settled (a payout the bookmaker has already credited but
    the ledger has not yet recorded would be booked twice otherwise, and corrected - and flagged - by
    the next reconciliation). The result is stored under ``system_state["last_reconciliation"]``.
    Raises ``ValueError`` for a balance that is not a finite number. Flushes, never commits.
    """
    when = at or utcnow()
    raw = D(observed_balance)
    if not raw.is_finite():
        raise ValueError(f"observed balance must be a finite number, got {observed_balance!r}")
    observed = quantize_money(raw)
    tol = abs(D(tolerance))
    ledger_balance = current_balance(session)
    diff = observed - ledger_balance
    unconfirmed = _unconfirmed_stakes(session) if diff < -tol else _ZERO
    deferred = unconfirmed > 0 and -diff <= unconfirmed + tol
    flagged = abs(diff) > tol and not deferred
    entry_id: int | None = None
    if diff != 0 and not deferred:
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
        "deferred": deferred,
        "unconfirmed_stakes": str(unconfirmed),
    }
    paused = False
    if deferred:
        logger.info(
            "balance check deferred: bookmaker %s vs ledger %s; %s PLN of unconfirmed placements may "
            "explain the difference %s",
            observed,
            ledger_balance,
            unconfirmed,
            diff,
        )
    if flagged:
        logger.warning(
            "balance mismatch: bookmaker %s vs ledger %s (difference %s > tolerance %s)",
            observed,
            ledger_balance,
            diff,
            tol,
        )
        state.audit(session, actor=actor, action="reconciliation_mismatch", details=details)
        if pause_on_flag and not state.get_pause(session)[0]:
            state.set_paused(
                session,
                True,
                actor=actor,
                reason=(
                    f"balance mismatch: Betclic shows {observed} PLN, the ledger expected "
                    f"{ledger_balance} PLN (difference {diff:+} PLN). The ledger now follows Betclic - "
                    "check the account for deposits, withdrawals or bets the bot does not know, then "
                    "resume from the dashboard"
                ),
            )
            paused = True
    details["paused"] = paused
    state.set_state(session, state.LAST_RECONCILIATION, details, actor=actor)
    return ReconcileResult(
        observed_balance=observed,
        ledger_balance=ledger_balance,
        difference=diff,
        tolerance=tol,
        flagged=flagged,
        entry_id=entry_id,
        at=when,
        deferred=deferred,
        paused=paused,
    )


def bet_stake_tax(bet: Bet, settings: TaxSource) -> Decimal:
    """Stake tax actually charged on a bet.

    Priority: ``bets.extra["stake_tax_rate"]`` recorded at placement, then a positive ``bets.tax``,
    else the current settings (``bets.tax`` of 0 is ambiguous - unset or promo - so a promo bet
    should record ``stake_tax_rate = "0"``; :func:`bet_money_fields` does that).
    """
    stake = quantize_money(D(bet.stake))
    raw = (bet.extra or {}).get(STAKE_TAX_RATE_KEY)
    if raw is not None:
        try:
            rate = D(raw)
        except (ArithmeticError, ValueError):
            rate = Decimal("NaN")
        if rate.is_finite() and Decimal(0) <= rate < Decimal(1):
            return quantize_money(stake * rate)
        logger.warning("bet %s: ignoring the unreadable stored stake tax rate %r", bet.id, raw)
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


def _attempted_at(extra: dict[str, object]) -> datetime | None:
    """The stored time of an unconfirmed placement attempt (``None`` if absent or unreadable)."""
    raw = extra.get(PLACEMENT_ATTEMPTED_AT_KEY)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def apply_placement(session: Session, bet: Bet, result: PlacementResult, settings: TaxSource) -> BetStatus:
    """Apply the executor's answer to a ``bets`` row and the ledger. Returns the new ``bets.status``.

    The status comes from :func:`cs2bot.domain.betting.bet_status_after_placement`:

    * ``PLACED``: ``placed_odds`` (the odds the bookmaker accepted, else the requested ones),
      ``placed_at`` (``result.at``; for a bet confirmed after an unconfirmed attempt, the time of that
      attempt - the bet counts for the day it was really placed), ``external_bet_id``, ``tax`` /
      ``potential_payout`` at the placed odds and the stake tax rate in ``extra``; the gross stake is
      debited (:func:`record_stake`). A row that was
      created as a dry run but was really placed is turned into a real bet (and logged as an error):
      money left the account, so the ledger and the limits must see it;
    * ``DRY_RUN``: the row is marked ``dry_run`` with the simulated odds; the ledger is not touched;
    * ``PLACING`` (the executor could not confirm the bet, ``PlacementStatus.UNCERTAIN``): nothing is
      debited - the stake stays *reserved* (``bankroll.compute_state`` subtracts it from the free
      balance and counts it as exposure) until reconciliation against the bookmaker's bet history
      calls this function again with a ``PLACED`` or a failure result. The attempt time is kept in
      ``extra["placement_attempted_at"]``; such a bet must never be retried automatically;
    * ``FAILED`` (anything else): no bet exists at the bookmaker; a stake that was already debited
      (not expected) is refunded in full.

    Only bets that are still ``PENDING`` or ``PLACING`` take a result. Replaying a result that leads
    to the status the bet already has is a no-op; any other transition (e.g. a failure for a bet that
    is already ``PLACED`` or settled) raises :class:`LedgerConflictError`. Flushes, never commits.
    Trading halts are NOT checked here: this only records what already happened at the bookmaker.
    """
    if result.at.tzinfo is None:
        raise ValueError("PlacementResult.at must be timezone-aware")
    new_status = bet_status_after_placement(result.status)
    bet_id = _persisted_id(session, bet)
    if bet.status not in (BetStatus.PENDING, BetStatus.PLACING):
        if bet.status is new_status:
            return new_status
        raise LedgerConflictError(
            f"bet {bet_id} is {bet.status.value}: the placement result {result.status.value!r} "
            "cannot be applied to it"
        )
    extra = {**(bet.extra or {}), PLACEMENT_STATUS_KEY: result.status.value}
    if result.screenshot_path:
        bet.screenshot_path = result.screenshot_path
    if new_status in (BetStatus.PLACED, BetStatus.DRY_RUN):
        odds = D(bet.requested_odds)
        if result.placed_odds is not None:
            reported = D(result.placed_odds)
            if reported.is_finite() and reported >= 1:
                odds = reported
            else:  # a real placement must be recorded whatever the executor read off the receipt
                logger.warning("bet %s: ignoring impossible placed odds %r", bet_id, result.placed_odds)
        fields = payout_breakdown(D(bet.stake), odds, settings)
        # a placement confirmed later (reconciliation) happened when it was attempted, not when it was found
        placed_at = _attempted_at(extra) or result.at
        bet.placed_odds, bet.placed_at = odds, placed_at
        bet.tax, bet.potential_payout = fields.stake_tax, fields.net_payout
        extra[STAKE_TAX_RATE_KEY] = str(TaxModel.from_settings(settings).stake_tax_rate)
        extra.pop(PLACEMENT_ATTEMPTED_AT_KEY, None)
        if result.external_bet_id:
            bet.external_bet_id = result.external_bet_id
        bet.error = None
    if new_status is BetStatus.PLACED:
        if is_dry_run(bet):
            logger.error("bet %s was created as a dry run but the executor reports a REAL placement", bet_id)
            bet.dry_run = False
        placed = bet.placed_odds
        if placed is not None and bet.min_acceptable_odds is not None and placed < D(bet.min_acceptable_odds):
            logger.warning(
                "bet %s was placed at %s, below its minimum acceptable odds %s",
                bet_id,
                bet.placed_odds,
                bet.min_acceptable_odds,
            )
        bet.status, bet.extra = BetStatus.PLACED, extra
        record_stake(session, bet, at=placed_at)
    elif new_status is BetStatus.DRY_RUN:
        debited = _bet_entry(session, bet_id, LedgerKind.STAKE) is not None
        if debited:  # never expected: give the money back before the row becomes a dry run
            record_refund(session, bet, at=result.at)
        bet.status, bet.dry_run, bet.extra = BetStatus.DRY_RUN, True, extra
    elif new_status is BetStatus.PLACING:
        extra.setdefault(PLACEMENT_ATTEMPTED_AT_KEY, result.at.isoformat())
        bet.status, bet.extra = BetStatus.PLACING, extra
        bet.error = result.message or "the bookmaker did not confirm the bet - reconcile before retrying"
    else:
        bet.status, bet.extra = BetStatus.FAILED, extra
        bet.error = result.message or f"placement failed: {result.status.value}"
        record_refund(session, bet, at=result.at)  # no-op unless a stake was debited
    session.flush()
    return new_status


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
    * idempotent: settling an already settled bet with the same outcome changes nothing (``void`` and
      ``push`` count as the same refund); a different outcome raises :class:`LedgerConflictError`
      (re-grading must be done by hand);
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
        if previous is None:
            same = bet.status is _SETTLED_STATUS[kind]
        else:  # a void and a push are the same refund (the bookmaker's history only says "void")
            same = previous == kind or {previous, kind} <= {"void", "push"}
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
