"""Adversarial sequences of bets and settlements against the bankroll guards (review round 2).

Complements ``test_engine_lifecycle.py`` with what it does not exercise:

* batches sized from ONE state with :func:`bankroll.size_decisions` (several bets per match, so the
  4 % match cap and the guards have to hold across the batch);
* refunds that keep the 12 % stake tax (``void_refunds_stake_tax = False``: a void costs money),
  tax-free promo days, and wins at odds below 1 / 0.88 (a "win" that loses money);
* settlements dated back to any moment after the placement - also across local midnight, so the
  loss lands on a day that is already over;
* deposits in the middle of a day;
* the autumn DST change (the week of 2026-10-19 .. 25 has a 25-hour Sunday).

Invariant: the realized net loss of every local day (week) never exceeds the daily (weekly) limit of
the day-start (week-start) bankroll the engine saw last during that day (week); deposits only raise
it. Every stake respects its caps, the ledger equals the test's own cash book and reconciliation
never flags anything.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, BetclicSettings, Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import Bet
from cs2bot.domain.betting import PlacementResult, PlacementStatus
from cs2bot.domain.enums import BetStatus
from cs2bot.engine import ledger
from cs2bot.engine.bankroll import check_loss_limits, compute_state, size_decisions
from tests.engine.engine_factories import add_bet, add_match, make_decision

WARSAW = ZoneInfo("Europe/Warsaw")
START = datetime(2026, 10, 19, 4, 0, tzinfo=UTC)  # Monday; clocks go back on Sunday 2026-10-25
TICK = timedelta(hours=2)
N_TICKS = 12 * 12
CFG = BankrollSettings(max_bets_per_day=14)
NORMAL = BetclicSettings(void_refunds_stake_tax=False)
PROMO = BetclicSettings(void_refunds_stake_tax=False, tax_free_promo=True)
STEP = CFG.stake_step


def local_day(ts: datetime) -> str:
    return ts.astimezone(WARSAW).date().isoformat()


def local_week(ts: datetime) -> str:
    local = ts.astimezone(WARSAW).date()
    return (local - timedelta(days=local.weekday())).isoformat()


@pytest.mark.parametrize("seed", [5, 6])
def test_adversarial_batches_refunds_and_backdated_settlements_never_break_a_limit(
    session: Session, settings: Settings, seed: int
) -> None:
    rng = random.Random(seed)
    ledger.ensure_initialized(session, CFG, at=START - timedelta(days=1))
    matches = [add_match(session).id for _ in range(5)]  # few matches: the exposure cap binds
    cash = Decimal("1000.00")
    placed: dict[int, Bet] = {}
    unconfirmed: dict[int, Bet] = {}
    really_placed: dict[int, bool] = {}
    day_loss: dict[str, Decimal] = defaultdict(Decimal)
    week_loss: dict[str, Decimal] = defaultdict(Decimal)
    last_day_start: dict[str, Decimal] = {}
    last_week_start: dict[str, Decimal] = {}
    seen = {"capped_match": 0, "backdated_across_midnight": 0, "void_cost": 0, "losing_win": 0, "deposit": 0}
    n_bets = 0

    for tick in range(N_TICKS):
        now = START + tick * TICK
        betclic = PROMO if local_day(now) in {"2026-10-21", "2026-10-26"} else NORMAL
        if db_state.get_pause(session)[0] and now.astimezone(WARSAW).hour < 9:
            db_state.set_paused(session, False, actor="owner")  # the owner resumes every morning

        if rng.random() < 0.04:  # the owner tops the account up
            amount = Decimal(rng.choice(["50", "120", "300"]))
            ledger.record_deposit(session, amount, at=now)
            cash += amount
            seen["deposit"] += 1

        for bet_id, bet in list(unconfirmed.items()):  # unconfirmed placements are resolved
            if rng.random() < 0.6:
                found = really_placed.pop(bet_id)
                status = PlacementStatus.PLACED if found else PlacementStatus.FAILED
                ledger.apply_placement(session, bet, PlacementResult(status=status, at=now), betclic)
                del unconfirmed[bet_id]
                if found:
                    cash -= bet.stake
                    placed[bet_id] = bet

        for bet_id, bet in list(placed.items()):  # adversarial settlement: mostly losses
            if rng.random() > 0.3:
                continue
            outcome = rng.choices(["lost", "won", "void", "push"], weights=[50, 35, 8, 7])[0]
            assert bet.placed_at is not None
            at = now
            if rng.random() < 0.35:  # dated back to the real settlement time, maybe yesterday
                at = bet.placed_at + (now - bet.placed_at) * rng.random()
                seen["backdated_across_midnight"] += local_day(at) != local_day(now)
            payout, pnl = ledger.settle_bet(session, bet, outcome, betclic, at=at)
            if outcome in ("void", "push"):
                assert payout == bet.stake - bet.tax and pnl == -bet.tax  # the stake tax is kept
                seen["void_cost"] += pnl < 0
            elif outcome == "won":
                seen["losing_win"] += pnl < 0
            else:
                assert payout == 0 and pnl == -bet.stake
            cash += payout
            day_loss[local_day(at)] -= pnl
            week_loss[local_week(at)] -= pnl
            del placed[bet_id]

        state = compute_state(session, settings, now=now, bankroll_settings=CFG)
        day, week = local_day(now), local_week(now)
        last_day_start[day] = state.day_start_bankroll
        last_week_start[week] = state.week_start_bankroll
        assert ledger.current_balance(session) == cash
        assert state.realized_pnl_today == -day_loss[day] and state.realized_pnl_week == -week_loss[week]
        hidden = sum((b.stake for i, b in unconfirmed.items() if really_placed[i]), Decimal(0))
        check = ledger.reconcile(session, cash - hidden, tolerance=Decimal("0.05"), at=now)
        assert not check.flagged and check.entry_id is None
        limit_reached = check_loss_limits(session, state, CFG)

        # a batch of decisions sized from one snapshot, several on the same match
        hot = rng.choice(matches)
        batch = [
            make_decision(
                match_id=hot if rng.random() < 0.75 else rng.choice(matches),
                kelly_full=rng.choice([0.04, 0.2, 0.7]),
                odds=rng.choice(["1.07", "1.12", "1.55", "1.90", "2.60"]),
            )
            for _ in range(rng.randint(0, 6))
        ]
        sized, projected = size_decisions(batch, state, CFG, betclic)
        ref = state.bankroll_reference
        exposure = dict(state.exposure_by_match)
        batch_total = Decimal(0)
        for decision in sized:
            if decision.stake == 0:
                assert not decision.accepted and decision.reasons
                seen["capped_match"] += any(r.startswith("match_exposure") for r in decision.reasons)
                continue
            stake = decision.stake
            assert not state.paused and not limit_reached
            assert stake % STEP == 0 and stake >= betclic.min_stake
            assert stake <= Decimal("0.02") * ref
            exposure[decision.match_id] = exposure.get(decision.match_id, Decimal(0)) + stake
            assert exposure[decision.match_id] <= Decimal("0.04") * ref
            batch_total += stake
        assert batch_total <= state.balance
        open_now = state.bankroll_reference - state.balance
        assert day_loss[day] + open_now + batch_total <= Decimal("0.05") * state.day_start_bankroll
        assert week_loss[week] + open_now + batch_total <= Decimal("0.10") * state.week_start_bankroll
        assert projected.balance == state.balance - batch_total

        for decision in (d for d in sized if d.stake > 0):
            when = now + timedelta(seconds=30)
            bet = add_bet(
                session,
                stake=decision.stake,
                status=BetStatus.PENDING,
                match_id=decision.match_id,
                odds=str(decision.priced.quote.odds),
                created_at=when,
                tax=ledger.bet_money_fields(decision.stake, decision.priced.quote.odds, betclic)["tax"],
            )
            bet.placed_odds = None
            result = rng.choices(
                [PlacementStatus.PLACED, PlacementStatus.UNCERTAIN, PlacementStatus.ODDS_MOVED],
                weights=[85, 8, 7],
            )[0]
            status = ledger.apply_placement(session, bet, PlacementResult(status=result, at=when), betclic)
            n_bets += 1
            if status is BetStatus.PLACED:
                cash -= bet.stake
                placed[bet.id] = bet
            elif status is BetStatus.PLACING:
                unconfirmed[bet.id] = bet
                really_placed[bet.id] = rng.random() < 0.5

    # the realized loss of every day / week stayed within the limit of what the engine saw last
    for day, loss in day_loss.items():
        assert loss <= Decimal("0.05") * last_day_start[day], (day, loss, last_day_start[day])
    for week, loss in week_loss.items():
        assert loss <= Decimal("0.10") * last_week_start[week], (week, loss, last_week_start[week])
    worst_day = max(day_loss[d] / last_day_start[d] for d in day_loss)
    assert worst_day > Decimal("0.035"), "the sequence must push the daily guard"
    assert n_bets > 25
    assert seen["capped_match"] and seen["backdated_across_midnight"] and seen["void_cost"]
    assert seen["losing_win"] and seen["deposit"], seen
