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
from cs2bot.domain.betting import PlacementResult, PlacementStatus
from cs2bot.domain.enums import BetStatus, LedgerKind
from cs2bot.domain.results import SettlementOutcome
from cs2bot.engine import ledger
from cs2bot.engine.ev import TaxModel, net_payout
from tests.engine.engine_factories import T0, add_bet, add_match


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


def test_ensure_initialized_from_the_balance_observed_at_the_bookmaker(
    session: Session, settings: Settings
) -> None:
    for bad in (Decimal("-1"), Decimal("NaN")):
        with pytest.raises(ValueError, match="opening balance"):
            ledger.ensure_initialized(session, settings, opening_balance=bad)
    entry = ledger.ensure_initialized(session, settings, at=T0, opening_balance=Decimal("437.129"))
    assert entry is not None and entry.amount == Decimal("437.13") and "observed" in (entry.note or "")
    assert ledger.current_balance(session) == Decimal("437.13")
    # the first reconciliation is clean: no flag, no pause
    first = ledger.reconcile(session, Decimal("437.13"), tolerance=Decimal("0.05"))
    assert not first.flagged and not first.paused and first.entry_id is None
    assert ledger.ensure_initialized(session, settings, opening_balance=Decimal("999")) is None


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


def test_void_and_push_are_the_same_refund_when_settling_twice(session: Session, settings: Settings) -> None:
    """The local resolver may say "push" where the bookmaker's history only says "void"."""
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="100", odds="2.00", placed_at=T0)
    ledger.record_stake(session, bet)
    ledger.settle_bet(session, bet, SettlementOutcome.PUSH, settings, at=T0)
    assert ledger.settle_bet(session, bet, SettlementOutcome.VOID, settings) == (
        Decimal("100.00"),
        Decimal("0.00"),
    )
    assert bet.extra[ledger.SETTLED_OUTCOME_KEY] == "push"  # the first outcome is kept
    assert [e.kind for e in entries(session)[1:]] == [LedgerKind.STAKE, LedgerKind.REFUND]
    with pytest.raises(ledger.LedgerConflictError):
        ledger.settle_bet(session, bet, "won", settings)


def test_unreadable_stored_tax_rate_falls_back(session: Session, settings: Settings) -> None:
    for bad in ("twelve", "NaN", "1.5", "-0.1"):
        bet = add_bet(session, stake="100", odds="2.00", tax="0", extra={ledger.STAKE_TAX_RATE_KEY: bad})
        assert ledger.bet_stake_tax(bet, settings) == Decimal("12.00")  # current settings
        assert ledger.settle_amounts(bet, "won", settings) == (Decimal("176.00"), Decimal("76.00"))


# ----------------------------------------------------------------------------------------------
# apply_placement: PlacementResult -> bets row + ledger
# ----------------------------------------------------------------------------------------------


def placement(status: PlacementStatus, **kw: object) -> PlacementResult:
    return PlacementResult(status=status, at=T0 + timedelta(minutes=5), **kw)


def test_apply_placement_placed(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="100", odds="2.00", status=BetStatus.PENDING, tax="0")
    bet.placed_odds = None
    result = placement(
        PlacementStatus.PLACED,
        placed_odds=Decimal("1.95"),
        external_bet_id="BC-123",
        screenshot_path="data/screenshots/1.png",
    )
    assert ledger.apply_placement(session, bet, result, settings) is BetStatus.PLACED
    assert bet.status is BetStatus.PLACED and bet.dry_run is False
    assert (bet.placed_odds, bet.placed_at) == (Decimal("1.95"), result.at)
    assert (bet.external_bet_id, bet.screenshot_path) == ("BC-123", "data/screenshots/1.png")
    # money fields follow the odds the bookmaker accepted: 12 tax, 88 x 1.95 = 171.60
    assert (bet.tax, bet.potential_payout) == (Decimal("12.00"), Decimal("171.60"))
    assert bet.extra[ledger.STAKE_TAX_RATE_KEY] == "0.12"
    assert bet.extra[ledger.PLACEMENT_STATUS_KEY] == "placed"
    stake_entry = entries(session)[-1]
    assert (stake_entry.kind, stake_entry.amount, stake_entry.ts) == (
        LedgerKind.STAKE,
        Decimal("-100.00"),
        result.at,
    )
    assert ledger.current_balance(session) == Decimal("900.00")
    # replaying the same result changes nothing
    assert ledger.apply_placement(session, bet, result, settings) is BetStatus.PLACED
    assert len(entries(session)) == 2
    # the stored rate is what settlement uses, whatever the settings say later
    promo = TaxModel(Decimal("0"), Decimal("2280"), Decimal("0.10"))
    assert ledger.settle_bet(session, bet, "won", promo, at=T0 + timedelta(hours=2)) == (
        Decimal("171.60"),
        Decimal("71.60"),
    )


def test_apply_placement_without_reported_odds_uses_the_requested_odds(
    session: Session, settings: Settings
) -> None:
    ledger.ensure_initialized(session, settings)
    for reported in (None, Decimal("0.5"), Decimal("0")):
        bet = add_bet(session, stake="10", odds="2.00", status=BetStatus.PENDING)
        result = placement(PlacementStatus.PLACED, placed_odds=reported)
        assert ledger.apply_placement(session, bet, result, settings) is BetStatus.PLACED
        assert bet.placed_odds == Decimal("2.00") and bet.potential_payout == Decimal("17.60")
    assert ledger.current_balance(session) == Decimal("970.00")  # every real placement was debited


def test_apply_placement_tax_free_promo(session: Session) -> None:
    promo = TaxModel(Decimal("0"), Decimal("2280"), Decimal("0.10"))
    ledger.ensure_initialized(session, BankrollSettings())
    bet = add_bet(session, stake="100", odds="2.00", status=BetStatus.PENDING)
    ledger.apply_placement(session, bet, placement(PlacementStatus.PLACED), promo)
    assert (bet.tax, bet.potential_payout) == (Decimal("0.00"), Decimal("200.00"))
    assert bet.extra[ledger.STAKE_TAX_RATE_KEY] == "0"
    # settled later with the normal 12 % settings: the promo rate recorded at placement wins
    normal = TaxModel(Decimal("0.12"), Decimal("2280"), Decimal("0.10"))
    assert ledger.settle_amounts(bet, "won", normal) == (Decimal("200.00"), Decimal("100.00"))


def test_apply_placement_dry_run_never_touches_the_ledger(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    # the row was created as a real bet but the executor ran in DRY_RUN mode
    bet = add_bet(session, stake="100", odds="2.00", status=BetStatus.PENDING, dry_run=False)
    result = placement(PlacementStatus.DRY_RUN_OK, placed_odds=Decimal("2.05"))
    assert ledger.apply_placement(session, bet, result, settings) is BetStatus.DRY_RUN
    assert bet.status is BetStatus.DRY_RUN and bet.dry_run is True and ledger.is_dry_run(bet)
    assert bet.placed_odds == Decimal("2.05") and bet.potential_payout == Decimal("180.40")
    assert len(entries(session)) == 1 and ledger.current_balance(session) == Decimal("1000.00")
    assert ledger.record_stake(session, bet) is None
    # paper settlement still works and still writes nothing
    assert ledger.settle_bet(session, bet, "won", settings, at=T0)[1] == Decimal("80.40")
    assert len(entries(session)) == 1


def test_apply_placement_uncertain_keeps_the_stake_reserved_until_reconciled(
    session: Session, settings: Settings
) -> None:
    from cs2bot.engine import bankroll

    ledger.ensure_initialized(session, settings, at=T0)
    add_match(session, match_id=5)
    bet = add_bet(session, stake="20", odds="2.00", status=BetStatus.PENDING, match_id=5, created_at=T0)
    uncertain = placement(PlacementStatus.UNCERTAIN, message="no receipt after 45 s")
    assert ledger.apply_placement(session, bet, uncertain, settings) is BetStatus.PLACING
    assert bet.status is BetStatus.PLACING and bet.error == "no receipt after 45 s"
    assert bet.placed_at is None and bet.extra[ledger.PLACEMENT_ATTEMPTED_AT_KEY] == uncertain.at.isoformat()
    assert len(entries(session)) == 1  # nothing debited ...
    state_now = bankroll.compute_state(session, settings, now=T0 + timedelta(minutes=10))
    assert state_now.reserved_stakes == Decimal("20.00")  # ... but the money is not free either
    assert state_now.balance == Decimal("980.00") and state_now.bankroll_reference == Decimal("1000.00")
    assert state_now.exposure_by_match == {5: Decimal("20.00")}
    # a second uncertain answer keeps the first attempt time
    again = PlacementResult(status=PlacementStatus.UNCERTAIN, at=T0 + timedelta(hours=1))
    ledger.apply_placement(session, bet, again, settings)
    assert bet.extra[ledger.PLACEMENT_ATTEMPTED_AT_KEY] == uncertain.at.isoformat()
    # reconciliation found the bet in the bookmaker's history: it becomes a normal placed bet
    found = placement(PlacementStatus.PLACED, external_bet_id="BC-9", placed_odds=Decimal("2.00"))
    assert ledger.apply_placement(session, bet, found, settings) is BetStatus.PLACED
    assert bet.error is None and ledger.PLACEMENT_ATTEMPTED_AT_KEY not in bet.extra
    assert ledger.current_balance(session) == Decimal("980.00")
    state_now = bankroll.compute_state(session, settings, now=T0 + timedelta(minutes=10))
    assert state_now.reserved_stakes == Decimal("0.00") and state_now.balance == Decimal("980.00")


def test_apply_placement_uncertain_then_absent_releases_the_exposure(
    session: Session, settings: Settings
) -> None:
    from cs2bot.engine import bankroll

    ledger.ensure_initialized(session, settings, at=T0)
    bet = add_bet(session, stake="20", status=BetStatus.PENDING, created_at=T0)
    ledger.apply_placement(session, bet, placement(PlacementStatus.UNCERTAIN), settings)
    assert "did not confirm" in (bet.error or "")
    failed = placement(PlacementStatus.FAILED, message="not in the bet history after the grace period")
    assert ledger.apply_placement(session, bet, failed, settings) is BetStatus.FAILED
    state_now = bankroll.compute_state(session, settings, now=T0 + timedelta(minutes=10))
    assert state_now.balance == state_now.bankroll_reference == Decimal("1000.00")
    assert state_now.reserved_stakes == Decimal("0.00") and len(entries(session)) == 1


@pytest.mark.parametrize(
    "status",
    [
        PlacementStatus.ODDS_MOVED,
        PlacementStatus.MARKET_UNAVAILABLE,
        PlacementStatus.INSUFFICIENT_BALANCE,
        PlacementStatus.LOGIN_REQUIRED,
        PlacementStatus.SELECTOR_ERROR,
        PlacementStatus.KILL_SWITCH,
        PlacementStatus.REJECTED_BY_BOOKMAKER,
        PlacementStatus.FAILED,
    ],
)
def test_apply_placement_failures_put_no_money_at_risk(
    session: Session, settings: Settings, status: PlacementStatus
) -> None:
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="100", status=BetStatus.PENDING)
    assert ledger.apply_placement(session, bet, placement(status), settings) is BetStatus.FAILED
    assert bet.status is BetStatus.FAILED and status.value in (bet.error or "")
    assert bet.placed_at is None and len(entries(session)) == 1
    # a stake that was (wrongly) debited before the attempt is given back
    debited = add_bet(session, stake="40", status=BetStatus.PENDING)
    ledger.record_stake(session, debited)
    ledger.apply_placement(session, debited, placement(status, message="boom"), settings)
    assert debited.error == "boom" and ledger.current_balance(session) == Decimal("1000.00")
    assert [e.kind for e in entries(session)[1:]] == [LedgerKind.STAKE, LedgerKind.REFUND]


def test_apply_placement_real_placement_of_a_dry_run_row_is_booked_as_real(
    session: Session, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Money left the account: the ledger and the limits must see it, whatever the row said."""
    ledger.ensure_initialized(session, settings)
    bet = add_bet(session, stake="50", status=BetStatus.PENDING, dry_run=True)
    with caplog.at_level("ERROR"):
        ledger.apply_placement(session, bet, placement(PlacementStatus.PLACED), settings)
    assert bet.dry_run is False and bet.status is BetStatus.PLACED
    assert ledger.current_balance(session) == Decimal("950.00")
    assert "REAL placement" in caplog.text


def test_apply_placement_refuses_impossible_transitions(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    placed = add_bet(session, stake="10", status=BetStatus.PLACED)
    ledger.record_stake(session, placed)
    for status in (PlacementStatus.FAILED, PlacementStatus.UNCERTAIN, PlacementStatus.DRY_RUN_OK):
        with pytest.raises(ledger.LedgerConflictError, match="cannot be applied"):
            ledger.apply_placement(session, placed, placement(status), settings)
    won = add_bet(session, stake="10", status=BetStatus.WON)
    with pytest.raises(ledger.LedgerConflictError):
        ledger.apply_placement(session, won, placement(PlacementStatus.PLACED), settings)
    failed = add_bet(session, stake="10", status=BetStatus.FAILED)
    with pytest.raises(ledger.LedgerConflictError):  # a failed bet is never resurrected
        ledger.apply_placement(session, failed, placement(PlacementStatus.PLACED), settings)
    assert ledger.apply_placement(session, failed, placement(PlacementStatus.FAILED), settings) is (
        BetStatus.FAILED
    )
    pending = add_bet(session, stake="10", status=BetStatus.PENDING)
    naive = PlacementResult(status=PlacementStatus.PLACED, at=T0.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone"):
        ledger.apply_placement(session, pending, naive, settings)
    assert placed.status is BetStatus.PLACED and ledger.current_balance(session) == Decimal("990.00")


# ----------------------------------------------------------------------------------------------
# reconciliation as a safety control
# ----------------------------------------------------------------------------------------------


def test_reconcile_mismatch_pauses_trading_until_the_owner_confirms(
    session: Session, settings: Settings
) -> None:
    """Stakes and limits are fractions of the ledger: money the bot cannot explain (here a balance
    read ten times too high) must be confirmed by the owner before it is used for sizing."""
    ledger.ensure_initialized(session, settings)
    tol = Decimal("0.05")
    drift = ledger.reconcile(session, Decimal("1000.04"), tolerance=tol)
    assert not drift.flagged and not drift.paused and state.is_trading_halted(session) == (False, None)

    phantom = ledger.reconcile(session, Decimal("10000.40"), tolerance=tol, actor="executor")
    assert phantom.flagged and phantom.paused and phantom.difference == Decimal("9000.36")
    halted, reason = state.is_trading_halted(session)
    assert halted and reason is not None
    assert "10000.40" in reason and "1000.04" in reason and "+9000.36" in reason
    assert state.get_state(session, state.LAST_RECONCILIATION)["paused"] is True
    assert ledger.current_balance(session) == Decimal("10000.40")  # the ledger follows the bookmaker

    # a further mismatch while paused keeps the first reason (and does not claim to have paused)
    again = ledger.reconcile(session, Decimal("1000.04"), tolerance=tol)
    assert again.flagged and not again.paused
    assert "+9000.36" in (state.get_pause(session)[1] or "")

    # opting out (e.g. a manual `cs2bot executor balance` right after a known deposit)
    state.set_paused(session, False, actor="owner")
    quiet = ledger.reconcile(session, Decimal("1500.04"), tolerance=tol, pause_on_flag=False)
    assert quiet.flagged and not quiet.paused and state.is_trading_halted(session) == (False, None)


def test_reconcile_does_not_overwrite_another_pause_reason(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    state.set_paused(session, True, actor="system", reason="daily loss limit reached")
    result = ledger.reconcile(session, Decimal("900.00"), tolerance=Decimal("0.05"))
    assert result.flagged and not result.paused
    assert state.get_pause(session) == (True, "daily loss limit reached")


def test_reconcile_defers_while_an_unconfirmed_placement_can_explain_the_shortfall(
    session: Session, settings: Settings
) -> None:
    ledger.ensure_initialized(session, settings)
    tol = Decimal("0.05")
    bet = add_bet(session, stake="20", odds="2.00", status=BetStatus.PENDING)
    ledger.apply_placement(session, bet, placement(PlacementStatus.UNCERTAIN), settings)
    # Betclic shows 980: exactly what an accepted bet looks like. Nothing is written or flagged.
    deferred = ledger.reconcile(session, Decimal("980.00"), tolerance=tol)
    assert deferred.deferred and not deferred.flagged and not deferred.paused
    assert deferred.entry_id is None and deferred.difference == Decimal("-20.00")
    assert ledger.current_balance(session) == Decimal("1000.00") and len(entries(session)) == 1
    last = state.get_state(session, state.LAST_RECONCILIATION)
    assert last["deferred"] is True and last["unconfirmed_stakes"] == "20.00"
    assert state.is_trading_halted(session) == (False, None)
    # the bet history confirms the bet: it is debited once, and the books agree
    ledger.apply_placement(session, bet, placement(PlacementStatus.PLACED), settings)
    clean = ledger.reconcile(session, Decimal("980.00"), tolerance=tol)
    assert not clean.deferred and not clean.flagged and clean.entry_id is None
    assert [e.kind for e in entries(session)[1:]] == [LedgerKind.STAKE]


def test_reconcile_deferral_is_limited_to_the_unconfirmed_stakes(
    session: Session, settings: Settings
) -> None:
    ledger.ensure_initialized(session, settings)
    tol = Decimal("0.05")
    bet = add_bet(session, stake="20", status=BetStatus.PENDING)
    ledger.apply_placement(session, bet, placement(PlacementStatus.UNCERTAIN), settings)
    # a surplus is never explained by an unconfirmed bet
    surplus = ledger.reconcile(session, Decimal("1030.00"), tolerance=tol, pause_on_flag=False)
    assert surplus.flagged and not surplus.deferred and surplus.entry_id is not None
    # a shortfall larger than the unconfirmed stake (+ tolerance) is a real mismatch
    short = ledger.reconcile(session, Decimal("1009.94"), tolerance=tol)  # 20.06 missing
    assert short.flagged and not short.deferred and short.paused
    assert ledger.current_balance(session) == Decimal("1009.94")
    state.set_paused(session, False, actor="owner")
    # within the stake + tolerance it is deferred again (20.05 missing)
    edge = ledger.reconcile(session, Decimal("989.89"), tolerance=tol)
    assert edge.deferred and not edge.flagged
    # the attempt turns out to have failed: now the same shortfall is unexplained
    ledger.apply_placement(session, bet, placement(PlacementStatus.FAILED), settings)
    unexplained = ledger.reconcile(session, Decimal("989.89"), tolerance=tol)
    assert unexplained.flagged and unexplained.paused and not unexplained.deferred
    # a PENDING row (committed, no result recorded - e.g. a crash right after the click) counts too
    state.set_paused(session, False, actor="owner")
    pending = add_bet(session, stake="15", status=BetStatus.PENDING)
    maybe_sent = ledger.reconcile(session, Decimal("974.89"), tolerance=tol)  # 15.00 missing
    assert maybe_sent.deferred and not maybe_sent.flagged
    pending.status = BetStatus.CANCELLED
    session.flush()
    # dry-run and already debited bets never defer anything
    add_bet(session, stake="50", status=BetStatus.PLACING, dry_run=True)
    debited = add_bet(session, stake="50", status=BetStatus.PLACING)
    ledger.record_stake(session, debited)
    result = ledger.reconcile(session, Decimal("900.00"), tolerance=tol)
    assert result.flagged and not result.deferred


def test_reconcile_rejects_a_balance_that_is_not_a_number(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings)
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="finite"):
            ledger.reconcile(session, bad, tolerance=Decimal("0.05"))
    assert ledger.current_balance(session) == Decimal("1000.00") and len(entries(session)) == 1
    assert state.is_trading_halted(session) == (False, None)
