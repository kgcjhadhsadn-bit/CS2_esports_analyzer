"""Pricing against what ``cs2bot.models.match_engine.build_match_prediction`` REALLY produces.

The other pricing tests use hand-built predictions; these run the real simulator (small simulation
counts) and check that every market type is priceable from its output and that the producer's
conventions are the ones pricing assumes: scope keys, conditional vs unconditional kill pmfs,
top-fragger keys, race-to keys, BO2 draws, low / high scenarios.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.predictions import MapSequenceProbability, MapWinEstimate, MatchPrediction
from cs2bot.engine import pricing
from cs2bot.models.map_sim import MapSimulator
from cs2bot.models.match_engine import PlayerInput, build_match_prediction
from cs2bot.models.player_props import PlayerKillModel
from cs2bot.models.round_model import RoundModel

MT = MarketType
MAPS = ["mirage", "inferno", "nuke", "dust2", "ancient"]
TEAM1 = (100, 101, 102, 103, 104)
TEAM2 = (200, 201, 202, 203, 204)
FLOOR = 0.01


def spec(market_type: MarketType, **kw: object) -> MarketSpec:
    return MarketSpec(market_type=market_type, **kw)


def price(pred: MatchPrediction, market: MarketSpec, selection: str) -> pricing.OutcomeSplit:
    out = pricing.price_outcome(
        market, selection, pred, sigma_floor=FLOOR, include_overtime_in_totals=True, h2h_tie="push"
    )
    assert out.sigma >= FLOOR
    assert 0.0 <= out.p_win <= 1.0 and 0.0 <= out.p_push <= 1.0 and 0.0 <= out.p_void <= 1.0
    return pricing.OutcomeSplit(out.p_win, out.p_push, out.p_void)


@pytest.fixture(scope="module", params=list(MatchFormat), ids=lambda f: f.value)
def real(request: pytest.FixtureRequest) -> MatchPrediction:
    fmt: MatchFormat = request.param
    n = fmt.n_maps
    maps = MAPS[:n]
    other = list(reversed(maps)) if n > 1 else ["nuke"]
    players = [PlayerInput(player_id=p, team=TeamSlot.TEAM1) for p in TEAM1]
    players += [PlayerInput(player_id=p, team=TeamSlot.TEAM2) for p in TEAM2]
    return build_match_prediction(
        match_id=1,
        fmt=fmt,
        team1_id=1,
        team2_id=2,
        veto_distribution=[
            MapSequenceProbability(maps=maps, pickers=[None] * n, probability=0.7),
            MapSequenceProbability(maps=other, pickers=[None] * n, probability=0.3),
        ],
        map_estimates={
            m: MapWinEstimate(map_name=m, p_team1_win=0.52 + 0.04 * i, sigma=0.04) for i, m in enumerate(MAPS)
        },
        round_model=RoundModel.default(),
        players=players,
        kill_model=PlayerKillModel().fit([]),  # no history: every player gets the prior rate
        n_sims=1500,
        seed=11,
        model_version="engine-test",
        created_at=datetime(2026, 11, 20, 18, 0, tzinfo=UTC),
        simulator=MapSimulator(),
    )


def test_every_market_type_is_priceable_from_the_real_output(real: MatchPrediction) -> None:
    fmt = real.format
    teams = ("team1", "team2")
    groups: list[tuple[MarketSpec, tuple[str, ...]]] = [
        (spec(MT.MATCH_WINNER), (*teams, "draw") if fmt is MatchFormat.BO2 else teams),
        (spec(MT.CORRECT_MAP_SCORE), tuple(f"{a}-{b}" for a, b in sorted(pricing.final_map_scores(fmt)))),
        (spec(MT.MAP_HANDICAP, line=-1.5), teams),
        (spec(MT.MAP_HANDICAP, line=1.0), teams),
        (spec(MT.TOTAL_MAPS, line=2.5), ("over", "under")),
        (spec(MT.BOTH_TEAMS_WIN_A_MAP), ("yes", "no")),
        (spec(MT.PLAYER_KILLS, player_id=100, line=30.5), ("over", "under")),
        (spec(MT.PLAYER_KILLS_H2H, player_id=100, opponent_player_id=200), ("player:100", "player:200")),
        (spec(MT.TOP_FRAGGER), tuple(f"player:{p}" for p in (*TEAM1, *TEAM2))),
        (spec(MT.TOP_FRAGGER, team="team1"), tuple(f"player:{p}" for p in TEAM1)),
        (spec(MT.TOP_FRAGGER, team="team2"), tuple(f"player:{p}" for p in TEAM2)),
    ]
    for k in range(1, fmt.n_maps + 1):
        groups += [
            (spec(MT.MAP_WINNER, map_number=k), teams),
            (spec(MT.ROUND_HANDICAP, map_number=k, line=-2.5), teams),
            (spec(MT.ROUND_HANDICAP, map_number=k, line=3.0), teams),
            (spec(MT.TOTAL_ROUNDS, map_number=k, line=21.5), ("over", "under")),
            (spec(MT.TOTAL_ROUNDS, map_number=k, line=22.0), ("over", "under")),
            (spec(MT.TEAM_TOTAL_ROUNDS, map_number=k, team="team2", line=10.5), ("over", "under")),
            (spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=k), ("odd", "even")),
            (spec(MT.MAP_OVERTIME, map_number=k), ("yes", "no")),
            (spec(MT.PISTOL_ROUND_WINNER, map_number=k, round_number=1), teams),
            (spec(MT.PISTOL_ROUND_WINNER, map_number=k, round_number=13), teams),
            (spec(MT.RACE_TO_ROUNDS, map_number=k, line=5.0), teams),
            (spec(MT.RACE_TO_ROUNDS, map_number=k, line=13.0), teams),
            (spec(MT.HALF_WINNER, map_number=k, half=1), (*teams, "draw")),
            (spec(MT.HALF_WINNER, map_number=k, half=2), (*teams, "draw")),
            (spec(MT.PLAYER_KILLS, player_id=203, map_number=k, line=14.5), ("over", "under")),
            (
                spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=k, line=1.5),
                ("player:101", "player:201"),
            ),
            (spec(MT.TOP_FRAGGER, map_number=k), tuple(f"player:{p}" for p in (*TEAM1, *TEAM2))),
            (spec(MT.TOP_FRAGGER, map_number=k, team="team1"), tuple(f"player:{p}" for p in TEAM1)),
        ]
    if fmt.min_maps >= 2:  # maps 1 and 2 are always played
        groups += [
            (spec(MT.PLAYER_KILLS, player_id=100, maps_scope=(1, 2), line=29.5), ("over", "under")),
            (spec(MT.TOP_FRAGGER, maps_scope=(1, 2)), tuple(f"player:{p}" for p in (*TEAM1, *TEAM2))),
        ]
    for market, selections in groups:
        splits = [price(real, market, s) for s in selections]  # raises if anything is unpriceable
        first = splits[0]
        assert all(s.p_push == pytest.approx(first.p_push) for s in splits), market.key()
        assert all(s.p_void == pytest.approx(first.p_void) for s in splits), market.key()
        # the selections of a market share the settled mass
        assert sum(s.p_win for s in splits) == pytest.approx(1.0 - first.p_push - first.p_void, abs=1e-6), (
            market.key()
        )
    # every map correct score in the simulated pmf is priceable and the scores share the played mass
    slot = real.central.slots[0]
    total = sum(price(real, spec(MT.MAP_CORRECT_SCORE, map_number=1), key).p_win for key in slot.score_pmf)
    assert total == pytest.approx(1.0, abs=1e-6)
    assert set(MT) - {m.market_type for m, _ in groups} == {MT.MAP_CORRECT_SCORE, MT.OTHER}


def test_void_probabilities_follow_the_series_structure(real: MatchPrediction) -> None:
    fmt = real.format
    scores = dict(real.central.map_score_pmf)
    for k in range(1, fmt.n_maps + 1):
        void = price(real, spec(MT.MAP_WINNER, map_number=k), "team1").p_void
        if k <= fmt.min_maps:
            assert void == pytest.approx(0.0, abs=1e-9)
        else:  # played only if nobody has won the series after k - 1 maps
            need = fmt.maps_to_win
            assert need is not None
            decided_early = sum(p for key, p in scores.items() if sum(int(x) for x in key.split("-")) < k)
            assert void == pytest.approx(decided_early, abs=1e-9)
            assert 0.0 < void < 1.0
    if fmt is MatchFormat.BO2:
        draw = price(real, spec(MT.MATCH_WINNER), "draw").p_win
        assert draw == pytest.approx(scores["1-1"]) and draw > 0.2
    else:
        with pytest.raises(pricing.UnpriceableMarket):
            price(real, spec(MT.MATCH_WINNER), "draw")


def test_kill_scopes_are_conditional_per_map_and_unconditional_for_the_match(real: MatchPrediction) -> None:
    """``map:k`` pmfs are conditional on the map being played (pricing multiplies by p_played);
    the ``match`` pmf is unconditional: its mean is the p_played-weighted sum of the map means."""
    core = real.central
    by_scope = core.player_kills_by_scope
    assert "match" in by_scope
    for pid in ("100", "203"):
        per_map = [by_scope[f"map:{s.map_number}"][pid].kills_mean for s in core.slots]
        assert all(9.0 < mean < 25.0 for mean in per_map)  # a full map's worth of kills in every slot
        expected = sum(s.p_played * mean for s, mean in zip(core.slots, per_map, strict=True))
        assert by_scope["match"][pid].kills_mean == pytest.approx(expected, rel=0.02)
        for s in core.slots:  # the slot's own table and the scope table are the same thing
            assert s.players[pid].kills_pmf == by_scope[f"map:{s.map_number}"][pid].kills_pmf
        if real.format.min_maps >= 2:
            assert by_scope["maps:1,2"][pid].kills_mean == pytest.approx(per_map[0] + per_map[1], rel=0.02)
    # consequence for pricing: an "over" on the last, optional map can win at most when it is played
    last = core.slots[-1]
    over = price(real, spec(MT.PLAYER_KILLS, player_id=100, map_number=last.map_number, line=0.5), "over")
    assert over.p_void == pytest.approx(1.0 - last.p_played, abs=1e-9)
    assert over.p_win == pytest.approx(last.p_played, abs=1e-3)  # nearly always more than 0 kills
    # the unconditional match scope never voids
    assert price(real, spec(MT.PLAYER_KILLS, player_id=100, line=0.5), "over").p_void == 0.0


def test_top_fragger_tables_and_keys(real: MatchPrediction) -> None:
    top = real.central.top_fragger
    expected = {"match", "match|team1", "match|team2"}
    expected |= {
        f"map:{k}{suffix}" for k in range(1, real.format.n_maps + 1) for suffix in ("", "|team1", "|team2")
    }
    assert expected <= set(top)
    for key, table in top.items():
        assert sum(table.values()) == pytest.approx(1.0), key  # dead heats already split
        ids = {int(p) for p in table}
        if key.endswith("|team1"):
            assert ids == set(TEAM1)
        elif key.endswith("|team2"):
            assert ids == set(TEAM2)
    # a player cannot be priced in the other team's market
    with pytest.raises(pricing.UnpriceableMarket):
        price(real, spec(MT.TOP_FRAGGER, team="team1"), "player:200")


def test_race_to_beyond_regulation_is_unpriceable_until_the_simulator_publishes_it(
    real: MatchPrediction,
) -> None:
    """The simulator publishes ``p_team1_race_to`` only for targets every map reaches (N <= 13), so
    overtime targets are skipped rather than guessed. If it starts publishing them (with
    ``p_neither_race_to``) the market becomes priceable with a void share - both are fine."""
    slot = real.central.slots[0]
    assert set(slot.p_team1_race_to) >= {"5", "13"}
    market = spec(MT.RACE_TO_ROUNDS, map_number=1, line=16.0)
    if "16" in slot.p_team1_race_to:
        assert price(real, market, "team1").p_void > 0.5
    else:
        with pytest.raises(pricing.UnpriceableMarket, match="race-to-16"):
            price(real, market, "team1")


def test_scenarios_give_a_strength_driven_sigma(real: MatchPrediction) -> None:
    assert real.low is not None and real.high is not None
    winner = pricing.price_outcome(spec(MT.MATCH_WINNER), "team1", real, sigma_floor=0.0)
    lo = pricing.price_core(spec(MT.MATCH_WINNER), "team1", real.low, fmt=real.format).p_win
    hi = pricing.price_core(spec(MT.MATCH_WINNER), "team1", real.high, fmt=real.format).p_win
    assert lo < winner.p_win < hi
    assert winner.sigma == pytest.approx((hi - lo) / 2.0, rel=0.35)  # the curvature guard can only raise it
    assert winner.sigma >= (hi - lo) / 2.0 - 1e-12
    assert winner.sigma == pytest.approx(real.sigma_team1_win, rel=0.35)
