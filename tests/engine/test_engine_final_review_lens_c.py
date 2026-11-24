"""Final review, Lens C: confidence-tier staking, its caps and validation, the high-confidence guard."""

from __future__ import annotations

import math
import random
import re
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cs2bot.config import BankrollSettings, BetclicSettings, Settings, confidence_tiers_problem
from cs2bot.domain.betting import BankrollState, BetDecision
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.engine import bankroll
from cs2bot.engine.bankroll import confidence_tier, loss_limit_status, size_decisions, size_stake
from cs2bot.engine.confidence import guard_note, guard_pairs, guarded_outcome
from cs2bot.engine.ev import TaxModel, big_win_stake_limit
from tests.engine.engine_factories import T0, make_outcome, make_priced, make_quote

BETCLIC = BetclicSettings()
TIERS = BankrollSettings()  # the default: confidence tiers 75 % -> 1 %, 85 % -> 1.5 %, 92 % -> 2 %
#: loss guards out of the way (the open stakes of a hand-built state would otherwise exhaust them)
NO_GUARDS: dict[str, Any] = {"daily_loss_limit_fraction": 2.0, "weekly_loss_limit_fraction": 2.0}


def state(**kw: Any) -> BankrollState:
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


def decision(*, p: float = 0.8, odds: str = "1.60", p_void: float = 0.0, match_id: int = 1) -> BetDecision:
    priced = make_priced(p_win=p, odds=odds, p_void=p_void)
    return BetDecision(priced=priced, match_id=match_id, accepted=True, decided_at=T0)


def test_default_staking_is_confidence_tiers() -> None:
    assert TIERS.staking_mode == "confidence_tiers"
    assert TIERS.confidence_tiers == [(0.75, 0.01), (0.85, 0.015), (0.92, 0.02)]


@pytest.mark.parametrize(
    ("p", "expected"),
    [
        (0.7499, "0"),
        (0.75, "10.00"),
        (0.80, "10.00"),
        (0.8499, "10.00"),
        (0.85, "15.00"),
        (0.9199, "15.00"),
        (0.92, "20.00"),
        (0.999, "20.00"),
    ],
)
def test_each_tier_boundary(p: float, expected: str) -> None:
    stake, messages = size_stake(decision(p=p), state(), TIERS, BETCLIC)
    assert stake == Decimal(expected)
    if stake == 0:
        assert messages[0].startswith("below_tier:")
    else:
        assert messages[0].startswith("tier:") and "odds ignored" in messages[0]


def test_odds_are_ignored_by_the_tier_stake() -> None:
    """Same confidence, any price: the same stake (a negative-EV 1.28 favourite included)."""
    stakes = {odds: size_stake(decision(p=0.87, odds=odds), state(), TIERS, BETCLIC)[0] for odds in
              ("1.28", "1.60", "3.20")}  # fmt: skip
    assert set(stakes.values()) == {Decimal("15.00")}
    assert decision(p=0.87, odds="1.28").priced.ev < 0


def test_the_tier_uses_the_probability_given_the_bet_settles() -> None:
    """A map-3 market void half the time: p_win 0.40, p_void 0.50 -> p(win | settled) 0.80 (75 % tier)."""
    stake, _ = size_stake(decision(p=0.40, p_void=0.50), state(), TIERS, BETCLIC)
    assert stake == Decimal("10.00")


@pytest.mark.parametrize(
    ("state_kw", "settings_kw", "expected", "prefix"),
    [
        # per-match exposure 4 %: 35 already on the match -> 5 left
        ({"exposure_by_match": {1: Decimal("35.00")}, "balance": Decimal("965.00")}, {}, "5.00", "capped"),
        # daily guard: limit 50, realized loss 30, open 15 -> 5 left
        (
            {
                "realized_pnl_today": Decimal("-30.00"),
                "realized_pnl_week": Decimal("-30.00"),
                "exposure_by_match": {2: Decimal("15.00")},
                "balance": Decimal("955.00"),
                "bankroll_reference": Decimal("970.00"),
                "day_start_bankroll": Decimal("1000.00"),
                "week_start_bankroll": Decimal("1000.00"),
            },
            {},
            "5.00",
            "capped",
        ),
        # weekly guard: limit 100, realized loss this week 95 -> 5 left (daily untouched)
        (
            {
                "realized_pnl_week": Decimal("-95.00"),
                "bankroll_reference": Decimal("905.00"),
                "balance": Decimal("905.00"),
                "week_start_bankroll": Decimal("1000.00"),
                "day_start_bankroll": Decimal("905.00"),
            },
            {},
            "5.00",
            "capped",
        ),
        # free balance
        ({"balance": Decimal("4.00")}, NO_GUARDS, "4.00", "capped"),
        # the max stake fraction caps a tier configured at the limit
        (
            {},
            {"max_stake_fraction": 0.012, "confidence_tiers": [(0.75, 0.01), (0.85, 0.012)]},
            "12.00",
            "tier",
        ),
        # stake step rounds DOWN (never above a cap): 1.5 % of 999.99 = 14.99985 -> 14.90
        ({"bankroll_reference": Decimal("999.99"), "balance": Decimal("999.99")}, {}, "14.90", "tier"),
        # below Betclic's minimum after the caps
        ({"balance": Decimal("1.50")}, NO_GUARDS, "0", "below_min_stake"),
        # bets per day
        ({"bets_placed_today": 10}, {}, "0", "bets_per_day"),
        # halted
        ({"kill_switch": True}, {}, "0", "halted"),
        ({"paused": True, "pause_reason": "manual"}, {}, "0", "halted"),
    ],
)
def test_every_cap_still_binds_with_tiers(
    state_kw: dict[str, Any], settings_kw: dict[str, Any], expected: str, prefix: str
) -> None:
    settings = BankrollSettings(**settings_kw)
    stake, messages = size_stake(decision(p=0.87), state(**state_kw), settings, BETCLIC)
    assert stake == Decimal(expected), messages
    assert (messages and messages[0].startswith(prefix)) or any(m.startswith(prefix) for m in messages), (
        messages
    )


def test_a_batch_respects_the_match_exposure_cap() -> None:
    """Three 92 %+ outcomes of one match: 2 % + 2 % fill the 4 % match cap, the third gets nothing."""
    batch = [decision(p=0.95), decision(p=0.94), decision(p=0.93)]
    sized, projected = size_decisions(batch, state(), TIERS, BETCLIC)
    assert [d.stake for d in sized] == [Decimal("20.00"), Decimal("20.00"), Decimal("0")]
    assert projected.exposure_by_match == {1: Decimal("40.00")}
    assert any(r.startswith("match_exposure") for r in sized[2].reasons)


def test_kelly_mode_is_still_available() -> None:
    kelly = BankrollSettings(staking_mode="kelly")
    # p 0.80 at 1.28: EV < 0 -> Kelly stakes nothing, the tier rule stakes 1 %
    assert size_stake(decision(p=0.80, odds="1.28"), state(), kelly, BETCLIC)[0] == 0
    assert size_stake(decision(p=0.80, odds="1.28"), state(), TIERS, BETCLIC)[0] == Decimal("10.00")


def test_tier_stake_holds_at_the_tax_free_winning_limit_when_crossing_does_not_pay() -> None:
    """Bankroll 200 000: 2 % = 4 000 PLN at 1.30 would win 4 576 PLN (> 2 280 PLN, 10 % withheld). With a
    negative EV the smaller tax-free stake has the higher expected log-growth: the stake is held there."""
    fields = ("balance", "bankroll_reference", "day_start_bankroll", "week_start_bankroll")
    big = state(**dict.fromkeys(fields, Decimal("200000.00")))
    stake, notes = size_stake(decision(p=0.95, odds="1.30"), big, TIERS, BETCLIC)
    limit = big_win_stake_limit(Decimal("1.30"), TaxModel.from_settings(BETCLIC))
    assert limit is not None and stake <= limit < Decimal("4000")
    assert any("winnings tax" in n for n in notes)


def test_confidence_tier_helper() -> None:
    tiers = [(0.75, 0.01), (0.85, 0.015), (0.92, 0.02)]
    assert confidence_tier(0.5, tiers) is None
    assert confidence_tier(math.nan, tiers) is None
    assert confidence_tier(0.86, tiers) == (0.85, 0.015)
    assert confidence_tier(0.86, list(reversed(tiers))) == (0.85, 0.015)  # order does not matter


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"confidence_tiers": []}, "must not be empty"),
        ({"confidence_tiers": [(0.85, 0.01), (0.75, 0.015)]}, "ascending"),
        ({"confidence_tiers": [(0.75, 0.01), (0.75, 0.015)]}, "ascending"),
        ({"confidence_tiers": [(0.75, 0.03)]}, "max_stake_fraction"),
        ({"confidence_tiers": [(0.75, 0.0)]}, "max_stake_fraction"),
        ({"confidence_tiers": [(1.2, 0.01)]}, "(0, 1)"),
        ({"confidence_tiers": [(float("nan"), 0.01)]}, "finite"),
        ({"confidence_tiers": [(0.75, 0.015), (0.85, 0.01)]}, "smaller fraction"),
        ({"max_stake_fraction": 0.015}, "max_stake_fraction"),  # the default 2 % tier no longer fits
    ],
)
def test_tier_validation(kw: dict[str, Any], message: str) -> None:
    """``.env`` settings with broken tiers refuse to load (Settings validator); the merged effective
    settings themselves never raise (an override saved before an upgrade must not stop the cycle)."""
    bank = BankrollSettings(**kw)
    problem = confidence_tiers_problem(bank)
    assert problem is not None and message in problem
    with pytest.raises(ValidationError, match=re.escape(message)):
        Settings(bankroll=bank)


def test_kelly_mode_does_not_require_tiers() -> None:
    assert confidence_tiers_problem(BankrollSettings(staking_mode="kelly", confidence_tiers=[])) is None


def test_an_unvalidated_tier_list_is_still_staked_safely() -> None:
    """A merged override (old max stake 1 %, default tiers up to 2 %): the max-stake cap still binds;
    unusable tiers are skipped."""
    merged = BankrollSettings(max_stake_fraction=0.01)
    stake, notes = size_stake(decision(p=0.95), state(), merged, BETCLIC)
    assert stake == Decimal("10.00") and any(n.startswith("capped: max_stake") for n in notes)
    broken = BankrollSettings(confidence_tiers=[(0.75, float("nan")), (0.80, -0.01)])
    assert size_stake(decision(p=0.95), state(), broken, BETCLIC)[0] == 0


def test_property_tier_stake_never_exceeds_any_cap() -> None:
    rng = random.Random(20261124)
    for _ in range(400):
        ref = Decimal(str(round(rng.uniform(50, 50_000), 2)))
        on_match = Decimal(str(round(rng.uniform(0, float(ref) * 0.05), 2)))
        loss_today = Decimal(str(round(rng.uniform(0, float(ref) * 0.06), 2)))
        st = state(
            balance=ref - on_match,
            bankroll_reference=ref,
            day_start_bankroll=ref + loss_today,
            week_start_bankroll=ref + loss_today,
            realized_pnl_today=-loss_today,
            realized_pnl_week=-loss_today,
            exposure_by_match={1: on_match} if on_match else {},
            bets_placed_today=rng.randint(0, 12),
        )
        p = rng.uniform(0.6, 0.999)
        odds = str(round(rng.uniform(1.14, 6.0), 2))
        settings = BankrollSettings(stake_step=rng.choice([Decimal("0.10"), Decimal("1")]))
        stake, _ = size_stake(decision(p=p, odds=odds), st, settings, BETCLIC)
        if stake == 0:
            continue
        status = loss_limit_status(st, settings)
        tier = confidence_tier(p, settings.confidence_tiers)
        assert tier is not None
        assert stake <= Decimal(str(tier[1])) * ref + Decimal("0.0001")
        assert stake <= Decimal(str(settings.max_stake_fraction)) * ref
        assert stake <= Decimal(str(settings.max_match_exposure_fraction)) * ref - on_match
        assert stake <= status.daily_allowance and stake <= status.weekly_allowance
        assert stake <= st.balance and stake >= BETCLIC.min_stake
        assert stake % settings.stake_step == 0
        assert st.bets_placed_today < settings.max_bets_per_day


# ------------------------------------------------------------------ the high-confidence guard


def test_guarded_outcome_never_raises_a_probability_and_keeps_refunds() -> None:
    spec = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=3)
    outcome = make_outcome(spec, p_win=0.45, p_void=0.5)  # p(win | settled) = 0.90
    shrunk = guarded_outcome(outcome, "map_winner", lambda _m, p: p - 0.05)
    assert shrunk.p_void == 0.5 and shrunk.p_win_given_settled == pytest.approx(0.85)
    assert shrunk.p_lose == pytest.approx(0.5 - 0.425)
    raised = guarded_outcome(outcome, "map_winner", lambda _m, p: min(1.0, p + 0.05))
    assert raised is outcome  # an adjuster may never raise a probability


def test_a_broken_guard_blocks_the_outcome() -> None:
    def boom(_m: str, _p: float) -> float:
        raise RuntimeError("broken guard")

    blocked = guarded_outcome(make_outcome(p_win=0.9), "match_winner", boom)
    assert math.isnan(blocked.p_win)
    nan = guarded_outcome(make_outcome(p_win=0.9), "match_winner", lambda _m, _p: math.nan)
    assert math.isnan(nan.p_win)


def test_guard_pairs_reports_what_changed() -> None:
    from cs2bot.models.confidence import ConfidenceGuard

    winner = MarketSpec(market_type=MarketType.MATCH_WINNER)
    pairs = [
        (make_outcome(winner, "team1", p_win=0.95), make_quote(winner, "team1", odds="1.20")),
        (make_outcome(winner, "team2", p_win=0.05), make_quote(winner, "team2", odds="5.00")),
    ]
    out, changed = guard_pairs(pairs, ConfidenceGuard().adjust)  # no history: half the excess over 0.75
    assert out[0][0].p_win == pytest.approx(0.85)
    assert out[1][0] is pairs[1][0]  # below the threshold: unchanged
    assert changed == {(winner.key(), "team1"): (pytest.approx(0.95), pytest.approx(0.85))}
    assert "0.950 -> 0.850" in guard_note(0.95, 0.85)
    # a 0.75 pick stays bettable under the no-history prior
    assert ConfidenceGuard().adjust("match_winner", 0.75) == pytest.approx(0.75)


def test_tier_after_the_guard_is_more_cautious() -> None:
    """Without backtest history the guard keeps half of the excess over 0.75: a 0.95 pick (2 % tier)
    becomes 0.85 (1.5 % tier) - the stake is more cautious, the pick still bettable."""
    from cs2bot.models.confidence import ConfidenceGuard

    winner = MarketSpec(market_type=MarketType.MATCH_WINNER)
    [(outcome, quote)], _ = guard_pairs(
        [(make_outcome(winner, p_win=0.95), make_quote(winner))], ConfidenceGuard().adjust
    )
    from cs2bot.engine.ev import price

    priced = price(outcome, quote, BETCLIC)
    dec = BetDecision(priced=priced, match_id=1, accepted=True, decided_at=T0)
    assert bankroll.size_stake(dec, state(), TIERS, BETCLIC)[0] == Decimal("15.00")


def test_thin_top_bins_inherit_the_over_confidence_seen_below() -> None:
    """97 % picks that won 70 %: an (empty) 99.5 % bin must not fall back to the milder no-history
    prior (it gave 0.87 and let the pick through); a populated, well-calibrated bin keeps its own
    evidence."""
    from cs2bot.models.confidence import HighConfidenceShrinker

    p = [0.97] * 400
    y = [1 if i % 10 < 7 else 0 for i in range(400)]
    shrinker = HighConfidenceShrinker().fit(p, y)
    assert shrinker.adjust(0.97) < 0.75
    assert shrinker.adjust(0.995) < 0.75
    calibrated_top = HighConfidenceShrinker().fit(
        p + [0.99] * 400, y + [1 if i % 100 < 99 else 0 for i in range(400)]
    )
    assert calibrated_top.adjust(0.99) > 0.95
