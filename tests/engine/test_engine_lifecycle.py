"""Three simulated weeks of betting through the real DB code path, checked against independent books.

Every tick: settle some open bets at random (mostly losses), run the loss-limit check, size a few new
bets against a fresh ``compute_state``, create the ``bets`` rows and apply random executor results
(placed, uncertain, failed, dry run). The test keeps its own cash / exposure / per-day accounts in
plain Python (local days from ``zoneinfo`` directly, across the spring DST change) and asserts after
every step that the engine agrees and that no limit was ever exceeded.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, BetclicSettings, Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import AuditLog, Bet
from cs2bot.domain.betting import PlacementResult, PlacementStatus
from cs2bot.domain.enums import BetStatus
from cs2bot.engine import ledger
from cs2bot.engine.bankroll import check_loss_limits, compute_state, size_decision
from cs2bot.engine.ev import net_payout
from tests.engine.engine_factories import add_bet, add_match, make_decision

WARSAW = ZoneInfo("Europe/Warsaw")
START = datetime(2026, 3, 23, 5, 0, tzinfo=UTC)  # Monday; clocks go forward on Sunday 2026-03-29
TICK = timedelta(hours=3)
N_TICKS = 8 * 21
BETCLIC = BetclicSettings()


def local_day(ts: datetime) -> str:
    return ts.astimezone(WARSAW).date().isoformat()


def local_week(ts: datetime) -> str:
    local = ts.astimezone(WARSAW).date()
    return (local - timedelta(days=local.weekday())).isoformat()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_three_weeks_of_random_betting_never_break_the_books_or_the_limits(
    session: Session, settings: Settings, seed: int
) -> None:
    rng = random.Random(seed)
    cfg = BankrollSettings(max_bets_per_day=6)
    ledger.ensure_initialized(session, cfg, at=START - timedelta(days=1))
    matches = [add_match(session).id for _ in range(40)]

    cash = Decimal("1000.00")  # what the bookmaker account holds, by our own arithmetic
    placed: dict[int, Bet] = {}  # debited, unsettled
    unconfirmed: dict[int, Bet] = {}  # PLACING: not debited, still exposure
    day_loss: dict[str, Decimal] = defaultdict(Decimal)
    week_loss: dict[str, Decimal] = defaultdict(Decimal)
    day_start_ref: dict[str, Decimal] = {}
    week_start_ref: dict[str, Decimal] = {}
    bets_per_day: dict[str, int] = defaultdict(int)
    attempted_at: dict[int, datetime] = {}
    really_placed: dict[int, bool] = {}  # hidden truth about unconfirmed placements
    blocked: set[str] = set()  # which rules stopped a stake at least once
    deferrals = 0
    real_bets = limit_hits = 0

    def reference() -> Decimal:
        return cash + sum((b.stake for b in placed.values()), Decimal(0))

    def balance_check(at: datetime) -> bool:
        """Reconcile against the bookmaker's balance: our cash minus the unconfirmed bets it did
        accept. This never invents a mismatch - the shortfall is deferred until they are resolved."""
        hidden = sum((b.stake for i, b in unconfirmed.items() if really_placed[i]), Decimal(0))
        check = ledger.reconcile(session, cash - hidden, tolerance=Decimal("0.05"), at=at)
        assert not check.flagged and not check.paused and check.entry_id is None
        assert check.deferred == (hidden > 0)
        assert ledger.current_balance(session) == cash
        return check.deferred

    for tick in range(N_TICKS):
        now = START + tick * TICK
        day, week = local_day(now), local_week(now)
        day_start_ref.setdefault(day, reference())
        week_start_ref.setdefault(week, reference())

        # the owner resumes a paused bot in the morning
        if db_state.get_pause(session)[0] and now.astimezone(WARSAW).hour < 9:
            db_state.set_paused(session, False, actor="owner")

        # reconciliation of unconfirmed placements: found (placed after all) or absent (failed)
        for bet_id, bet in list(unconfirmed.items()):
            if rng.random() < 0.5:
                found = really_placed.pop(bet_id)
                status = PlacementStatus.PLACED if found else PlacementStatus.FAILED
                ledger.apply_placement(session, bet, PlacementResult(status=status, at=now), settings)
                del unconfirmed[bet_id]
                if found:
                    cash -= bet.stake
                    placed[bet_id] = bet
                    assert bet.placed_at == attempted_at[bet_id]  # placed when attempted, not when found
                else:  # the bet never existed: it no longer uses one of that day's slots
                    bets_per_day[local_day(attempted_at[bet_id])] -= 1

        # settlement
        for bet_id, bet in list(placed.items()):
            if rng.random() < 0.35:
                outcome = rng.choices(["won", "lost", "void", "push"], weights=[30, 60, 5, 5])[0]
                payout, pnl = ledger.settle_bet(session, bet, outcome, settings, at=now)
                expected = {
                    "won": net_payout(bet.stake, bet.placed_odds or bet.requested_odds, settings),
                    "lost": Decimal("0.00"),
                }.get(outcome, bet.stake)
                assert payout == expected and pnl == expected - bet.stake
                cash += payout
                day_loss[day] -= pnl
                week_loss[week] -= pnl
                del placed[bet_id]

        state = compute_state(session, settings, now=now, bankroll_settings=cfg)
        open_stakes = sum((b.stake for b in (*placed.values(), *unconfirmed.values())), Decimal(0))
        reserved = sum((b.stake for b in unconfirmed.values()), Decimal(0))
        assert ledger.current_balance(session) == cash
        assert state.balance == cash - reserved and state.reserved_stakes == reserved
        assert state.bankroll_reference == reference() + reserved - reserved == state.balance + open_stakes
        assert state.realized_pnl_today == -day_loss[day] and state.realized_pnl_week == -week_loss[week]
        assert state.day_start_bankroll == day_start_ref[day]
        assert state.week_start_bankroll == week_start_ref[week]
        assert state.bets_placed_today == bets_per_day[day]

        deferrals += balance_check(now)

        # the limits held, whatever just lost
        assert day_loss[day] <= Decimal("0.05") * day_start_ref[day]
        assert week_loss[week] <= Decimal("0.10") * week_start_ref[week]
        # True while a limit is reached. The bot is paused once per period; after the owner's resume
        # the guards alone keep it from staking (asserted below).
        limit_reached = check_loss_limits(session, state, cfg)
        limit_hits += limit_reached

        # new bets, one at a time against a fresh state
        for _ in range(rng.randint(0, 4)):
            when = now + timedelta(minutes=rng.randint(1, 170))
            state = compute_state(session, settings, now=when, bankroll_settings=cfg)
            match_id = rng.choice(matches)
            decision = make_decision(
                match_id=match_id,
                kelly_full=rng.choice([0.01, 0.03, 0.08, 0.3]),
                odds=rng.choice(["1.45", "1.60", "1.95", "2.40"]),
            )
            sized = size_decision(decision, state, cfg, BETCLIC)
            halted, _ = db_state.is_trading_halted(session)
            if limit_reached and local_day(when) == day:
                assert sized.stake == 0 and not sized.accepted
            if halted or local_day(when) != day:
                assert not halted or (sized.stake == 0 and not sized.accepted)
                continue
            if sized.stake == 0:
                assert sized.reasons and not sized.accepted
                blocked.update(r.split(":")[0] for r in sized.reasons)
                continue
            stake = sized.stake
            ref = state.bankroll_reference
            on_match = sum(
                (b.stake for b in (*placed.values(), *unconfirmed.values()) if b.match_id == match_id),
                Decimal(0),
            )
            # caps, checked against the test's own books
            assert stake <= Decimal("0.02") * ref and on_match + stake <= Decimal("0.04") * ref
            assert stake <= cash - reserved and stake >= BETCLIC.min_stake
            assert day_loss[day] + open_stakes + stake <= Decimal("0.05") * day_start_ref[day]
            assert bets_per_day[day] < cfg.max_bets_per_day

            bet = add_bet(
                session,
                stake=stake,
                status=BetStatus.PENDING,
                match_id=match_id,
                odds=str(decision.priced.quote.odds),
                created_at=when,
            )
            bet.placed_odds = None
            result_status = rng.choices(
                [
                    PlacementStatus.PLACED,
                    PlacementStatus.UNCERTAIN,
                    PlacementStatus.ODDS_MOVED,
                    PlacementStatus.DRY_RUN_OK,
                ],
                weights=[78, 8, 9, 5],
            )[0]
            new_status = ledger.apply_placement(
                session, bet, PlacementResult(status=result_status, at=when), settings
            )
            if new_status is BetStatus.PLACED:
                cash -= stake
                placed[bet.id] = bet
                real_bets += 1
            elif new_status is BetStatus.PLACING:
                unconfirmed[bet.id] = bet
                attempted_at[bet.id] = when
                really_placed[bet.id] = rng.random() < 0.5
                reserved += stake
            if new_status in (BetStatus.PLACED, BetStatus.PLACING):
                open_stakes += stake
                bets_per_day[day] += 1
            assert ledger.current_balance(session) == cash
        deferrals += balance_check(now + TICK - timedelta(minutes=1))

    assert real_bets > 30, "the simulation must actually bet"
    assert deferrals > 0, "unconfirmed placements that the bookmaker accepted must have occurred"
    # the guards were really exercised (they are what keeps the realized losses inside the limits)
    assert {"daily_loss_guard", "bets_per_day"} <= blocked, blocked
    worst_day = max(day_loss[d] / day_start_ref[d] for d in day_start_ref)
    worst_week = max(week_loss[w] / week_start_ref[w] for w in week_start_ref)
    # ... and the limits were approached without ever being crossed
    assert Decimal("0.04") < worst_day <= Decimal("0.05")
    assert Decimal("0.08") < worst_week <= Decimal("0.10")
    assert len(day_start_ref) == 22 and len(week_start_ref) == 4  # 23 March .. 13 April, local time
    # a reached limit paused the bot (once per period), and nothing else ever did
    pause_events = session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "pause")
    )
    assert (pause_events > 0) == (limit_hits > 0) and pause_events <= len(day_start_ref) + len(week_start_ref)
