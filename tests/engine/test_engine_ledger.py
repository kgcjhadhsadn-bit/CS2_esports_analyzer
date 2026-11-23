"""Ledger arithmetic, idempotency, dry runs, reconciliation and settlement amounts."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, BetclicSettings, Settings
from cs2bot.db import state
from cs2bot.db.models import AuditLog, BankrollLedger, Bet
from cs2bot.domain.enums import BetStatus, LedgerKind
from cs2bot.domain.results import SettlementOutcome
from cs2bot.engine import ledger
from cs2bot.engine.ev import TaxModel, net_payout
from tests.engine.engine_factories import T0, add_bet


def entries(session: Session) -> list[BankrollLedger]:
    return list(session.scalars(select(BankrollLedger).order_by(BankrollLedger.id)))


def test_ensure_initialized_is_idempotent(session: Session, settings: Settings) -> None:
    assert ledger.current_balance(session) == Decimal("0.00")
    first = ledger.ensure_initialized(session, settings, at=T0)
    assert first is not None and first.kind is LedgerKind.DEPOSIT
    assert first.amount == Decimal("1000.00") and first.balance_after == Decimal("1000.00")
    assert first.ts == T0
    assert ledger.ensure_initialized(session, settings) is None
    assert ledger.current_balance(session) == Decimal("1000.00")
    assert len(entries(session)) == 1


def test_ensure_initialized_honours_dashboard_override(session: Session, settings: Settings) -> None:
    state.set_config_overrides(session, {"bankroll": {"starting_bankroll": "2500"}}, actor="test")
    ledger.ensure_initialized(session, settings)
    assert ledger.current_balance(session) == Decimal("2500.00")


def test_ensure_initialized_with_bankroll_settings(session: Session) -> None:
    ledger.ensure_initialized(session, BankrollSettings(starting_bankroll=Decimal("321.456")))
    assert ledger.current_balance(session) == Decimal("321.46")


def test_stake_and_win(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="100", odds="2.00", placed_at=T0)
    e1 = ledger.record_stake(session, bet)
    assert e1 is not None and e1.amount == Decimal("-100.00") and e1.balance_after == Decimal("900.00")
    assert e1.bet_id == bet.id and e1.ts == T0  # defaults to the placement time
    assert ledger.record_stake(session, bet) is e1  # idempotent
    payout, pnl = ledger.settle_amounts(bet, "won", settings)
    assert (payout, pnl) == (Decimal("176.00"), Decimal("76.00"))
    e2 = ledger.record_payout(session, bet, payout)
    assert e2 is not None and e2.balance_after == Decimal("1076.00")
    assert ledger.record_payout(session, bet, payout) is e2
    assert ledger.current_balance(session) == Decimal("1076.00")
    # the running balance chains: every balance_after = previous + amount
    rows = entries(session)
    for prev, cur in pairwise(rows):
        assert cur.balance_after == prev.balance_after + cur.amount


def test_stake_and_loss(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="50")
    ledger.record_stake(session, bet)
    payout, pnl = ledger.settle_amounts(bet, "lost", settings)
    assert (payout, pnl) == (Decimal("0.00"), Decimal("-50.00"))
    assert ledger.record_payout(session, bet, payout) is None
    assert ledger.current_balance(session) == Decimal("950.00")


@pytest.mark.parametrize("outcome", ["void", "push", "VOID"])
def test_void_and_push_refund_the_full_gross_stake(
    session: Session, settings: Settings, outcome: str
) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="40")
    ledger.record_stake(session, bet)
    assert ledger.current_balance(session) == Decimal("960.00")
    assert ledger.settle_amounts(bet, outcome, settings) == (Decimal("40.00"), Decimal("0.00"))
    refund = ledger.record_refund(session, bet)
    assert refund is not None and refund.amount == Decimal("40.00") and refund.kind is LedgerKind.REFUND
    assert ledger.record_refund(session, bet) is refund
    assert ledger.current_balance(session) == Decimal("1000.00")


def test_refund_without_debited_stake_writes_nothing(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="40", status=BetStatus.FAILED)
    assert ledger.record_refund(session, bet) is None
    assert ledger.current_balance(session) == Decimal("1000.00")


def test_dry_run_bets_never_touch_the_ledger(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    dry = add_bet(session, stake="25", dry_run=True, status=BetStatus.DRY_RUN)
    flagged_only = add_bet(session, stake="25", dry_run=False, status=BetStatus.DRY_RUN)
    for bet in (dry, flagged_only):
        assert ledger.record_stake(session, bet) is None
        assert ledger.record_payout(session, bet, Decimal("44")) is None
        assert ledger.record_refund(session, bet) is None
    assert len(entries(session)) == 1 and ledger.current_balance(session) == Decimal("1000.00")


def test_payout_records_a_missing_stake_first(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="100", odds="2.00")
    ledger.record_payout(session, bet, Decimal("176.00"))
    kinds = [(e.kind, e.amount) for e in entries(session)[1:]]
    assert kinds == [(LedgerKind.STAKE, Decimal("-100.00")), (LedgerKind.PAYOUT, Decimal("176.00"))]
    assert ledger.current_balance(session) == Decimal("1076.00")


def test_stake_requires_a_positive_amount(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="0")
    with pytest.raises(ValueError, match="non-positive"):
        ledger.record_stake(session, bet)
    with pytest.raises(ValueError):
        ledger.record_payout(session, add_bet(session), Decimal("-1"))


def test_adjustments_deposits_withdrawals(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    adj = ledger.record_adjustment(session, Decimal("-3.333"), "fee")
    assert adj.amount == Decimal("-3.33") and adj.kind is LedgerKind.ADJUSTMENT and adj.note == "fee"
    dep = ledger.record_deposit(session, Decimal("200"))
    wd = ledger.record_withdrawal(session, Decimal("50"))
    assert (dep.amount, wd.amount) == (Decimal("200.00"), Decimal("-50.00"))
    assert ledger.current_balance(session) == Decimal("1146.67")
    with pytest.raises(ValueError):
        ledger.record_deposit(session, Decimal("0"))
    with pytest.raises(ValueError):
        ledger.record_withdrawal(session, Decimal("-5"))


def test_balance_follows_insertion_order_not_timestamps(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings, at=T0)
    ledger.record_adjustment(session, Decimal("10"), "late", at=T0 + timedelta(days=1))
    back = ledger.record_adjustment(session, Decimal("5"), "back-dated", at=T0 - timedelta(days=3))
    assert back.balance_after == Decimal("1015.00")
    assert ledger.current_balance(session) == Decimal("1015.00")


def test_reconcile(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    tol = Decimal("0.05")
    same = ledger.reconcile(session, Decimal("1000.00"), tolerance=tol)
    assert not same.flagged and same.entry_id is None and same.difference == 0
    assert state.get_state(session, state.LAST_RECONCILIATION)["flagged"] is False

    small = ledger.reconcile(session, Decimal("1000.03"), tolerance=tol)
    assert not small.flagged and small.entry_id is not None  # rounding drift is synced, not flagged
    assert ledger.current_balance(session) == Decimal("1000.03")

    big = ledger.reconcile(session, Decimal("987.69"), tolerance=tol, actor="executor")
    assert big.flagged and big.difference == Decimal("-12.34") and big.ledger_balance == Decimal("1000.03")
    row = session.get(BankrollLedger, big.entry_id)
    assert row is not None and row.kind is LedgerKind.RECONCILIATION and row.amount == Decimal("-12.34")
    assert ledger.current_balance(session) == Decimal("987.69")
    last = state.get_state(session, state.LAST_RECONCILIATION)
    assert last["flagged"] is True and last["difference"] == "-12.34"
    audits = session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "reconciliation_mismatch")
    )
    assert audits == 1


def test_settle_amounts_consistent_with_net_payout(session: Session, settings: Settings) -> None:
    cases = [("10", "1.85"), ("7.30", "1.85"), ("123.45", "2.37"), ("1000", "3.00"), ("2.00", "14.5")]
    for stake, odds in cases:
        bet = add_bet(session, stake=stake, odds=odds)
        payout, pnl = ledger.settle_amounts(bet, "won", settings)
        assert payout == net_payout(Decimal(stake), Decimal(odds), settings)
        assert pnl == payout - Decimal(stake).quantize(Decimal("0.01"))
    big = add_bet(session, stake="1000", odds="3.00")
    assert ledger.settle_amounts(big, "won", settings) == (Decimal("2376.00"), Decimal("1376.00"))
    with pytest.raises(ValueError, match="unknown settlement outcome"):
        ledger.settle_amounts(big, "half-won", settings)


def test_settle_amounts_uses_placed_then_requested_odds(settings: Settings) -> None:
    bet = Bet(stake=Decimal("100"), requested_odds=Decimal("2.10"), placed_odds=Decimal("2.00"))
    assert ledger.settle_amounts(bet, "won", settings)[0] == Decimal("176.00")
    bet.placed_odds = None
    assert ledger.settle_amounts(bet, "won", settings)[0] == Decimal("184.80")  # 88 * 2.10, tax unset


def test_settle_amounts_respects_the_tax_charged_at_placement(session: Session, settings: Settings) -> None:
    # placed during a "bez podatku" promo: no stake tax, recorded in extra
    promo = add_bet(session, stake="100", odds="2.00", tax="0", extra={ledger.STAKE_TAX_RATE_KEY: "0"})
    assert ledger.settle_amounts(promo, "won", settings)[0] == Decimal("200.00")
    # an explicit positive bets.tax is honoured even if the settings changed since
    charged = add_bet(session, stake="100", odds="2.00", tax="12.00")
    promo_settings = Settings(betclic=BetclicSettings(tax_free_promo=True))
    assert ledger.settle_amounts(charged, "won", promo_settings)[0] == Decimal("176.00")
    # neither recorded: the current settings decide
    unknown = add_bet(session, stake="100", odds="2.00", tax="0")
    assert ledger.settle_amounts(unknown, "won", settings)[0] == Decimal("176.00")


def test_bet_money_fields(settings: Settings) -> None:
    fields = ledger.bet_money_fields(Decimal("100"), Decimal("2.00"), settings)
    assert fields == {
        "tax": Decimal("12.00"),
        "potential_payout": Decimal("176.00"),
        "extra": {"stake_tax_rate": "0.12"},
    }
    promo = ledger.bet_money_fields(
        Decimal("100"), Decimal("2.00"), TaxModel(Decimal("0"), Decimal("2280"), Decimal("0.1"))
    )
    assert promo["tax"] == Decimal("0.00") and promo["extra"] == {"stake_tax_rate": "0"}
    bet = Bet(stake=Decimal("100"), requested_odds=Decimal("2.00"), **promo)  # type: ignore[arg-type]
    assert ledger.settle_amounts(bet, "won", settings)[0] == Decimal("200.00")


# ------------------------------------------------------------------ review round 1 additions


NET_REFUND = Settings(betclic=BetclicSettings(void_refunds_stake_tax=False))


def test_void_refund_without_the_stake_tax(session: Session, settings: Settings) -> None:
    """betclic.void_refunds_stake_tax = False: a void returns stake - stake tax and costs the tax."""
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="40", odds="2.00")
    ledger.record_stake(session, bet)
    payout, pnl = ledger.settle_amounts(bet, "void", NET_REFUND)
    assert (payout, pnl) == (Decimal("35.20"), Decimal("-4.80"))
    assert ledger.settle_amounts(bet, SettlementOutcome.PUSH, NET_REFUND) == (payout, pnl)
    refund = ledger.record_refund(session, bet, amount=payout)
    assert refund is not None and refund.amount == Decimal("35.20")
    assert ledger.current_balance(session) == Decimal("995.20")
    # a bet placed during a tax-free promo paid no stake tax, so nothing is kept
    promo = add_bet(session, stake="40", tax="0", extra={ledger.STAKE_TAX_RATE_KEY: "0"})
    assert ledger.settle_amounts(promo, "void", NET_REFUND) == (Decimal("40.00"), Decimal("0.00"))


def test_refund_amount_is_bounded_by_the_debited_stake(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="40")
    ledger.record_stake(session, bet)
    with pytest.raises(ValueError, match="outside"):
        ledger.record_refund(session, bet, amount=Decimal("40.01"))
    with pytest.raises(ValueError, match="outside"):
        ledger.record_refund(session, bet, amount=Decimal("-1"))
    assert ledger.record_refund(session, bet, amount=Decimal("0")) is None
    assert ledger.current_balance(session) == Decimal("960.00")


def test_a_bet_is_never_both_paid_out_and_refunded(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    won = add_bet(session, stake="100", odds="2.00")
    ledger.record_stake(session, won)
    ledger.record_payout(session, won, Decimal("176.00"))
    with pytest.raises(ledger.LedgerConflictError, match="already paid out"):
        ledger.record_refund(session, won)
    void = add_bet(session, stake="50")
    ledger.record_stake(session, void)
    ledger.record_refund(session, void)
    with pytest.raises(ledger.LedgerConflictError, match="already refunded"):
        ledger.record_payout(session, void, Decimal("88.00"))
    assert ledger.current_balance(session) == Decimal("1076.00")  # nothing was credited twice
    # a second, different payout for the same bet is ignored (first one wins), never added
    again = ledger.record_payout(session, won, Decimal("999.00"))
    assert again is not None and again.amount == Decimal("176.00")
    assert ledger.current_balance(session) == Decimal("1076.00")


def test_settle_amounts_accepts_settlement_outcomes(session: Session, settings: Settings) -> None:
    bet = add_bet(session, stake="100", odds="2.00")
    assert ledger.settle_amounts(bet, SettlementOutcome.WON, settings) == (
        Decimal("176.00"),
        Decimal("76.00"),
    )
    assert ledger.settle_amounts(bet, SettlementOutcome.LOST, settings) == (
        Decimal("0.00"),
        Decimal("-100.00"),
    )
    assert ledger.settle_amounts(bet, SettlementOutcome.PUSH, settings) == (
        Decimal("100.00"),
        Decimal("0.00"),
    )
    with pytest.raises(ValueError, match="unknown settlement outcome"):
        ledger.settle_amounts(bet, SettlementOutcome.UNKNOWN, settings)


@pytest.mark.parametrize(
    ("outcome", "status", "payout", "pnl", "balance", "kinds"),
    [
        ("won", BetStatus.WON, "176.00", "76.00", "1076.00", [LedgerKind.STAKE, LedgerKind.PAYOUT]),
        ("lost", BetStatus.LOST, "0.00", "-100.00", "900.00", [LedgerKind.STAKE]),
        ("void", BetStatus.VOID, "100.00", "0.00", "1000.00", [LedgerKind.STAKE, LedgerKind.REFUND]),
        ("push", BetStatus.VOID, "100.00", "0.00", "1000.00", [LedgerKind.STAKE, LedgerKind.REFUND]),
    ],
)
def test_settle_bet(
    session: Session,
    settings: Settings,
    outcome: str,
    status: BetStatus,
    payout: str,
    pnl: str,
    balance: str,
    kinds: list[LedgerKind],
) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="100", odds="2.00", placed_at=T0)
    ledger.record_stake(session, bet)
    when = T0 + timedelta(hours=3)
    assert ledger.settle_bet(session, bet, outcome, settings, at=when) == (Decimal(payout), Decimal(pnl))
    assert (bet.status, bet.payout, bet.pnl, bet.settled_at) == (status, Decimal(payout), Decimal(pnl), when)
    assert bet.extra[ledger.SETTLED_OUTCOME_KEY] == outcome
    assert [e.kind for e in entries(session)[1:]] == kinds
    assert ledger.current_balance(session) == Decimal(balance)
    # idempotent: the same outcome again changes nothing
    assert ledger.settle_bet(session, bet, outcome, settings) == (Decimal(payout), Decimal(pnl))
    assert bet.settled_at == when and [e.kind for e in entries(session)[1:]] == kinds
    # a different outcome is a re-grade: refused
    other = "lost" if outcome != "lost" else "won"
    with pytest.raises(ledger.LedgerConflictError, match="already settled"):
        ledger.settle_bet(session, bet, other, settings)
    assert ledger.current_balance(session) == Decimal(balance)


def test_settle_bet_debits_a_stake_that_was_never_recorded(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    lost = add_bet(session, stake="30")
    ledger.settle_bet(session, lost, SettlementOutcome.LOST, settings, at=T0)
    void = add_bet(session, stake="20")
    ledger.settle_bet(session, void, SettlementOutcome.VOID, settings, at=T0)
    assert ledger.current_balance(session) == Decimal("970.00")
    assert [e.kind for e in entries(session)[1:]] == [LedgerKind.STAKE, LedgerKind.STAKE, LedgerKind.REFUND]


def test_settle_bet_with_net_refunds(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="40")
    ledger.record_stake(session, bet)
    assert ledger.settle_bet(session, bet, "void", NET_REFUND, at=T0) == (Decimal("35.20"), Decimal("-4.80"))
    assert bet.status is BetStatus.VOID and bet.pnl == Decimal("-4.80")
    assert ledger.current_balance(session) == Decimal("995.20")


def test_settle_bet_rejects_bets_the_bookmaker_does_not_hold(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    for status in (BetStatus.PENDING, BetStatus.PLACING, BetStatus.FAILED, BetStatus.CANCELLED):
        bet = add_bet(session, stake="10", status=status)
        with pytest.raises(ValueError, match="only PLACED bets"):
            ledger.settle_bet(session, bet, "won", settings)
        assert bet.status is status and bet.payout is None and bet.settled_at is None
    placed = add_bet(session, stake="10")
    with pytest.raises(ValueError, match="unknown settlement outcome"):
        ledger.settle_bet(session, placed, SettlementOutcome.UNKNOWN, settings)
    with pytest.raises(ValueError, match="timezone"):
        ledger.settle_bet(session, placed, "won", settings, at=T0.replace(tzinfo=None))
    assert placed.status is BetStatus.PLACED and len(entries(session)) == 1


def test_settle_bet_dry_run_keeps_status_and_ledger(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    dry = add_bet(session, stake="100", odds="2.00", dry_run=True, status=BetStatus.DRY_RUN)
    assert ledger.settle_bet(session, dry, "won", settings, at=T0) == (Decimal("176.00"), Decimal("76.00"))
    assert dry.status is BetStatus.DRY_RUN and dry.pnl == Decimal("76.00") and dry.settled_at == T0
    assert len(entries(session)) == 1 and ledger.current_balance(session) == Decimal("1000.00")
    assert ledger.settle_bet(session, dry, "won", settings) == (Decimal("176.00"), Decimal("76.00"))
    with pytest.raises(ledger.LedgerConflictError):
        ledger.settle_bet(session, dry, "lost", settings)
