"""Bankroll state on SQLite (local midnight, DST, dry runs), stake sizing caps and loss limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, BetclicSettings, Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import AuditLog, Bet
from cs2bot.domain.betting import BankrollState
from cs2bot.domain.enums import BetStatus
from cs2bot.engine import ledger
from cs2bot.engine.bankroll import (
    LOSS_LIMIT_STATE_KEY,
    apply_stake,
    check_loss_limits,
    compute_state,
    loss_limit_status,
    reserve_stake,
    size_decision,
    size_decisions,
    size_stake,
    total_open_stakes,
)
from cs2bot.engine.ev import TaxModel, kelly_fraction
from tests.engine.engine_factories import add_bet, add_match, make_decision, make_outcome

BETCLIC = BetclicSettings()
BANKROLL = BankrollSettings()


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def settle(session: Session, bet: Bet, status: BetStatus, settled_at: datetime, settings: Settings) -> None:
    """Settle a bet the way the settlement service will: status, payout/pnl, ledger entries."""
    outcome = {BetStatus.WON: "won", BetStatus.LOST: "lost", BetStatus.VOID: "void"}[status]
    payout, pnl = ledger.settle_amounts(bet, outcome, settings)
    bet.status, bet.settled_at, bet.payout, bet.pnl = status, settled_at, payout, pnl
    if status is BetStatus.VOID:
        ledger.record_refund(session, bet, at=settled_at)
    else:
        ledger.record_payout(session, bet, payout, at=settled_at)
    session.flush()


def place(
    session: Session, stake: str, placed_at: datetime, *, match_id: int | None = None, odds: str = "2.00"
) -> Bet:
    bet = add_bet(session, stake=stake, placed_at=placed_at, match_id=match_id, odds=odds)
    ledger.record_stake(session, bet)
    return bet


# ------------------------------------------------------------------ compute_state


def test_empty_ledger_is_initialised(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)
    st = compute_state(session, settings, now=now)
    assert st.balance == st.bankroll_reference == Decimal("1000.00")
    assert st.day_start_bankroll == st.week_start_bankroll == Decimal("1000.00")
    assert st.realized_pnl_today == st.realized_pnl_week == Decimal("0.00")
    assert st.open_stakes_today == st.open_stakes_week == Decimal("0.00")
    assert st.exposure_by_match == {} and st.bets_placed_today == 0
    assert not st.kill_switch and not st.paused and st.pause_reason is None
    assert ledger.current_balance(session) == Decimal("1000.00")


def test_open_stakes_reserved_stakes_exposure_and_dry_runs(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=now - timedelta(days=30))
    a, b = add_match(session), add_match(session)
    place(session, "20", now - timedelta(hours=1), match_id=a.id)  # debited in the ledger
    pending_at = now - timedelta(minutes=1)
    add_bet(
        session, stake="10", status=BetStatus.PENDING, match_id=a.id, created_at=pending_at
    )  # not debited
    place(session, "15", now - timedelta(days=2), match_id=b.id)  # placed earlier this week
    add_bet(session, stake="99", status=BetStatus.PLACED, dry_run=True, match_id=a.id, placed_at=now)
    add_bet(session, stake="77", status=BetStatus.FAILED, match_id=a.id, placed_at=now)
    st = compute_state(session, settings, now=now)
    # ledger: 1000 - 20 - 15 = 965; the pending 10 is still in it but already committed
    assert st.balance == Decimal("955.00")
    assert st.bankroll_reference == Decimal("1000.00")  # 955 cash + 45 at risk
    assert st.exposure_by_match == {a.id: Decimal("30.00"), b.id: Decimal("15.00")}
    assert st.open_stakes_today == Decimal("30.00")  # 20 placed + 10 pending (created a minute ago)
    assert st.open_stakes_week == Decimal("45.00")  # Jan 13 (Tue) is in the week of Mon Jan 12
    assert st.bets_placed_today == 2  # dry run and failed bets do not count


def test_realized_pnl_across_local_midnight_in_winter(session: Session, settings: Settings) -> None:
    # Thursday 2026-01-15 11:00 Warsaw (CET, UTC+1): the local day started at 2026-01-14 23:00 UTC
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    early = place(session, "20", utc(2026, 1, 14, 21))
    late = place(session, "20", utc(2026, 1, 14, 21))
    older = place(session, "10", utc(2026, 1, 9, 12))
    settle(session, early, BetStatus.WON, utc(2026, 1, 14, 22, 30), settings)  # 23:30 local, yesterday
    settle(session, late, BetStatus.LOST, utc(2026, 1, 14, 23, 30), settings)  # 00:30 local, today
    settle(session, older, BetStatus.LOST, utc(2026, 1, 11, 22, 59), settings)  # Sun 23:59, last week
    st = compute_state(session, settings, now=now)
    assert st.realized_pnl_today == Decimal("-20.00")
    assert st.realized_pnl_week == Decimal("-20.00") + Decimal("15.20")  # win: 17.60 * 2 - 20
    # ledger: 1000 - 20 - 20 - 10 + 35.20 = 985.20
    assert st.balance == st.bankroll_reference == Decimal("985.20")
    assert st.day_start_bankroll == Decimal("1005.20")
    assert st.week_start_bankroll == Decimal("990.00")
    assert st.bets_placed_today == 0  # both placed yesterday (local)


@pytest.mark.parametrize(
    ("now", "inside", "outside"),
    [
        # spring forward: local day of Sun 2026-03-29 starts 2026-03-28 23:00 UTC (CET)
        (utc(2026, 3, 29, 12), utc(2026, 3, 28, 23, 15), utc(2026, 3, 28, 22, 45)),
        # the next day starts at 2026-03-29 22:00 UTC (CEST) - also the start of the week
        (utc(2026, 3, 30, 8), utc(2026, 3, 29, 22, 30), utc(2026, 3, 29, 21, 30)),
        # fall back: Sun 2026-10-25 starts 2026-10-24 22:00 UTC (CEST) and lasts 25 hours
        (utc(2026, 10, 25, 22, 45), utc(2026, 10, 24, 22, 30), utc(2026, 10, 24, 21, 45)),
        # ... so 23:30 local on the 25th (22:30 UTC) is still "today" at 23:45 local
        (utc(2026, 10, 25, 22, 45), utc(2026, 10, 25, 22, 30), utc(2026, 10, 24, 21, 59)),
        # the next local day starts 2026-10-25 23:00 UTC (CET)
        (utc(2026, 10, 25, 23, 30), utc(2026, 10, 25, 23, 5), utc(2026, 10, 25, 22, 55)),
    ],
)
def test_day_boundaries_across_dst(
    session: Session, settings: Settings, now: datetime, inside: datetime, outside: datetime
) -> None:
    ledger.ensure_initialized(session, settings, at=now - timedelta(days=40))
    a = place(session, "10", now - timedelta(days=3))
    b = place(session, "30", now - timedelta(days=3))
    settle(session, a, BetStatus.LOST, inside, settings)
    settle(session, b, BetStatus.LOST, outside, settings)
    st = compute_state(session, settings, now=now)
    assert st.realized_pnl_today == Decimal("-10.00")


def test_week_start_on_monday_after_spring_forward(session: Session, settings: Settings) -> None:
    now = utc(2026, 3, 30, 8)  # Monday 10:00 CEST, week started 2026-03-29 22:00 UTC
    ledger.ensure_initialized(session, settings, at=utc(2026, 3, 1))
    a = place(session, "10", utc(2026, 3, 27, 12))
    b = place(session, "30", utc(2026, 3, 27, 12))
    settle(session, a, BetStatus.LOST, utc(2026, 3, 29, 21, 30), settings)  # Sunday 23:30 local
    settle(session, b, BetStatus.LOST, utc(2026, 3, 29, 22, 30), settings)  # Monday 00:30 local
    st = compute_state(session, settings, now=now)
    assert st.realized_pnl_week == Decimal("-30.00") == st.realized_pnl_today


def test_deposits_and_withdrawals_count_as_start_of_period_capital(
    session: Session, settings: Settings
) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=now - timedelta(hours=2))  # first day of the bot
    st = compute_state(session, settings, now=now)
    assert st.day_start_bankroll == Decimal("1000.00")  # not 0: the deposit is today's capital
    bet = place(session, "50", now - timedelta(hours=1))
    settle(session, bet, BetStatus.LOST, now - timedelta(minutes=30), settings)
    ledger.record_deposit(session, Decimal("500"), at=now - timedelta(minutes=10))
    ledger.record_withdrawal(session, Decimal("200"), at=now - timedelta(minutes=5))
    st = compute_state(session, settings, now=now)
    assert st.bankroll_reference == Decimal("1250.00")
    assert st.day_start_bankroll == Decimal("1300.00")  # 1000 + 500 - 200, before today's -50
    assert loss_limit_status(st, BANKROLL).daily_limit == Decimal("65.00")


def test_compute_state_reads_kill_switch_and_pause(session: Session, settings: Settings) -> None:
    db_state.set_kill_switch(session, True, actor="owner", reason="manual")
    db_state.set_paused(session, True, actor="system", reason="daily loss limit reached")
    st = compute_state(session, settings, now=utc(2026, 1, 15, 10))
    assert st.kill_switch and st.paused and st.pause_reason == "daily loss limit reached"
    with pytest.raises(ValueError, match="timezone"):
        compute_state(session, settings, now=datetime(2026, 1, 15, 10))


def test_settled_bet_without_pnl_is_derived(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    common: dict[str, Any] = {"placed_at": now - timedelta(hours=3), "settled_at": now - timedelta(hours=1)}
    won = add_bet(session, stake="10", odds="2.00", status=BetStatus.WON, **common)
    lost = add_bet(session, stake="5", status=BetStatus.LOST, **common)
    void = add_bet(session, stake="7", status=BetStatus.VOID, **common)
    paid = add_bet(session, stake="10", status=BetStatus.WON, payout="15.00", **common)
    assert all(b.pnl is None for b in (won, lost, void, paid))
    st = compute_state(session, settings, now=now)
    assert st.realized_pnl_today == Decimal("7.60") - Decimal("5.00") + Decimal("5.00")
    assert st.bets_placed_today == 4


def test_state_carries_period_starts_and_reserved_stakes(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)  # Thursday 11:00 Warsaw
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    place(session, "20", now - timedelta(hours=1))
    add_bet(session, stake="10", status=BetStatus.PENDING, created_at=now - timedelta(minutes=1))
    add_bet(session, stake="5", status=BetStatus.PLACING, created_at=now - timedelta(minutes=1))
    st = compute_state(session, settings, now=now)
    assert st.day_start == utc(2026, 1, 14, 23) and st.week_start == utc(2026, 1, 11, 23)
    assert st.reserved_stakes == Decimal("15.00")  # pending + uncertain placement: not debited yet
    assert st.balance == Decimal("965.00") and st.bankroll_reference == Decimal("1000.00")
    assert total_open_stakes(st) == Decimal("35.00") == st.bankroll_reference - st.balance


def test_settled_bet_without_settled_at_still_counts_for_the_limits(
    session: Session, settings: Settings
) -> None:
    """An integration bug (status set, settled_at forgotten) must not hide a loss from the limits."""
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    bet = place(session, "60", now - timedelta(days=2))
    bet.status, bet.pnl = BetStatus.LOST, Decimal("-60.00")
    bet.updated_at = now - timedelta(hours=1)
    session.flush()
    assert bet.settled_at is None
    st = compute_state(session, settings, now=now)
    assert st.realized_pnl_today == Decimal("-60.00") == st.realized_pnl_week
    assert check_loss_limits(session, st, settings) is True
    # ... while a loss whose row was last touched before this week is left alone
    old = place(session, "30", now - timedelta(days=20))
    old.status, old.pnl = BetStatus.LOST, Decimal("-30.00")
    old.updated_at = now - timedelta(days=19)
    session.flush()
    assert compute_state(session, settings, now=now).realized_pnl_week == Decimal("-60.00")


# ------------------------------------------------------------------ loss limits


def test_daily_loss_limit_pauses_once_and_respects_manual_resume(
    session: Session, settings: Settings
) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    bets = [place(session, "25", now - timedelta(hours=3)) for _ in range(2)]
    for b in bets:
        settle(session, b, BetStatus.LOST, now - timedelta(hours=1), settings)
    st = compute_state(session, settings, now=now)
    status = loss_limit_status(st, BANKROLL)
    assert (status.realized_loss_today, status.daily_limit, status.daily_hit) == (
        Decimal("50.00"),
        Decimal("50.00"),
        True,
    )
    assert check_loss_limits(session, st, settings, now=now) is True
    paused, reason = db_state.get_pause(session)
    assert paused and reason is not None and "daily loss limit reached" in reason and "50.00" in reason
    assert db_state.is_trading_halted(session)[0]

    def pause_audits() -> int:
        return int(
            session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "pause")) or 0
        )

    assert pause_audits() == 1
    st = compute_state(session, settings, now=now)
    assert check_loss_limits(session, st, settings, now=now) is True
    assert pause_audits() == 1  # no repeated pause on every scheduler tick

    db_state.set_paused(session, False, actor="owner", reason="checked, continue")
    st = compute_state(session, settings, now=now + timedelta(minutes=5))
    assert check_loss_limits(session, st, settings, now=now + timedelta(minutes=5)) is True
    assert db_state.get_pause(session)[0] is False  # the manual resume is respected
    # ... but the daily guard still blocks new stakes until the day rolls over
    stake, reasons = size_stake(make_decision(kelly_full=0.3), st, BANKROLL, BETCLIC)
    assert stake == 0 and reasons[0].startswith("daily_loss_guard")
    assert db_state.get_state(session, LOSS_LIMIT_STATE_KEY)["daily"] == "2026-01-14T23:00:00+00:00"


def test_loss_limit_period_comes_from_the_state_not_the_wall_clock(
    session: Session, settings: Settings
) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    bet = place(session, "50", now - timedelta(hours=3))
    settle(session, bet, BetStatus.LOST, now - timedelta(hours=1), settings)
    st = compute_state(session, settings, now=now)
    assert check_loss_limits(session, st, settings) is True  # no ``now``: the real clock is irrelevant
    assert db_state.get_state(session, LOSS_LIMIT_STATE_KEY)["daily"] == "2026-01-14T23:00:00+00:00"
    assert db_state.get_pause(session)[0] is True


def test_weekly_loss_limit(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)  # Thursday
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    for day in (12, 13, 14):  # Mon-Wed: -40 a day, below the daily limit each day
        b = place(session, "40", utc(2026, 1, day, 9))
        settle(session, b, BetStatus.LOST, utc(2026, 1, day, 12), settings)
    st = compute_state(session, settings, now=now)
    status = loss_limit_status(st, BANKROLL)
    assert not status.daily_hit and status.weekly_hit
    assert status.weekly_limit == Decimal("100.00")  # 10% of the week-start bankroll 1000
    assert check_loss_limits(session, st, BANKROLL, now=now) is True
    reason = db_state.get_pause(session)[1] or ""
    assert "weekly loss limit reached" in reason and "120.00" in reason


def test_no_limit_hit_means_no_pause(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    b = place(session, "20", now - timedelta(hours=2))
    settle(session, b, BetStatus.LOST, now - timedelta(hours=1), settings)
    st = compute_state(session, settings, now=now)
    assert check_loss_limits(session, st, settings, now=now) is False
    assert db_state.get_pause(session) == (False, None)


def test_loss_limit_rearms_the_next_day(session: Session, settings: Settings) -> None:
    day1 = utc(2026, 1, 13, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    b = place(session, "50", day1 - timedelta(hours=2))
    settle(session, b, BetStatus.LOST, day1 - timedelta(hours=1), settings)
    assert check_loss_limits(session, compute_state(session, settings, now=day1), settings, now=day1)
    db_state.set_paused(session, False, actor="owner")
    day2 = day1 + timedelta(days=1)
    c = place(session, "48", day2 - timedelta(hours=2))
    settle(session, c, BetStatus.LOST, day2 - timedelta(hours=1), settings)
    st2 = compute_state(session, settings, now=day2)
    assert check_loss_limits(session, st2, settings, now=day2) is True
    assert db_state.get_pause(session)[0] is True  # a new day's limit pauses again


# ------------------------------------------------------------------ stake sizing


def base_state(**kw: Any) -> BankrollState:
    values: dict[str, Any] = {
        "balance": Decimal("1000.00"),
        "bankroll_reference": Decimal("1000.00"),
        "day_start_bankroll": Decimal("1000.00"),
        "week_start_bankroll": Decimal("1000.00"),
        "realized_pnl_today": Decimal("0.00"),
        "realized_pnl_week": Decimal("0.00"),
        "open_stakes_today": Decimal("0.00"),
        "open_stakes_week": Decimal("0.00"),
    }
    values.update(kw)
    return BankrollState(**values)


NO_GUARDS: dict[str, Any] = {"daily_loss_limit_fraction": 2.0, "weekly_loss_limit_fraction": 2.0}

STAKE_CASES: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], str, str | None]] = [
    # name, state kw, decision kw, bankroll settings kw, expected stake, expected first reason/note prefix
    ("quarter-kelly", {}, {"kelly_full": 0.04}, {}, "10.00", None),
    ("rounds-down", {}, {"kelly_full": 0.0413}, {}, "10.30", None),  # 10.325
    ("rounds-down-to-step", {}, {"kelly_full": 0.0413}, {"stake_step": Decimal("1")}, "10.00", None),
    ("max-stake", {}, {"kelly_full": 0.3}, {}, "20.00", "capped: max_stake"),
    ("match-exposure", {"exposure_by_match": {1: Decimal("35")}}, {"kelly_full": 0.3}, {}, "5.00",
     "capped: max_stake"),
    ("other-match-exposure-ignored", {"exposure_by_match": {2: Decimal("40")}}, {"kelly_full": 0.04}, {},
     "10.00", None),
    ("match-exposure-full", {"exposure_by_match": {1: Decimal("40")}}, {"kelly_full": 0.3}, {}, "0",
     "match_exposure: nothing left"),
    ("daily-guard", {"realized_pnl_today": Decimal("-30"), "realized_pnl_week": Decimal("-30"),
                     "open_stakes_today": Decimal("15"), "open_stakes_week": Decimal("15")},
     {"kelly_full": 0.3}, {}, "5.00", "capped: max_stake"),
    ("daily-guard-exhausted", {"realized_pnl_today": Decimal("-40"), "realized_pnl_week": Decimal("-40"),
                               "open_stakes_today": Decimal("10"), "open_stakes_week": Decimal("10")},
     {"kelly_full": 0.3}, {}, "0", "daily_loss_guard: nothing left"),
    ("profits-do-not-extend-the-guard",
     {"realized_pnl_today": Decimal("80"), "realized_pnl_week": Decimal("80"),
      "open_stakes_today": Decimal("45"), "open_stakes_week": Decimal("45")},
     {"kelly_full": 0.3}, {}, "5.00", "capped: max_stake"),
    ("weekly-guard", {"realized_pnl_week": Decimal("-90"), "open_stakes_week": Decimal("6")},
     {"kelly_full": 0.3}, {}, "4.00", "capped: max_stake"),
    ("weekly-guard-exhausted", {"realized_pnl_week": Decimal("-100")}, {"kelly_full": 0.3}, {}, "0",
     "weekly_loss_guard: nothing left"),
    ("bets-per-day", {"bets_placed_today": 10}, {"kelly_full": 0.3}, {}, "0", "bets_per_day"),
    ("bets-per-day-custom", {"bets_placed_today": 3}, {"kelly_full": 0.3}, {"max_bets_per_day": 3}, "0",
     "bets_per_day"),
    # a free balance of 3.55 with a 1000 bankroll means 996.45 is at risk: the loss guards stop that
    # long before the balance cap does, so the balance cap is isolated with the guards switched off
    ("balance", {"balance": Decimal("3.55")}, {"kelly_full": 0.3}, NO_GUARDS, "3.50", "capped: max_stake"),
    ("balance-implies-open-stakes", {"balance": Decimal("3.55")}, {"kelly_full": 0.3}, {}, "0",
     "daily_loss_guard: nothing left"),
    ("carried-over-open-stakes", {"balance": Decimal("965"), "exposure_by_match": {7: Decimal("35")}},
     {"kelly_full": 0.3}, {}, "15.00", "capped: max_stake"),  # nothing placed today, 35 still open
    ("carried-over-open-stakes-full", {"balance": Decimal("950")}, {"kelly_full": 0.3}, {}, "0",
     "daily_loss_guard: nothing left"),
    ("min-stake", {}, {"kelly_full": 0.005}, {}, "0", "below_min_stake"),  # 1.25 < 2.00
    ("min-stake-after-cap", {"balance": Decimal("1.99")}, {"kelly_full": 0.3}, NO_GUARDS, "0",
     "below_min_stake"),
    ("never-rounds-above-a-cap", {"exposure_by_match": {1: Decimal("37.994")}}, {"kelly_full": 0.3},
     {"stake_step": Decimal("0.001")}, "2.00", "capped: max_stake"),  # cap 2.006: 2.00, never 2.01
    ("kill-switch", {"kill_switch": True}, {"kelly_full": 0.3}, {}, "0", "halted: kill switch"),
    ("paused", {"paused": True, "pause_reason": "daily loss limit reached"}, {"kelly_full": 0.3}, {}, "0",
     "halted: paused (daily loss limit reached)"),
    ("not-accepted", {}, {"kelly_full": 0.3, "accepted": False}, {}, "0", "not_accepted"),
    ("no-edge", {}, {"kelly_full": -0.1}, {}, "0", "no_edge"),
    ("no-bankroll", {"bankroll_reference": Decimal("0"), "balance": Decimal("0")}, {"kelly_full": 0.3}, {},
     "0", "no_bankroll"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("state_kw", "decision_kw", "settings_kw", "expected", "first"),
    [c[1:] for c in STAKE_CASES],
    ids=[c[0] for c in STAKE_CASES],
)
def test_size_stake(
    state_kw: dict[str, Any],
    decision_kw: dict[str, Any],
    settings_kw: dict[str, Any],
    expected: str,
    first: str | None,
) -> None:
    stake, reasons = size_stake(
        make_decision(**decision_kw), base_state(**state_kw), BankrollSettings(**settings_kw), BETCLIC
    )
    assert stake == Decimal(expected)
    if first is None:
        assert reasons == []
    else:
        assert reasons and reasons[0].startswith(first), reasons
    if stake == 0:
        assert reasons, "a zero stake always comes with a reason"


def test_halted_lists_every_reason() -> None:
    st = base_state(kill_switch=True, paused=True, pause_reason="weekly loss limit", bets_placed_today=12)
    stake, reasons = size_stake(make_decision(kelly_full=-0.2), st, BANKROLL, BETCLIC)
    assert stake == 0
    assert [r.split(":")[0] for r in reasons] == ["halted", "halted", "bets_per_day", "no_edge"]


def test_binding_caps_are_reported_as_notes() -> None:
    st = base_state(exposure_by_match={1: Decimal("35")}, balance=Decimal("4.20"))
    stake, notes = size_stake(make_decision(kelly_full=0.3), st, BankrollSettings(**NO_GUARDS), BETCLIC)
    assert stake == Decimal("4.20")
    assert [n.split(" ->")[0] for n in notes] == [
        "capped: max_stake",
        "capped: match_exposure",
        "capped: balance",
    ]


def test_many_small_bets_hit_the_daily_guard(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)
    bankroll = BankrollSettings(max_bets_per_day=30)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    stakes: list[Decimal] = []
    last_reasons: list[str] = []
    for i in range(15):
        st = compute_state(session, settings, now=now + timedelta(minutes=i))
        match = add_match(session)
        stake, last_reasons = size_stake(
            make_decision(match_id=match.id, kelly_full=0.02), st, bankroll, BETCLIC
        )
        if stake == 0:
            break
        stakes.append(stake)
        place(session, str(stake), now + timedelta(minutes=i), match_id=match.id)
    # 0.25 * 0.02 * 1000 = 5 PLN each; the 5% daily limit (50 PLN) allows exactly ten open bets
    assert stakes == [Decimal("5.00")] * 10
    assert last_reasons[0].startswith("daily_loss_guard: nothing left")
    st = compute_state(session, settings, now=now + timedelta(minutes=20))
    assert st.open_stakes_today == Decimal("50.00") and st.bets_placed_today == 10
    # The next local day the bets are still open: they can all lose "today", so they keep counting
    # against the daily guard although none of them was placed today.
    tomorrow = now + timedelta(days=1)
    st2 = compute_state(session, settings, now=tomorrow)
    assert st2.open_stakes_today == 0 and st2.open_stakes_week == Decimal("50.00")
    assert total_open_stakes(st2) == Decimal("50.00") and st2.bets_placed_today == 0
    stake2, reasons2 = size_stake(
        make_decision(match_id=add_match(session).id, kelly_full=0.02), st2, bankroll, BETCLIC
    )
    assert stake2 == 0 and reasons2[0].startswith("daily_loss_guard: nothing left")
    # once four of them have won, their stakes are no longer at risk and the guard has room again
    open_bets = list(session.scalars(select(Bet).where(Bet.status == BetStatus.PLACED).order_by(Bet.id)))
    for bet in open_bets[:4]:
        settle(session, bet, BetStatus.WON, tomorrow, settings)
    st3 = compute_state(session, settings, now=tomorrow + timedelta(minutes=1))
    assert total_open_stakes(st3) == Decimal("30.00") and st3.realized_pnl_today > 0
    stake3, _ = size_stake(
        make_decision(match_id=add_match(session).id, kelly_full=0.02), st3, bankroll, BETCLIC
    )
    assert stake3 == Decimal("5.00")


def test_worst_case_daily_loss_stays_within_the_limit_across_midnight(
    session: Session, settings: Settings
) -> None:
    """Bets placed just before and just after local midnight, all losing on the same day."""
    bankroll = BankrollSettings(max_bets_per_day=50)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    evening = utc(2026, 1, 14, 22, 30)  # 23:30 Warsaw on the 14th
    after_midnight = utc(2026, 1, 14, 23, 10)  # 00:10 Warsaw on the 15th
    placed: list[Bet] = []
    for when in (evening, after_midnight):
        for i in range(20):
            now = when + timedelta(seconds=i)
            st = compute_state(session, settings, now=now)
            match = add_match(session)
            stake, _ = size_stake(make_decision(match_id=match.id, kelly_full=0.3), st, bankroll, BETCLIC)
            if stake == 0:
                break
            placed.append(place(session, str(stake), now, match_id=match.id))
    # 20 + 20 + 10 in the evening exhaust the 5% guard; nothing more fits after midnight
    assert [b.stake for b in placed] == [Decimal("20.00"), Decimal("20.00"), Decimal("10.00")]
    for bet in placed:
        settle(session, bet, BetStatus.LOST, utc(2026, 1, 15, 18), settings)
    st = compute_state(session, settings, now=utc(2026, 1, 15, 19))
    status = loss_limit_status(st, bankroll)
    assert status.realized_loss_today == Decimal("50.00") <= status.daily_limit == Decimal("50.00")


def test_multi_day_worst_case_never_exceeds_the_daily_or_weekly_limit(
    session: Session, settings: Settings
) -> None:
    """Stake as much as allowed every day; every bet loses the day after it was placed."""
    bankroll = BankrollSettings(max_bets_per_day=50)
    ledger.ensure_initialized(session, settings, at=utc(2025, 12, 1))
    open_bets: list[Bet] = []
    week_loss = Decimal("0")
    week_start_bankroll: Decimal | None = None
    for day in range(12, 19):  # Mon 2026-01-12 .. Sun 2026-01-18
        morning = utc(2026, 1, day, 9)
        day_loss = Decimal("0")
        for bet in open_bets:  # yesterday's bets all lose this morning
            settle(session, bet, BetStatus.LOST, morning, settings)
            day_loss += bet.stake
        open_bets = []
        st = compute_state(session, settings, now=morning + timedelta(minutes=1))
        week_start_bankroll = week_start_bankroll or st.week_start_bankroll
        assert day_loss <= loss_limit_status(st, bankroll).daily_limit
        week_loss += day_loss
        for i in range(30):
            now = morning + timedelta(hours=1, minutes=i)
            st = compute_state(session, settings, now=now)
            match = add_match(session)
            stake, _ = size_stake(make_decision(match_id=match.id, kelly_full=0.3), st, bankroll, BETCLIC)
            if stake == 0:
                break
            open_bets.append(place(session, str(stake), now, match_id=match.id))
    assert week_start_bankroll is not None
    assert Decimal("0") < week_loss <= Decimal("0.10") * week_start_bankroll


def test_match_exposure_cap_with_real_bets(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    m = add_match(session)
    sizes = []
    for i in range(4):
        st = compute_state(session, settings, now=now + timedelta(minutes=i))
        stake, _ = size_stake(make_decision(match_id=m.id, kelly_full=0.3), st, BANKROLL, BETCLIC)
        sizes.append(stake)
        if stake > 0:
            place(session, str(stake), now + timedelta(minutes=i), match_id=m.id)
    assert sizes == [Decimal("20.00"), Decimal("20.00"), Decimal("0"), Decimal("0")]  # 4% of 1000


# ------------------------------------------------------------------ the 10 % winnings tax


def test_winnings_tax_caps_the_stake_when_crossing_does_not_pay() -> None:
    # p = 0.5 at 3.00: the PIT on the whole winning makes a stake just above the limit worse
    outcome = make_outcome(p_win=0.5)
    kelly = kelly_fraction(outcome, Decimal("3.00"), TaxModel.from_settings(BETCLIC))
    decision = make_decision(odds="3.00", p_win=0.5, kelly_full=kelly)
    st = base_state(
        balance=Decimal("50000"),
        bankroll_reference=Decimal("50000"),
        day_start_bankroll=Decimal("50000"),
        week_start_bankroll=Decimal("50000"),
    )
    stake, notes = size_stake(decision, st, BANKROLL, BETCLIC)
    assert stake == Decimal("863.60")  # tax-free limit 863.64, rounded down to the 0.10 step
    assert any(n.startswith("capped: winnings tax") for n in notes)


def test_winnings_tax_is_accepted_when_the_edge_is_large() -> None:
    outcome = make_outcome(p_win=0.8)
    kelly = kelly_fraction(outcome, Decimal("3.00"), TaxModel.from_settings(BETCLIC))
    decision = make_decision(odds="3.00", p_win=0.8, kelly_full=kelly)
    st = base_state(
        balance=Decimal("200000"),
        bankroll_reference=Decimal("200000"),
        day_start_bankroll=Decimal("200000"),
        week_start_bankroll=Decimal("200000"),
    )
    stake, notes = size_stake(decision, st, BANKROLL, BETCLIC)
    assert stake == Decimal("4000.00")  # 2% cap, well above the tax-free limit
    assert any(n.startswith("winnings tax: staking 4000.00") for n in notes)


def test_no_winnings_tax_logic_below_the_threshold() -> None:
    stake, notes = size_stake(make_decision(odds="3.00", kelly_full=0.3), base_state(), BANKROLL, BETCLIC)
    assert stake == Decimal("20.00") and not any("winnings tax" in n for n in notes)


# ------------------------------------------------------------------ applying the stake


def test_apply_stake_and_size_decision() -> None:
    st = base_state()
    d = make_decision(kelly_full=0.04)
    sized = size_decision(d, st, BANKROLL, BETCLIC)
    assert sized.accepted and sized.stake == Decimal("10.00")
    assert sized.kelly_fraction_used == pytest.approx(0.01)
    assert d.stake == 0  # the original is untouched
    assert sized.notes == [] and sized.reasons == []
    rejected = size_decision(d, base_state(kill_switch=True), BANKROLL, BETCLIC)
    assert not rejected.accepted and rejected.stake == 0 and rejected.kelly_fraction_used == 0
    assert rejected.reasons == ["halted: kill switch is on"] and rejected.notes == []
    zero = apply_stake(d, base_state(bankroll_reference=Decimal("0")), Decimal("0"), ["x: y"])
    assert not zero.accepted and zero.reasons == ["x: y"]


def test_sizing_notes_and_rejection_reasons_are_kept_apart() -> None:
    capped = size_decision(make_decision(kelly_full=0.3), base_state(), BANKROLL, BETCLIC)
    assert capped.accepted and capped.stake == Decimal("20.00")
    assert capped.reasons == [] and capped.notes and capped.notes[0].startswith("capped: max_stake")
    zero = size_decision(make_decision(kelly_full=0.005), base_state(), BANKROLL, BETCLIC)
    assert not zero.accepted and zero.stake == 0 and zero.notes == []
    assert zero.reasons and zero.reasons[0].startswith("below_min_stake")
    rejected = make_decision(kelly_full=0.3, accepted=False)
    assert size_decision(rejected, base_state(), BANKROLL, BETCLIC) is rejected  # left untouched


def test_reserve_stake_matches_what_compute_state_sees(session: Session, settings: Settings) -> None:
    now = utc(2026, 1, 15, 10)
    ledger.ensure_initialized(session, settings, at=utc(2026, 1, 1))
    match = add_match(session)
    before = compute_state(session, settings, now=now)
    projected = reserve_stake(before, match.id, Decimal("12.50"))
    add_bet(session, stake="12.50", status=BetStatus.PENDING, match_id=match.id, created_at=now)
    assert compute_state(session, settings, now=now) == projected
    assert reserve_stake(before, match.id, Decimal("0")) is before


def test_size_decisions_reserves_each_stake_before_sizing_the_next() -> None:
    state = base_state()
    decisions = [make_decision(match_id=m, kelly_full=0.3) for m in (1, 1, 1, 2, 3, 4)]
    decisions.insert(2, make_decision(match_id=9, kelly_full=0.3, accepted=False))
    # against one snapshot every bet would get the 2% cap: 120 PLN at risk with a 50 PLN daily limit
    assert sum(size_stake(d, state, BANKROLL, BETCLIC)[0] for d in decisions) == Decimal("120.00")
    sized, after = size_decisions(decisions, state, BANKROLL, BETCLIC)
    assert [d.stake for d in sized] == [Decimal(x) for x in ("20", "20", "0", "0", "10", "0", "0")]
    assert [d.accepted for d in sized] == [True, True, False, False, True, False, False]
    assert sized[3].reasons[0].startswith("match_exposure: nothing left")  # 4% on match 1 is used up
    assert sized[4].notes[-1].startswith("capped: daily_loss_guard")
    assert sized[5].reasons[0].startswith("daily_loss_guard: nothing left")
    assert after.exposure_by_match == {1: Decimal("40.00"), 2: Decimal("10.00")}
    assert after.balance == Decimal("950.00") and after.bankroll_reference == Decimal("1000.00")
    assert after.bets_placed_today == 3 and after.reserved_stakes == Decimal("50.00")
    assert total_open_stakes(after) == Decimal("50.00")
    assert state.balance == Decimal("1000.00")  # the input state is not mutated


def test_size_decisions_respects_bets_per_day() -> None:
    settings = BankrollSettings(max_bets_per_day=2, **NO_GUARDS)
    decisions = [make_decision(match_id=m, kelly_full=0.04) for m in (1, 2, 3)]
    sized, after = size_decisions(decisions, base_state(), settings, BETCLIC)
    assert [d.stake for d in sized] == [Decimal("10.00"), Decimal("10.00"), Decimal("0")]
    assert sized[2].reasons[0].startswith("bets_per_day") and after.bets_placed_today == 2


def test_non_finite_kelly_is_no_edge() -> None:
    decision = make_decision(kelly_full=0.3)
    broken = decision.model_copy(
        update={"priced": decision.priced.model_copy(update={"kelly_full": float("nan")})}
    )
    stake, reasons = size_stake(broken, base_state(), BANKROLL, BETCLIC)
    assert stake == 0 and reasons[0].startswith("no_edge")


# ------------------------------------------------------------------ property: no cap is ever exceeded


@hyp_settings(max_examples=400, deadline=None)
@given(
    bankroll=st.decimals(min_value=Decimal("1"), max_value=Decimal("300000"), places=2),
    open_share=st.floats(min_value=0.0, max_value=0.2),
    match_share=st.floats(min_value=0.0, max_value=1.0),
    pnl_today=st.decimals(min_value=Decimal("-400"), max_value=Decimal("400"), places=2),
    pnl_week_extra=st.decimals(min_value=Decimal("-800"), max_value=Decimal("800"), places=2),
    kelly_full=st.floats(min_value=-0.5, max_value=1.0),
    p_win=st.floats(min_value=0.05, max_value=0.99),
    odds=st.decimals(min_value=Decimal("1.01"), max_value=Decimal("15"), places=2),
    step=st.sampled_from([Decimal("0.01"), Decimal("0.10"), Decimal("0.50"), Decimal("1"), Decimal("5")]),
    kelly_fraction_=st.sampled_from([0.1, 0.25, 0.5, 1.0]),
    bets_today=st.integers(min_value=0, max_value=12),
    halted=st.sampled_from(["no", "no", "no", "kill", "pause"]),
)
def test_property_stake_never_exceeds_any_cap(
    bankroll: Decimal,
    open_share: float,
    match_share: float,
    pnl_today: Decimal,
    pnl_week_extra: Decimal,
    kelly_full: float,
    p_win: float,
    odds: Decimal,
    step: Decimal,
    kelly_fraction_: float,
    bets_today: int,
    halted: str,
) -> None:
    """Whatever the state: the stake is a multiple of the step, at or above the minimum (or zero with a
    reason), never above the fractional-Kelly stake, the per-bet cap, the match cap, the free balance
    or what the daily / weekly loss guards leave - and a halted bot stakes nothing."""
    open_total = (bankroll * Decimal(repr(open_share))).quantize(Decimal("0.01"))
    on_match = (open_total * Decimal(repr(match_share))).quantize(Decimal("0.01"))
    pnl_week = pnl_today + pnl_week_extra
    state = BankrollState(
        balance=bankroll - open_total,
        bankroll_reference=bankroll,
        day_start_bankroll=bankroll - pnl_today,
        week_start_bankroll=bankroll - pnl_week,
        realized_pnl_today=pnl_today,
        realized_pnl_week=pnl_week,
        open_stakes_today=on_match,
        open_stakes_week=on_match,
        exposure_by_match={1: on_match, 2: open_total - on_match},
        bets_placed_today=bets_today,
        kill_switch=halted == "kill",
        paused=halted == "pause",
    )
    cfg = BankrollSettings(kelly_fraction=kelly_fraction_, stake_step=step)
    decision = make_decision(match_id=1, kelly_full=kelly_full, odds=str(odds), p_win=p_win)
    stake, messages = size_stake(decision, state, cfg, BETCLIC)

    assert stake >= 0
    if stake == 0:
        assert messages, "a zero stake always says why"
        return
    assert halted == "no" and kelly_full > 0 and bets_today < cfg.max_bets_per_day
    assert stake >= BETCLIC.min_stake
    assert (stake / step) == (stake / step).to_integral_value()  # a multiple of the stake step
    assert stake <= Decimal(repr(kelly_fraction_)) * Decimal(repr(kelly_full)) * bankroll
    assert stake <= Decimal(repr(cfg.max_stake_fraction)) * bankroll
    assert on_match + stake <= Decimal(repr(cfg.max_match_exposure_fraction)) * bankroll
    assert stake <= state.balance
    # worst case: every open stake and this one lose, on top of the losses already realized
    loss_today = max(Decimal(0), -pnl_today)
    loss_week = max(Decimal(0), -pnl_week)
    daily_limit = Decimal(repr(cfg.daily_loss_limit_fraction)) * state.day_start_bankroll
    weekly_limit = Decimal(repr(cfg.weekly_loss_limit_fraction)) * state.week_start_bankroll
    assert loss_today + open_total + stake <= daily_limit + Decimal("0.005")
    assert loss_week + open_total + stake <= weekly_limit + Decimal("0.005")
    # reserving the stake reproduces the caps for the next bet
    after = reserve_stake(state, 1, stake)
    assert total_open_stakes(after) == open_total + stake
    assert after.balance == state.balance - stake and after.bankroll_reference == bankroll


def test_crossing_the_winnings_tax_threshold_is_never_worse_than_staying_below_it() -> None:
    """Random large-bankroll cases, judged with the EXACT payout (stake tax, zloty-rounded 10 % PIT):
    whenever the sized stake differs from the plain capped Kelly stake because of the 2,280 PLN
    threshold, its expected log-growth is positive and at least that of the tax-free limit."""
    import math
    import random

    from cs2bot.engine.ev import big_win_stake_limit, net_payout

    def growth(p: float, stake: Decimal, odds: Decimal, bankroll: Decimal) -> float:
        win = float((net_payout(stake, odds, BETCLIC) - stake) / bankroll)
        return p * math.log1p(win) + (1.0 - p) * math.log1p(-float(stake / bankroll))

    rng = random.Random(5)
    crossed = held = 0
    for _ in range(600):
        bankroll = Decimal(rng.choice([9000, 20000, 60000, 150000, 400000, 1000000]))
        odds = Decimal(str(round(rng.uniform(1.2, 15.0), 2)))
        p = rng.uniform(0.3, 0.97)
        kelly = kelly_fraction(make_outcome(p_win=p), odds, TaxModel.from_settings(BETCLIC))
        if kelly <= 0:
            continue
        cfg = BankrollSettings(
            kelly_fraction=rng.choice([0.25, 0.5, 1.0]),
            max_stake_fraction=rng.choice([0.02, 0.05, 0.2]),
            max_match_exposure_fraction=1.0,
            daily_loss_limit_fraction=1.0,
            weekly_loss_limit_fraction=1.0,
        )
        st = base_state(
            balance=bankroll,
            bankroll_reference=bankroll,
            day_start_bankroll=bankroll,
            week_start_bankroll=bankroll,
        )
        stake, notes = size_stake(make_decision(odds=str(odds), p_win=p, kelly_full=kelly), st, cfg, BETCLIC)
        limit = big_win_stake_limit(odds, BETCLIC)
        assert limit is not None
        cap = (
            min(
                Decimal(repr(cfg.kelly_fraction)) * Decimal(repr(kelly)),
                Decimal(repr(cfg.max_stake_fraction)),
            )
            * bankroll
        )
        assert stake <= cap
        if cap <= limit or stake == 0:
            assert not any("winnings tax" in n for n in notes)
            continue
        assert any("winnings tax" in n for n in notes)
        at_limit = (limit / cfg.stake_step).to_integral_value(rounding="ROUND_DOWN") * cfg.stake_step
        assert growth(p, stake, odds, bankroll) > 0
        assert growth(p, stake, odds, bankroll) >= growth(p, at_limit, odds, bankroll) - 1e-12
        if stake > limit:
            crossed += 1
        else:
            held += 1
            assert stake == at_limit
    assert crossed > 50 and held > 5  # both branches are exercised
