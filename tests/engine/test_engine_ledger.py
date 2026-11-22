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
