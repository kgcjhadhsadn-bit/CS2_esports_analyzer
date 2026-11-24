"""Pricing engine tests on hand-built predictions (see engine_factories for the templates).

Default BO3 central maps q = (0.60, 0.55, 0.50):
P(2-0) = 0.33, P(0-2) = 0.18, P(map 3 played) = 0.49, P(2-1) = P(1-2) = 0.245, P(team1) = 0.575.
Low scenario q - 0.05 -> P(team1) = 0.5; high q + 0.05 -> P(team1) = 0.6485.
Map 1 (q = 0.6) round scores: 13-5 .12, 13-8 .18, 13-11 .18, 16-14 .12, 7-13 .10, 10-13 .14,
11-13 .08, 14-16 .08.
"""

from __future__ import annotations

import logging

import pytest

from cs2bot.config import BetclicSettings, reset_settings_cache
from cs2bot.domain.enums import MatchFormat
from cs2bot.domain.markets import (
    SELECTION_DRAW,
    SELECTION_EVEN,
    SELECTION_NO,
    SELECTION_ODD,
    SELECTION_OVER,
    SELECTION_TEAM1,
    SELECTION_TEAM2,
    SELECTION_UNDER,
    SELECTION_YES,
    MarketSpec,
    MarketType,
    player_selection,
)
from cs2bot.domain.predictions import MatchPrediction, OutcomeProbability
from cs2bot.engine import pricing as pricing_module
from cs2bot.engine.pricing import (
    UnpriceableMarket,
    fallback_sigma,
    final_map_scores,
    is_final_round_score,
    market_rules,
    price_core,
    price_outcome,
    price_quotes,
    price_quotes_report,
)
from tests.engine.engine_factories import (
    make_prediction,
    make_quote,
    mirror_prediction,
    mirror_selection,
    mirror_spec,
)

FLOOR = 0.01
APPROX = 1e-9

MT = MarketType


def spec(market_type: MarketType, **kw: object) -> MarketSpec:
    return MarketSpec(market_type=market_type, **kw)  # type: ignore[arg-type]


def price(s: MarketSpec, sel: str, pred: MatchPrediction | None = None, **kw: object) -> OutcomeProbability:
    return price_outcome(s, sel, pred or make_prediction(), sigma_floor=FLOOR, **kw)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def bo3() -> MatchPrediction:
    return make_prediction(MatchFormat.BO3)


# ------------------------------------------------------------------ match-level markets


def test_match_winner_bo3(bo3: MatchPrediction) -> None:
    t1 = price(spec(MT.MATCH_WINNER), SELECTION_TEAM1, bo3)
    t2 = price(spec(MT.MATCH_WINNER), SELECTION_TEAM2, bo3)
    assert t1.p_win == pytest.approx(0.575)
    assert t2.p_win == pytest.approx(0.425)
    assert t1.p_push == t1.p_void == 0.0
    # sigma = |0.6485 - 0.5| / 2
    assert t1.sigma == pytest.approx(0.07425)
    assert t2.sigma == pytest.approx(0.07425)
    assert t1.spec_key == "match_winner" and t1.selection == SELECTION_TEAM1
    with pytest.raises(UnpriceableMarket, match="BO2"):
        price(spec(MT.MATCH_WINNER), SELECTION_DRAW, bo3)
    with pytest.raises(UnpriceableMarket):
        price(spec(MT.MATCH_WINNER), "home", bo3)


def test_match_winner_bo2_has_a_draw() -> None:
    pred = make_prediction(MatchFormat.BO2, [0.6, 0.55])
    t1 = price(spec(MT.MATCH_WINNER), SELECTION_TEAM1, pred)
    t2 = price(spec(MT.MATCH_WINNER), SELECTION_TEAM2, pred)
    draw = price(spec(MT.MATCH_WINNER), SELECTION_DRAW, pred)
    assert t1.p_win == pytest.approx(0.33)
    assert draw.p_win == pytest.approx(0.49)
    assert t2.p_win == pytest.approx(0.18)
    assert t1.p_win + t2.p_win + draw.p_win == pytest.approx(1.0)
    # a draw is a loss for team1 in the 3-way market, not a push
    assert t1.p_push == 0.0 and t1.p_lose == pytest.approx(0.67)


def test_correct_map_score_bo3(bo3: MatchPrediction) -> None:
    expected = {"2-0": 0.33, "2-1": 0.245, "1-2": 0.245, "0-2": 0.18}
    for sel, p in expected.items():
        assert price(spec(MT.CORRECT_MAP_SCORE), sel, bo3).p_win == pytest.approx(p)
    assert sum(price(spec(MT.CORRECT_MAP_SCORE), s, bo3).p_win for s in expected) == pytest.approx(1.0)
    with pytest.raises(UnpriceableMarket, match="not a possible final score"):
        price(spec(MT.CORRECT_MAP_SCORE), "3-0", bo3)
    with pytest.raises(UnpriceableMarket):
        price(spec(MT.CORRECT_MAP_SCORE), SELECTION_TEAM1, bo3)


def test_correct_score_selections_that_settlement_refuses_are_unpriceable(bo3: MatchPrediction) -> None:
    """Review round 2: a malformed pmf carrying mass on an impossible score used to make that
    selection priceable, while settlement leaves it UNKNOWN forever (the bet could never settle)."""
    broken_series = bo3.central.model_copy(
        update={"map_score_pmf": {**bo3.central.map_score_pmf, "1-0": 0.0}}
    )
    slot = bo3.central.slots[0]
    broken_slot = slot.model_copy(update={"score_pmf": {**slot.score_pmf, "13-12": 0.0}})
    broken = bo3.model_copy(
        update={"central": broken_series.model_copy(update={"slots": [broken_slot, *bo3.central.slots[1:]]})}
    )
    with pytest.raises(UnpriceableMarket, match="not a possible final score"):
        price(spec(MT.CORRECT_MAP_SCORE), "1-0", broken)
    with pytest.raises(UnpriceableMarket, match="not a possible final CS2 map score"):
        price(spec(MT.MAP_CORRECT_SCORE, map_number=1), "13-12", broken)
    assert price(spec(MT.CORRECT_MAP_SCORE), "2-0", broken).p_win == pytest.approx(0.33)


def test_final_map_scores() -> None:
    assert final_map_scores(MatchFormat.BO1) == {(1, 0), (0, 1)}
    assert final_map_scores(MatchFormat.BO2) == {(2, 0), (1, 1), (0, 2)}
    assert final_map_scores(MatchFormat.BO3) == {(2, 0), (2, 1), (1, 2), (0, 2)}
    assert len(final_map_scores(MatchFormat.BO5)) == 6


@pytest.mark.parametrize(
    ("line", "sel", "win", "push"),
    [
        (-1.5, SELECTION_TEAM1, 0.33, 0.0),  # team1 wins 2-0
        (-1.5, SELECTION_TEAM2, 0.67, 0.0),  # team2 +1.5
        (1.5, SELECTION_TEAM1, 0.82, 0.0),  # team1 +1.5 = not 0-2
        (1.5, SELECTION_TEAM2, 0.18, 0.0),  # team2 -1.5 = 0-2
        (-1.0, SELECTION_TEAM1, 0.33, 0.245),  # integer line: 2-1 pushes
        (-1.0, SELECTION_TEAM2, 0.425, 0.245),
        (0.5, SELECTION_TEAM2, 0.425, 0.0),
    ],
)
def test_map_handicap(bo3: MatchPrediction, line: float, sel: str, win: float, push: float) -> None:
    o = price(spec(MT.MAP_HANDICAP, line=line), sel, bo3)
    assert o.p_win == pytest.approx(win)
    assert o.p_push == pytest.approx(push)
    assert o.p_void == 0.0


def test_map_handicap_bo2_zero_line_pushes_on_draw() -> None:
    pred = make_prediction(MatchFormat.BO2, [0.6, 0.55])
    t1 = price(spec(MT.MAP_HANDICAP, line=0.0), SELECTION_TEAM1, pred)
    t2 = price(spec(MT.MAP_HANDICAP, line=0.0), SELECTION_TEAM2, pred)
    assert (t1.p_win, t1.p_push) == (pytest.approx(0.33), pytest.approx(0.49))
    assert (t2.p_win, t2.p_push) == (pytest.approx(0.18), pytest.approx(0.49))
    assert t1.p_win_given_settled == pytest.approx(0.33 / 0.51)


def test_total_maps_and_both_teams(bo3: MatchPrediction) -> None:
    over = price(spec(MT.TOTAL_MAPS, line=2.5), SELECTION_OVER, bo3)
    under = price(spec(MT.TOTAL_MAPS, line=2.5), SELECTION_UNDER, bo3)
    assert over.p_win == pytest.approx(0.49) and under.p_win == pytest.approx(0.51)
    yes = price(spec(MT.BOTH_TEAMS_WIN_A_MAP), SELECTION_YES, bo3)
    no = price(spec(MT.BOTH_TEAMS_WIN_A_MAP), SELECTION_NO, bo3)
    assert yes.p_win == pytest.approx(0.49) and no.p_win == pytest.approx(0.51)
    with pytest.raises(UnpriceableMarket, match="needs a line"):
        price(spec(MT.TOTAL_MAPS), SELECTION_OVER, bo3)
    with pytest.raises(UnpriceableMarket):
        price(spec(MT.TOTAL_MAPS, line=2.5), SELECTION_YES, bo3)


def test_bo1() -> None:
    pred = make_prediction(MatchFormat.BO1, [0.6])
    assert price(spec(MT.MATCH_WINNER), SELECTION_TEAM1, pred).p_win == pytest.approx(0.6)
    mw = price(spec(MT.MAP_WINNER, map_number=1), SELECTION_TEAM1, pred)
    assert mw.p_win == pytest.approx(0.6) and mw.p_void == pytest.approx(0.0)
    assert price(spec(MT.BOTH_TEAMS_WIN_A_MAP), SELECTION_YES, pred).p_win == pytest.approx(0.0)
    assert price(spec(MT.TOTAL_MAPS, line=1.5), SELECTION_UNDER, pred).p_win == pytest.approx(1.0)
    assert price(spec(MT.CORRECT_MAP_SCORE), "1-0", pred).p_win == pytest.approx(0.6)
    with pytest.raises(UnpriceableMarket, match="does not exist"):
        price(spec(MT.MAP_WINNER, map_number=2), SELECTION_TEAM1, pred)
    with pytest.raises(UnpriceableMarket):
        price(spec(MT.CORRECT_MAP_SCORE), "2-0", pred)


def test_bo5() -> None:
    pred = make_prediction(MatchFormat.BO5)  # q = .6 .55 .5 .45 .4
    assert price(spec(MT.CORRECT_MAP_SCORE), "3-0", pred).p_win == pytest.approx(0.165)
    assert price(spec(MT.CORRECT_MAP_SCORE), "0-3", pred).p_win == pytest.approx(0.09)
    map4 = price(spec(MT.MAP_WINNER, map_number=4), SELECTION_TEAM1, pred)
    assert map4.p_void == pytest.approx(0.255)  # 1 - P(3-0) - P(0-3)
    assert map4.p_win == pytest.approx(0.745 * 0.45)
    over = price(spec(MT.TOTAL_MAPS, line=4.0), SELECTION_OVER, pred)
    under = price(spec(MT.TOTAL_MAPS, line=4.0), SELECTION_UNDER, pred)
    p5 = price(spec(MT.MAP_WINNER, map_number=5), SELECTION_TEAM1, pred)
    assert over.p_win == pytest.approx(1 - p5.p_void)  # five maps played
    assert under.p_win == pytest.approx(0.255)
    assert over.p_push == pytest.approx(1 - 0.255 - over.p_win)  # exactly four maps
    assert price(spec(MT.MAP_HANDICAP, line=-2.5), SELECTION_TEAM1, pred).p_win == pytest.approx(0.165)
    scores = ["3-0", "3-1", "3-2", "2-3", "1-3", "0-3"]
    total = sum(price(spec(MT.CORRECT_MAP_SCORE), s, pred).p_win for s in scores)
    assert total == pytest.approx(1.0)


# ------------------------------------------------------------------ map-slot markets


def test_map_winner_void_when_not_played(bo3: MatchPrediction) -> None:
    t1 = price(spec(MT.MAP_WINNER, map_number=3), SELECTION_TEAM1, bo3)
    t2 = price(spec(MT.MAP_WINNER, map_number=3), SELECTION_TEAM2, bo3)
    assert t1.p_win == pytest.approx(0.245) and t1.p_void == pytest.approx(0.51)
    assert t2.p_win == pytest.approx(0.245)
    assert t1.p_win_given_settled == pytest.approx(0.5)
    # sigma: low 0.5 * 0.45 = 0.225, high 0.47 * 0.55 = 0.2585
    assert t1.sigma == pytest.approx((0.2585 - 0.225) / 2)
    m1 = price(spec(MT.MAP_WINNER, map_number=1), SELECTION_TEAM1, bo3)
    assert m1.p_win == pytest.approx(0.6) and m1.p_void == pytest.approx(0.0)
    with pytest.raises(UnpriceableMarket, match="map_number"):
        price(spec(MT.MAP_WINNER), SELECTION_TEAM1, bo3)


@pytest.mark.parametrize(
    ("line", "sel", "win", "push"),
    [
        (-4.5, SELECTION_TEAM1, 0.30, 0.0),
        (-4.5, SELECTION_TEAM2, 0.70, 0.0),
        (-2.0, SELECTION_TEAM1, 0.30, 0.30),
        (-2.0, SELECTION_TEAM2, 0.40, 0.30),
        (3.5, SELECTION_TEAM2, 0.10, 0.0),  # team2 -3.5 needs a 4+ round margin: only 7-13
        (3.5, SELECTION_TEAM1, 0.90, 0.0),
    ],
)
def test_round_handicap(bo3: MatchPrediction, line: float, sel: str, win: float, push: float) -> None:
    o = price(spec(MT.ROUND_HANDICAP, map_number=1, line=line), sel, bo3)
    assert o.p_win == pytest.approx(win)
    assert o.p_push == pytest.approx(push)


def test_round_handicap_map3_is_scaled_by_p_played(bo3: MatchPrediction) -> None:
    o = price(spec(MT.ROUND_HANDICAP, map_number=3, line=-4.5), SELECTION_TEAM1, bo3)
    # map 3 q = 0.5: P(margin >= 5) = (0.2 + 0.3) * 0.5 = 0.25
    assert o.p_win == pytest.approx(0.49 * 0.25)
    assert o.p_void == pytest.approx(0.51)


def test_total_rounds_with_and_without_overtime(bo3: MatchPrediction) -> None:
    s = spec(MT.TOTAL_ROUNDS, map_number=1, line=22.5)
    assert price(s, SELECTION_OVER, bo3).p_win == pytest.approx(0.60)
    assert price(s, SELECTION_UNDER, bo3).p_win == pytest.approx(0.40)
    s24 = spec(MT.TOTAL_ROUNDS, map_number=1, line=24.0)
    over = price(s24, SELECTION_OVER, bo3)
    under = price(s24, SELECTION_UNDER, bo3)
    assert (over.p_win, over.p_push, under.p_win) == (
        pytest.approx(0.20),
        pytest.approx(0.26),
        pytest.approx(0.54),
    )
    # regulation only: overtime maps count as 24 rounds
    reg_over = price(s24, SELECTION_OVER, bo3, include_overtime_in_totals=False)
    assert reg_over.p_win == pytest.approx(0.0) and reg_over.p_push == pytest.approx(0.46)
    s245 = spec(MT.TOTAL_ROUNDS, map_number=1, line=24.5)
    assert price(s245, SELECTION_OVER, bo3).p_win == pytest.approx(0.2)
    assert price(s245, SELECTION_UNDER, bo3, include_overtime_in_totals=False).p_win == pytest.approx(1.0)


def test_team_total_rounds(bo3: MatchPrediction) -> None:
    s = spec(MT.TEAM_TOTAL_ROUNDS, map_number=1, team=SELECTION_TEAM2, line=12.5)
    assert price(s, SELECTION_OVER, bo3).p_win == pytest.approx(0.52)
    assert price(s, SELECTION_OVER, bo3, include_overtime_in_totals=False).p_win == pytest.approx(0.32)
    s1 = spec(MT.TEAM_TOTAL_ROUNDS, map_number=1, team=SELECTION_TEAM1, line=10.5)
    # team1 > 10.5 rounds: 13-5, 13-8, 13-11, 16-14, 11-13, 14-16
    assert price(s1, SELECTION_OVER, bo3).p_win == pytest.approx(0.12 + 0.18 + 0.18 + 0.12 + 0.08 + 0.08)
    with pytest.raises(UnpriceableMarket, match="team"):
        price(spec(MT.TEAM_TOTAL_ROUNDS, map_number=1, line=10.5), SELECTION_OVER, bo3)


def test_odd_even(bo3: MatchPrediction) -> None:
    s = spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=1)
    assert price(s, SELECTION_ODD, bo3).p_win == pytest.approx(0.32)  # 21 and 23 rounds
    assert price(s, SELECTION_EVEN, bo3).p_win == pytest.approx(0.68)
    # a 16-13 overtime map is odd with OT but 24 (even) on regulation time
    pred = make_prediction(shift=None)
    slot = pred.central.slots[0].model_copy(update={"score_pmf": {"13-8": 0.5, "16-13": 0.5}})
    custom = pred.model_copy(
        update={"central": pred.central.model_copy(update={"slots": [slot, *pred.central.slots[1:]]})}
    )
    assert price(s, SELECTION_ODD, custom).p_win == pytest.approx(1.0)
    assert price(s, SELECTION_ODD, custom, include_overtime_in_totals=False).p_win == pytest.approx(0.5)
    with pytest.raises(UnpriceableMarket):
        price(s, SELECTION_OVER, bo3)


def test_map_correct_score(bo3: MatchPrediction) -> None:
    assert price(spec(MT.MAP_CORRECT_SCORE, map_number=1), "13-8", bo3).p_win == pytest.approx(0.18)
    m3 = price(spec(MT.MAP_CORRECT_SCORE, map_number=3), "13-8", bo3)
    assert m3.p_win == pytest.approx(0.49 * 0.15) and m3.p_void == pytest.approx(0.51)
    # feasible but absent from the pmf -> probability 0; impossible -> unpriceable
    assert price(spec(MT.MAP_CORRECT_SCORE, map_number=1), "13-0", bo3).p_win == 0.0
    assert price(spec(MT.MAP_CORRECT_SCORE, map_number=1), "19-17", bo3).p_win == 0.0
    for bad in ("13-12", "14-12", "16-15", "12-12", "17-14"):
        with pytest.raises(UnpriceableMarket, match="not a possible"):
            price(spec(MT.MAP_CORRECT_SCORE, map_number=1), bad, bo3)


def test_is_final_round_score() -> None:
    assert is_final_round_score(13, 11) and is_final_round_score(0, 13)
    assert is_final_round_score(16, 14) and is_final_round_score(12, 16) and is_final_round_score(19, 17)
    assert not is_final_round_score(13, 12)
    assert not is_final_round_score(16, 15)
    assert not is_final_round_score(15, 13)
    assert not is_final_round_score(22, 17)


def test_map_overtime_and_pistols(bo3: MatchPrediction) -> None:
    yes = price(spec(MT.MAP_OVERTIME, map_number=1), SELECTION_YES, bo3)
    no = price(spec(MT.MAP_OVERTIME, map_number=3), SELECTION_NO, bo3)
    assert yes.p_win == pytest.approx(0.2)
    assert no.p_win == pytest.approx(0.49 * 0.8) and no.p_void == pytest.approx(0.51)
    p1 = price(spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=1), SELECTION_TEAM1, bo3)
    p13 = price(spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=13), SELECTION_TEAM2, bo3)
    assert p1.p_win == pytest.approx(0.55) and p13.p_win == pytest.approx(0.52)
    with pytest.raises(UnpriceableMarket, match="pistol"):
        price(spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=7), SELECTION_TEAM1, bo3)
    with pytest.raises(UnpriceableMarket, match="pistol"):
        price(spec(MT.PISTOL_ROUND_WINNER, map_number=1), SELECTION_TEAM1, bo3)


def test_race_to_rounds(bo3: MatchPrediction) -> None:
    t1 = price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=5), SELECTION_TEAM1, bo3)
    t2 = price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=5), SELECTION_TEAM2, bo3)
    assert (t1.p_win, t2.p_win, t1.p_void) == (pytest.approx(0.58), pytest.approx(0.42), 0.0)
    # race to 16: only overtime maps get there; otherwise the bet is void
    a = price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=16), SELECTION_TEAM1, bo3)
    b = price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=16), SELECTION_TEAM2, bo3)
    assert a.p_win == pytest.approx(0.12) and b.p_win == pytest.approx(0.08)
    assert a.p_void == pytest.approx(0.8) and b.p_void == pytest.approx(0.8)
    with pytest.raises(UnpriceableMarket, match="race-to-9"):
        price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=9), SELECTION_TEAM1, bo3)
    with pytest.raises(UnpriceableMarket, match="whole number"):
        price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=5.5), SELECTION_TEAM1, bo3)


def test_race_to_beyond_regulation_needs_the_score_pmf() -> None:
    pred = make_prediction(shift=None)
    slot = pred.central.slots[0].model_copy(update={"score_pmf": {}})
    custom = pred.model_copy(
        update={"central": pred.central.model_copy(update={"slots": [slot, *pred.central.slots[1:]]})}
    )
    assert price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=5), SELECTION_TEAM1, custom).p_win == 0.58
    with pytest.raises(UnpriceableMarket, match="neither team"):
        price(spec(MT.RACE_TO_ROUNDS, map_number=1, line=16), SELECTION_TEAM1, custom)


def _with_slot1(pred: MatchPrediction, **update: object) -> MatchPrediction:
    slot = pred.central.slots[0].model_copy(update=update)
    return pred.model_copy(
        update={"central": pred.central.model_copy(update={"slots": [slot, *pred.central.slots[1:]]})}
    )


def test_race_to_prefers_the_published_neither_probability() -> None:
    pred = make_prediction(shift=None)
    s16 = spec(MT.RACE_TO_ROUNDS, map_number=1, line=16)
    # the simulator's own P(nobody reaches 16) wins over the value derived from the score pmf (0.8) ...
    explicit = _with_slot1(pred, p_neither_race_to={"16": 0.7}, p_team1_race_to={"16": 0.18})
    a, b = price(s16, SELECTION_TEAM1, explicit), price(s16, SELECTION_TEAM2, explicit)
    assert (a.p_win, a.p_void) == (pytest.approx(0.18), pytest.approx(0.7))
    assert (b.p_win, b.p_void) == (pytest.approx(0.12), pytest.approx(0.7))
    # ... and makes the market priceable without a score pmf
    no_pmf = _with_slot1(pred, score_pmf={}, p_neither_race_to={"16": 0.8})
    assert price(s16, SELECTION_TEAM2, no_pmf).p_win == pytest.approx(0.08)
    # inconsistent inputs are refused, not clipped
    broken = _with_slot1(pred, p_neither_race_to={"16": 0.95})
    with pytest.raises(UnpriceableMarket, match="inconsistent"):
        price(s16, SELECTION_TEAM2, broken)
    with pytest.raises(UnpriceableMarket, match="not a probability"):
        price(s16, SELECTION_TEAM1, _with_slot1(pred, p_neither_race_to={"16": 1.3}))


def test_round_handicap_on_regulation_time(bo3: MatchPrediction) -> None:
    # map 1 (q = 0.6): P(16-14) = 0.12 and P(14-16) = 0.08 count as 12-12 on regulation time
    s = spec(MT.ROUND_HANDICAP, map_number=1, line=-1.5)
    assert price(s, SELECTION_TEAM1, bo3).p_win == pytest.approx(0.60)
    reg1 = price(s, SELECTION_TEAM1, bo3, include_overtime_in_totals=False)
    reg2 = price(s, SELECTION_TEAM2, bo3, include_overtime_in_totals=False)
    assert reg1.p_win == pytest.approx(0.48) and reg2.p_win == pytest.approx(0.52)
    level = spec(MT.ROUND_HANDICAP, map_number=1, line=0.0)
    t1 = price(level, SELECTION_TEAM1, bo3, include_overtime_in_totals=False)
    assert (t1.p_win, t1.p_push) == (pytest.approx(0.48), pytest.approx(0.20))
    assert price(level, SELECTION_TEAM1, bo3).p_push == pytest.approx(0.0)


def test_settlement_rules_default_to_the_betclic_settings(
    bo3: MatchPrediction, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert market_rules() == (True, "push")  # the BetclicSettings defaults
    strict = BetclicSettings(h2h_tie_rule="lose", totals_include_overtime=False)
    assert market_rules(betclic=strict) == (False, "lose")
    assert market_rules(True, None, strict) == (True, "lose")  # explicit arguments win
    assert market_rules(None, "push", strict) == (False, "push")
    with pytest.raises(ValueError, match="h2h_tie"):
        market_rules(h2h_tie="draw")

    s24 = spec(MT.TOTAL_ROUNDS, map_number=1, line=24.0)
    assert price(s24, SELECTION_OVER, bo3).p_push == pytest.approx(0.26)
    assert price(s24, SELECTION_OVER, bo3, betclic=strict).p_push == pytest.approx(0.46)
    assert price(s24, SELECTION_OVER, bo3, betclic=strict, include_overtime_in_totals=True).p_push == (
        pytest.approx(0.26)
    )
    h2h = spec(MT.PLAYER_KILLS_H2H, map_number=1, player_id=101, opponent_player_id=201)
    assert price(h2h, player_selection(101), bo3).p_push > 0
    assert price(h2h, player_selection(101), bo3, betclic=strict).p_push == 0
    assert price_core(h2h, player_selection(101), bo3.central, fmt=bo3.format, betclic=strict).p_push == 0
    quote = make_quote(s24, SELECTION_OVER)
    [(priced, _)] = price_quotes(bo3, [quote], sigma_floor=FLOOR, betclic=strict)
    assert priced.p_push == pytest.approx(0.46)

    # without any argument the process settings (environment / .env) decide
    monkeypatch.setenv("CS2BOT_BETCLIC__TOTALS_INCLUDE_OVERTIME", "false")
    monkeypatch.setenv("CS2BOT_BETCLIC__H2H_TIE_RULE", "lose")
    reset_settings_cache()
    try:
        assert market_rules() == (False, "lose")
        assert price(s24, SELECTION_OVER, bo3).p_push == pytest.approx(0.46)
        assert price(h2h, player_selection(101), bo3).p_push == 0
    finally:
        # not monkeypatch.undo(): the autouse isolation fixture shares this monkeypatch instance
        monkeypatch.delenv("CS2BOT_BETCLIC__TOTALS_INCLUDE_OVERTIME")
        monkeypatch.delenv("CS2BOT_BETCLIC__H2H_TIE_RULE")
        reset_settings_cache()
    assert market_rules() == (True, "push")


def test_one_broken_quote_never_aborts_the_batch(
    bo3: MatchPrediction, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_: object) -> pricing_module.OutcomeSplit:
        raise KeyError("unexpected")

    monkeypatch.setitem(pricing_module._HANDLERS, MT.MAP_OVERTIME, boom)
    quotes = [
        make_quote(spec(MT.MAP_OVERTIME, map_number=1), SELECTION_YES),
        make_quote(spec(MT.MATCH_WINNER), SELECTION_TEAM1),
    ]
    with caplog.at_level(logging.WARNING, logger="cs2bot.engine.pricing"):
        report = price_quotes_report(bo3, quotes, sigma_floor=FLOOR)
    assert [q.spec.market_type for _, q in report.priced] == [MT.MATCH_WINNER]
    assert len(report.skipped) == 1 and "pricing error" in report.skipped[0][1]
    assert any("error while pricing" in r.message for r in caplog.records)
    # bad arguments are still the caller's problem
    with pytest.raises(ValueError, match="sigma_floor"):
        price_quotes_report(bo3, quotes, sigma_floor=-1.0)
    with pytest.raises(ValueError, match="h2h_tie"):
        price_quotes_report(bo3, quotes, sigma_floor=FLOOR, h2h_tie="maybe")


def test_half_winner(bo3: MatchPrediction) -> None:
    s = spec(MT.HALF_WINNER, map_number=1, half=1)
    t1, t2, d = (price(s, sel, bo3).p_win for sel in (SELECTION_TEAM1, SELECTION_TEAM2, SELECTION_DRAW))
    assert (t1, t2, d) == (pytest.approx(0.5), pytest.approx(0.3), pytest.approx(0.2))
    with pytest.raises(UnpriceableMarket, match="half"):
        price(spec(MT.HALF_WINNER, map_number=1, half=3), SELECTION_TEAM1, bo3)
    pred = make_prediction(shift=None)
    slot = pred.central.slots[0].model_copy(update={"p_half_draw": {}})
    custom = pred.model_copy(
        update={"central": pred.central.model_copy(update={"slots": [slot, *pred.central.slots[1:]]})}
    )
    assert price(s, SELECTION_TEAM1, custom).p_win == pytest.approx(0.5)
    with pytest.raises(UnpriceableMarket, match="draw"):
        price(s, SELECTION_TEAM2, custom)


# ------------------------------------------------------------------ player markets


def test_player_kills_scopes(bo3: MatchPrediction) -> None:
    # map 1, player 101 (slot projection): kills {13, 15, 17, 19}
    m1 = price(spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=15.5), SELECTION_OVER, bo3)
    assert m1.p_win == pytest.approx(0.5) and m1.p_void == 0.0
    push = price(spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=15.0), SELECTION_OVER, bo3)
    assert (push.p_win, push.p_push) == (pytest.approx(0.5), pytest.approx(0.25))
    under = price(spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=15.0), SELECTION_UNDER, bo3)
    assert (under.p_win, under.p_push) == (pytest.approx(0.25), pytest.approx(0.25))
    # map 3 is void when not played: kills {15, 17, 19, 21}
    m3 = price(spec(MT.PLAYER_KILLS, player_id=101, map_number=3, line=17.5), SELECTION_OVER, bo3)
    assert m3.p_win == pytest.approx(0.49 * 0.5) and m3.p_void == pytest.approx(0.51)
    # maps 1-2 are always played in a BO3
    m12 = price(spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(2, 1), line=33.0), SELECTION_OVER, bo3)
    assert (m12.p_win, m12.p_push, m12.p_void) == (pytest.approx(0.5), pytest.approx(0.3), 0.0)
    # maps 1-3 need map 3 to be played
    m123 = price(spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 2, 3), line=50.5), SELECTION_OVER, bo3)
    assert m123.p_win == pytest.approx(0.245) and m123.p_void == pytest.approx(0.51)
    # whole match
    full = price(spec(MT.PLAYER_KILLS, player_id=101, line=40.5), SELECTION_OVER, bo3)
    assert full.p_win == pytest.approx(0.5) and full.p_void == 0.0
    # single-element maps_scope == map_number
    single = price(spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1,), line=15.5), SELECTION_OVER, bo3)
    assert single.p_win == pytest.approx(m1.p_win)


def test_player_kills_unpriceable(bo3: MatchPrediction) -> None:
    with pytest.raises(UnpriceableMarket, match="player 999"):
        price(spec(MT.PLAYER_KILLS, player_id=999, map_number=1, line=15.5), SELECTION_OVER, bo3)
    with pytest.raises(UnpriceableMarket, match="player 202"):  # no maps:1,2 projection for 202
        price(spec(MT.PLAYER_KILLS, player_id=202, maps_scope=(1, 2), line=30.5), SELECTION_OVER, bo3)
    with pytest.raises(UnpriceableMarket, match="player_id"):
        price(spec(MT.PLAYER_KILLS, map_number=1, line=15.5), SELECTION_OVER, bo3)
    with pytest.raises(UnpriceableMarket, match="line"):
        price(spec(MT.PLAYER_KILLS, player_id=101, map_number=1), SELECTION_OVER, bo3)
    with pytest.raises(UnpriceableMarket, match="not all part"):
        price(spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 4), line=50.5), SELECTION_OVER, bo3)


def test_player_kills_h2h(bo3: MatchPrediction) -> None:
    # map 1: player 101 {13,15,17,19} vs player 201 {11,13,15,17}: 10/16 win, 3/16 tie, 3/16 lose
    s = spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=1)
    a = price(s, player_selection(101), bo3)
    b = price(s, player_selection(201), bo3)
    assert (a.p_win, a.p_push) == (pytest.approx(10 / 16), pytest.approx(3 / 16))
    assert (b.p_win, b.p_push) == (pytest.approx(3 / 16), pytest.approx(3 / 16))
    lose = price(s, player_selection(101), bo3, h2h_tie="lose")
    assert (lose.p_win, lose.p_push, lose.p_lose) == (pytest.approx(10 / 16), 0.0, pytest.approx(6 / 16))
    # a -1.5 kills handicap on player 101 removes ties
    hc = spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=1, line=-1.5)
    assert price(hc, player_selection(101), bo3).p_win == pytest.approx(10 / 16)
    assert price(hc, player_selection(201), bo3).p_win == pytest.approx(6 / 16)
    with pytest.raises(UnpriceableMarket, match="neither player"):
        price(s, player_selection(103), bo3)
    with pytest.raises(ValueError, match="h2h_tie"):
        price(s, player_selection(101), bo3, h2h_tie="half")


def test_top_fragger(bo3: MatchPrediction) -> None:
    assert price(spec(MT.TOP_FRAGGER), player_selection(101), bo3).p_win == pytest.approx(0.3)
    team = price(spec(MT.TOP_FRAGGER, team=SELECTION_TEAM1), player_selection(101), bo3)
    assert team.p_win == pytest.approx(0.45)
    m1 = price(spec(MT.TOP_FRAGGER, map_number=1), player_selection(201), bo3)
    assert m1.p_win == pytest.approx(0.3) and m1.p_void == 0.0
    with pytest.raises(UnpriceableMarket, match="map:3"):
        price(spec(MT.TOP_FRAGGER, map_number=3), player_selection(101), bo3)
    with pytest.raises(UnpriceableMarket, match="not in the top-fragger"):
        price(spec(MT.TOP_FRAGGER), player_selection(105), bo3)
    with pytest.raises(UnpriceableMarket, match="not in the top-fragger"):
        price(spec(MT.TOP_FRAGGER, team=SELECTION_TEAM1), player_selection(201), bo3)
    # a corrupted team table listing a team2 player is caught by the projection's team
    top = dict(bo3.central.top_fragger)
    top["match|team1"] = {**top["match|team1"], "101": 0.4, "201": 0.05}
    corrupt = bo3.model_copy(update={"central": bo3.central.model_copy(update={"top_fragger": top})})
    with pytest.raises(UnpriceableMarket, match="plays for team2"):
        price(spec(MT.TOP_FRAGGER, team=SELECTION_TEAM1), player_selection(201), corrupt)


def test_top_fragger_team_scoped_sums_to_one(bo3: MatchPrediction) -> None:
    s = spec(MT.TOP_FRAGGER, team=SELECTION_TEAM2)
    total = sum(price(s, player_selection(pid), bo3).p_win for pid in (201, 202, 203, 204, 205))
    assert total == pytest.approx(1.0)


# ------------------------------------------------------------------ structural properties

COMPLEMENTARY_GROUPS: list[tuple[MatchFormat, MarketSpec, list[str]]] = [
    (MatchFormat.BO3, spec(MT.MATCH_WINNER), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO2, spec(MT.MATCH_WINNER), [SELECTION_TEAM1, SELECTION_TEAM2, SELECTION_DRAW]),
    (MatchFormat.BO1, spec(MT.MATCH_WINNER), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO5, spec(MT.MAP_WINNER, map_number=5), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO3, spec(MT.MAP_WINNER, map_number=3), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO3, spec(MT.CORRECT_MAP_SCORE), ["2-0", "2-1", "1-2", "0-2"]),
    (MatchFormat.BO5, spec(MT.CORRECT_MAP_SCORE), ["3-0", "3-1", "3-2", "2-3", "1-3", "0-3"]),
    (MatchFormat.BO3, spec(MT.MAP_HANDICAP, line=-1.5), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO3, spec(MT.MAP_HANDICAP, line=-1.0), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO2, spec(MT.MAP_HANDICAP, line=0.0), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO5, spec(MT.MAP_HANDICAP, line=2.0), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO3, spec(MT.TOTAL_MAPS, line=2.5), [SELECTION_OVER, SELECTION_UNDER]),
    (MatchFormat.BO5, spec(MT.TOTAL_MAPS, line=4.0), [SELECTION_OVER, SELECTION_UNDER]),
    (MatchFormat.BO3, spec(MT.BOTH_TEAMS_WIN_A_MAP), [SELECTION_YES, SELECTION_NO]),
    (MatchFormat.BO3, spec(MT.ROUND_HANDICAP, map_number=2, line=-2.0), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO3, spec(MT.ROUND_HANDICAP, map_number=3, line=3.5), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO3, spec(MT.TOTAL_ROUNDS, map_number=3, line=24.0), [SELECTION_OVER, SELECTION_UNDER]),
    (MatchFormat.BO1, spec(MT.TOTAL_ROUNDS, map_number=1, line=22.5), [SELECTION_OVER, SELECTION_UNDER]),
    (
        MatchFormat.BO3,
        spec(MT.TEAM_TOTAL_ROUNDS, map_number=3, team=SELECTION_TEAM1, line=12.0),
        [SELECTION_OVER, SELECTION_UNDER],
    ),
    (MatchFormat.BO3, spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=3), [SELECTION_ODD, SELECTION_EVEN]),
    (
        MatchFormat.BO3,
        spec(MT.MAP_CORRECT_SCORE, map_number=3),
        ["13-5", "13-8", "13-11", "16-14", "7-13", "10-13", "11-13", "14-16"],
    ),
    (MatchFormat.BO3, spec(MT.MAP_OVERTIME, map_number=3), [SELECTION_YES, SELECTION_NO]),
    (
        MatchFormat.BO3,
        spec(MT.PISTOL_ROUND_WINNER, map_number=3, round_number=13),
        [SELECTION_TEAM1, SELECTION_TEAM2],
    ),
    (MatchFormat.BO3, spec(MT.RACE_TO_ROUNDS, map_number=3, line=16), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (MatchFormat.BO3, spec(MT.RACE_TO_ROUNDS, map_number=2, line=5), [SELECTION_TEAM1, SELECTION_TEAM2]),
    (
        MatchFormat.BO3,
        spec(MT.HALF_WINNER, map_number=3, half=2),
        [SELECTION_TEAM1, SELECTION_TEAM2, SELECTION_DRAW],
    ),
    (
        MatchFormat.BO3,
        spec(MT.PLAYER_KILLS, player_id=201, map_number=3, line=14.0),
        [SELECTION_OVER, SELECTION_UNDER],
    ),
    (
        MatchFormat.BO3,
        spec(MT.PLAYER_KILLS_H2H, player_id=102, opponent_player_id=202, map_number=3),
        [player_selection(102), player_selection(202)],
    ),
    (
        MatchFormat.BO5,
        spec(MT.TOP_FRAGGER, team=SELECTION_TEAM1),
        [player_selection(p) for p in (101, 102, 103, 104, 105)],
    ),
]


@pytest.mark.parametrize(("fmt", "market", "selections"), COMPLEMENTARY_GROUPS, ids=lambda x: str(x))
def test_complementary_selections_sum_to_settled_mass(
    fmt: MatchFormat, market: MarketSpec, selections: list[str]
) -> None:
    pred = make_prediction(fmt)
    outcomes = [price(market, sel, pred) for sel in selections]
    pushes = {round(o.p_push, 12) for o in outcomes}
    voids = {round(o.p_void, 12) for o in outcomes}
    assert len(pushes) == 1 and len(voids) == 1, "push/void must not depend on the selection"
    total = sum(o.p_win for o in outcomes)
    assert total == pytest.approx(1.0 - outcomes[0].p_push - outcomes[0].p_void, abs=1e-9)
    for o in outcomes:
        assert 0.0 <= o.p_win <= 1.0 and o.p_win + o.p_push + o.p_void <= 1.0 + 1e-12
        assert o.sigma >= FLOOR


MIRROR_CASES: list[tuple[MarketSpec, str]] = [
    (spec(MT.MATCH_WINNER), SELECTION_TEAM1),
    (spec(MT.MAP_WINNER, map_number=2), SELECTION_TEAM2),
    (spec(MT.MAP_WINNER, map_number=3), SELECTION_TEAM1),
    (spec(MT.CORRECT_MAP_SCORE), "2-1"),
    (spec(MT.MAP_HANDICAP, line=-1.5), SELECTION_TEAM1),
    (spec(MT.MAP_HANDICAP, line=1.5), SELECTION_TEAM1),
    (spec(MT.MAP_HANDICAP, line=-1.0), SELECTION_TEAM2),
    (spec(MT.TOTAL_MAPS, line=2.5), SELECTION_OVER),
    (spec(MT.BOTH_TEAMS_WIN_A_MAP), SELECTION_YES),
    (spec(MT.ROUND_HANDICAP, map_number=1, line=-4.5), SELECTION_TEAM1),
    (spec(MT.ROUND_HANDICAP, map_number=3, line=2.0), SELECTION_TEAM2),
    (spec(MT.TOTAL_ROUNDS, map_number=2, line=22.5), SELECTION_UNDER),
    (spec(MT.TEAM_TOTAL_ROUNDS, map_number=1, team=SELECTION_TEAM1, line=10.5), SELECTION_OVER),
    (spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=1), SELECTION_ODD),
    (spec(MT.MAP_CORRECT_SCORE, map_number=1), "13-8"),
    (spec(MT.MAP_CORRECT_SCORE, map_number=3), "14-16"),
    (spec(MT.MAP_OVERTIME, map_number=2), SELECTION_YES),
    (spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=1), SELECTION_TEAM1),
    (spec(MT.RACE_TO_ROUNDS, map_number=1, line=5), SELECTION_TEAM2),
    (spec(MT.RACE_TO_ROUNDS, map_number=1, line=16), SELECTION_TEAM1),
    (spec(MT.HALF_WINNER, map_number=2, half=1), SELECTION_TEAM1),
    (spec(MT.HALF_WINNER, map_number=2, half=1), SELECTION_DRAW),
    (spec(MT.PLAYER_KILLS, player_id=101, map_number=3, line=17.5), SELECTION_OVER),
    (spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=1), player_selection(201)),
    (spec(MT.TOP_FRAGGER, team=SELECTION_TEAM2), player_selection(202)),
]


@pytest.mark.parametrize(("market", "selection"), MIRROR_CASES, ids=lambda x: str(x))
def test_orientation_mirror(bo3: MatchPrediction, market: MarketSpec, selection: str) -> None:
    """Swapping team1/team2 everywhere (prediction, spec, selection) must not change any price."""
    mirrored = mirror_prediction(bo3)
    a = price(market, selection, bo3)
    b = price(mirror_spec(market), mirror_selection(selection), mirrored)
    assert (a.p_win, a.p_push, a.p_void) == (
        pytest.approx(b.p_win, abs=APPROX),
        pytest.approx(b.p_push, abs=APPROX),
        pytest.approx(b.p_void, abs=APPROX),
    )
    assert a.sigma == pytest.approx(b.sigma, abs=APPROX)


# ------------------------------------------------------------------ uncertainty


def test_sigma_floor_applies() -> None:
    pred = make_prediction(shift=0.0001)
    o = price_outcome(spec(MT.MATCH_WINNER), SELECTION_TEAM1, pred, sigma_floor=0.03)
    assert o.sigma == 0.03
    with pytest.raises(ValueError, match="sigma_floor"):
        price_outcome(spec(MT.MATCH_WINNER), SELECTION_TEAM1, pred, sigma_floor=-0.1)


def test_sigma_with_one_scenario() -> None:
    pred = make_prediction(shift=0.05)
    only_low = pred.model_copy(update={"high": None})
    o = price(spec(MT.MATCH_WINNER), SELECTION_TEAM1, only_low)
    assert o.sigma == pytest.approx(0.575 - 0.5)


def test_sigma_fallback_without_scenarios() -> None:
    pred = make_prediction(shift=None, sigma_team1_win=0.04)
    mw = price(spec(MT.MATCH_WINNER), SELECTION_TEAM1, pred)
    assert mw.sigma == pytest.approx(0.04)  # the fallback reproduces the match-winner sigma
    m1 = price(spec(MT.MAP_WINNER, map_number=1), SELECTION_TEAM1, pred)
    assert m1.sigma == pytest.approx(0.6 * 0.4 * 0.04 / (0.575 * 0.425))
    assert fallback_sigma(0.0, pred) == 0.0
    # the logit-scale sigma is capped
    wild = make_prediction(shift=None, sigma_team1_win=5.0)
    assert fallback_sigma(0.5, wild) == pytest.approx(0.25 * 2.0)


def test_sigma_falls_back_when_scenarios_cannot_price_the_outcome() -> None:
    pred = make_prediction(shift=0.05)
    bare = make_prediction(shift=0.05, with_players=False)
    mixed = pred.model_copy(update={"low": bare.low, "high": bare.high})
    s = spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=15.5)
    o = price(s, SELECTION_OVER, mixed)
    assert o.p_win == pytest.approx(0.5)
    assert o.sigma == pytest.approx(fallback_sigma(0.5, mixed))


# ------------------------------------------------------------------ robustness


def test_other_market_and_bad_inputs(bo3: MatchPrediction) -> None:
    with pytest.raises(UnpriceableMarket, match="never priced"):
        price(spec(MT.OTHER), SELECTION_TEAM1, bo3)
    with pytest.raises(UnpriceableMarket):
        price(spec(MT.MAP_WINNER, map_number=1), SELECTION_OVER, bo3)
    with pytest.raises(UnpriceableMarket):
        price(spec(MT.MAP_OVERTIME, map_number=1), SELECTION_TEAM1, bo3)


def _with_map_pmf(pred: MatchPrediction, pmf: dict[str, float]) -> MatchPrediction:
    return pred.model_copy(update={"central": pred.central.model_copy(update={"map_score_pmf": pmf})})


def test_broken_distributions_are_unpriceable(bo3: MatchPrediction) -> None:
    half = _with_map_pmf(bo3, {"2-0": 0.25, "0-2": 0.25})
    with pytest.raises(UnpriceableMarket, match="sums to"):
        price(spec(MT.CORRECT_MAP_SCORE), "2-0", half)
    negative = _with_map_pmf(bo3, {"2-0": 1.2, "0-2": -0.2})
    with pytest.raises(UnpriceableMarket, match="not a probability"):
        price(spec(MT.TOTAL_MAPS, line=2.5), SELECTION_OVER, negative)
    empty = _with_map_pmf(bo3, {})
    with pytest.raises(UnpriceableMarket, match="empty"):
        price(spec(MT.BOTH_TEAMS_WIN_A_MAP), SELECTION_YES, empty)
    bad_key = _with_map_pmf(bo3, {"two-zero": 1.0})
    with pytest.raises(UnpriceableMarket, match="invalid score key"):
        price(spec(MT.MAP_HANDICAP, line=-1.5), SELECTION_TEAM1, bad_key)
    # a small deficit (truncated tail) is renormalised
    slightly = _with_map_pmf(bo3, {"2-0": 0.33, "2-1": 0.24, "1-2": 0.24, "0-2": 0.18})
    o = price(spec(MT.CORRECT_MAP_SCORE), "2-0", slightly)
    assert o.p_win == pytest.approx(0.33 / 0.99)
    bad_prob = bo3.model_copy(update={"central": bo3.central.model_copy(update={"p_team1_win": 1.3})})
    with pytest.raises(UnpriceableMarket, match="not a probability"):
        price(spec(MT.MATCH_WINNER), SELECTION_TEAM1, bad_prob)


def test_missing_slot_is_unpriceable(bo3: MatchPrediction) -> None:
    pred = bo3.model_copy(update={"central": bo3.central.model_copy(update={"slots": bo3.central.slots[:2]})})
    with pytest.raises(UnpriceableMarket, match="no slot for map 3"):
        price(spec(MT.MAP_WINNER, map_number=3), SELECTION_TEAM1, pred)


def test_price_core_matches_central(bo3: MatchPrediction) -> None:
    s = spec(MT.MAP_WINNER, map_number=3)
    split = price_core(s, SELECTION_TEAM1, bo3.central, fmt=bo3.format)
    o = price(s, SELECTION_TEAM1, bo3)
    assert (split.p_win, split.p_void) == (o.p_win, o.p_void)
    assert split.p_lose == pytest.approx(o.p_lose)


def test_price_quotes_skips_unpriceable_and_logs(
    bo3: MatchPrediction, caplog: pytest.LogCaptureFixture
) -> None:
    good = make_quote(spec(MT.MATCH_WINNER), SELECTION_TEAM1)
    other = make_quote(spec(MT.OTHER), "x")
    missing = make_quote(spec(MT.PLAYER_KILLS, player_id=999, map_number=1, line=10.5), SELECTION_OVER)
    with caplog.at_level(logging.INFO, logger="cs2bot.engine.pricing"):
        pairs = price_quotes(bo3, [good, other, missing], sigma_floor=FLOOR)
    assert [q.selection_id for _, q in pairs] == [good.selection_id]
    assert pairs[0][0].p_win == pytest.approx(0.575)
    text = caplog.text
    assert "never priced" in text and "player 999" in text
    report = price_quotes_report(bo3, [good, other, missing], sigma_floor=FLOOR)
    assert len(report.priced) == 1
    assert [q.selection_id for q, _ in report.skipped] == [other.selection_id, missing.selection_id]
    assert "player 999" in report.skipped[1][1]


def test_sigma_curvature_guard_for_outcomes_peaking_at_the_centre() -> None:
    """P(overtime) is highest for even teams: both scenarios lower it, the central difference is ~0."""
    pred = make_prediction(MatchFormat.BO1, [0.5], shift=0.1)
    slot_c = pred.central.slots[0].model_copy(update={"p_overtime": 0.30})
    lo = pred.low.slots[0].model_copy(update={"p_overtime": 0.26})  # type: ignore[union-attr]
    hi = pred.high.slots[0].model_copy(update={"p_overtime": 0.26})  # type: ignore[union-attr]
    peaked = pred.model_copy(
        update={
            "central": pred.central.model_copy(update={"slots": [slot_c]}),
            "low": pred.low.model_copy(update={"slots": [lo]}),  # type: ignore[union-attr]
            "high": pred.high.model_copy(update={"slots": [hi]}),  # type: ignore[union-attr]
        }
    )
    o = price(spec(MT.MAP_OVERTIME, map_number=1), SELECTION_YES, peaked)
    assert o.p_win == pytest.approx(0.30)
    assert o.sigma == pytest.approx(0.08 / 2**0.5)  # sqrt(2) |c|, c = (0.26 + 0.26 - 0.60) / 2
    # monotone outcomes keep the plain central difference
    assert price(spec(MT.MAP_WINNER, map_number=1), SELECTION_TEAM1, peaked).sigma == pytest.approx(0.1)
