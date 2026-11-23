"""Producer/consumer contract: real ``build_match_prediction`` output priced by ``engine.pricing``.

Every market type of :mod:`cs2bot.domain.markets` (except OTHER) is priced through
:func:`cs2bot.engine.pricing.price_outcome` for BO1 / BO2 / BO3 / BO5 predictions with known and unknown
vetoes and full lineups. Checked for every market:

* nothing that the prediction should support is unpriceable;
* complementary selections sum to ``1 - p_push - p_void`` (the same void/push mass on each side);
* for team1-favouring outcomes the low / high scenarios bracket the central value;
* the pricing sigma is strictly positive.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from cs2bot.domain.enums import MatchFormat, Side, TeamSlot
from cs2bot.domain.markets import MarketSpec, MarketType, player_selection
from cs2bot.domain.predictions import MapSequenceProbability, MatchPrediction
from cs2bot.domain.records import PlayerMapRecord
from cs2bot.engine.pricing import OutcomeSplit, UnpriceableMarket, price_core, price_outcome
from cs2bot.models.map_sim import MapSimulator
from cs2bot.models.match_engine import build_match_prediction
from cs2bot.models.player_props import PlayerInput, PlayerKillModel
from cs2bot.models.round_model import RoundModel
from cs2bot.testing.synthetic import SynthUniverse
from tests.models.sim.sim_helpers import AS_OF, estimates, lineup, veto_sequences

N_SIMS = 1500
SIGMA = 0.06
RULES: dict[str, object] = {"include_overtime_in_totals": True, "h2h_tie": "push"}


@dataclass(frozen=True)
class Case:
    name: str
    fmt: MatchFormat
    known_veto: bool


CASES = [
    Case("bo1-known", MatchFormat.BO1, True),
    Case("bo1-unknown", MatchFormat.BO1, False),
    Case("bo2-unknown", MatchFormat.BO2, False),
    Case("bo3-known", MatchFormat.BO3, True),
    Case("bo3-unknown", MatchFormat.BO3, False),
    Case("bo5-unknown", MatchFormat.BO5, False),
]


def _prediction(
    case: Case, uni: SynthUniverse, round_model: RoundModel, kill_model: PlayerKillModel
) -> tuple[MatchPrediction, list[PlayerInput]]:
    pool = list(uni.map_pool)
    n = case.fmt.n_maps
    if case.known_veto:
        seqs = [
            MapSequenceProbability(
                maps=pool[:n], pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None][:n], probability=1.0
            )
        ]
    else:
        seqs = veto_sequences(pool, n, n_seq=5, seed=n)
    probs = {m: 0.35 + 0.05 * i for i, m in enumerate(sorted(pool))}
    players = lineup(uni, 1, 2)
    pred = build_match_prediction(
        match_id=1,
        fmt=case.fmt,
        team1_id=1,
        team2_id=2,
        veto_distribution=seqs,
        map_estimates=estimates(probs, sigma=SIGMA),
        round_model=round_model,
        start_sides={pool[0]: Side.CT},
        players=players,
        kill_model=kill_model,
        n_sims=N_SIMS,
        scenario_n_sims=N_SIMS,
        top_fragger_samples=4000,
        seed=11,
        model_version="contract",
        created_at=AS_OF,
        veto_known=case.known_veto,
        simulator=MapSimulator(),
    )
    return pred, players


@pytest.fixture(scope="module")
def predictions(
    synthetic_universe: SynthUniverse, fitted_round_model: RoundModel, fitted_kill_model: PlayerKillModel
) -> dict[str, tuple[MatchPrediction, list[PlayerInput]]]:
    return {c.name: _prediction(c, synthetic_universe, fitted_round_model, fitted_kill_model) for c in CASES}


# ---------------------------------------------------------------------------------------------- markets


@dataclass(frozen=True)
class Market:
    spec: MarketSpec
    selections: tuple[str, ...]
    exhaustive: bool  # the selections cover every settled outcome (sum = 1 - push - void)
    team1_up: str | None = None  # a selection whose probability rises with team1's strength


def _markets(pred: MatchPrediction, players: list[PlayerInput]) -> Iterator[Market]:
    fmt = pred.format
    n = fmt.n_maps
    t1, t2 = "team1", "team2"
    winner_sel = (t1, t2, "draw") if fmt is MatchFormat.BO2 else (t1, t2)
    yield Market(MarketSpec(market_type=MarketType.MATCH_WINNER), winner_sel, True, t1)
    scores = tuple(pred.central.map_score_pmf)
    yield Market(MarketSpec(market_type=MarketType.CORRECT_MAP_SCORE), scores, True)
    if n >= 2:
        for line in (-1.5, 1.5) if n <= 3 else (-1.5, -2.5, 1.5):
            yield Market(MarketSpec(market_type=MarketType.MAP_HANDICAP, line=line), (t1, t2), True, t1)
        if fmt is not MatchFormat.BO2:  # a BO2 always has exactly 2 maps (nothing to price)
            spec = MarketSpec(market_type=MarketType.TOTAL_MAPS, line=n - 0.5)
            yield Market(spec, ("over", "under"), True)
        yield Market(MarketSpec(market_type=MarketType.BOTH_TEAMS_WIN_A_MAP), ("yes", "no"), True)
    if fmt is MatchFormat.BO2:
        yield Market(MarketSpec(market_type=MarketType.MAP_HANDICAP, line=0.0), (t1, t2), True, t1)
    for k in range(1, n + 1):
        slot_scores = tuple(pred.central.slots[k - 1].score_pmf)
        yield Market(MarketSpec(market_type=MarketType.MAP_WINNER, map_number=k), (t1, t2), True, t1)
        for line in (-3.5, -3.0, 2.5):
            spec = MarketSpec(market_type=MarketType.ROUND_HANDICAP, map_number=k, line=line)
            yield Market(spec, (t1, t2), True, t1)
        for line in (20.5, 22.0, 24.5):
            spec = MarketSpec(market_type=MarketType.TOTAL_ROUNDS, map_number=k, line=line)
            yield Market(spec, ("over", "under"), True)
        for team in (t1, t2):
            spec = MarketSpec(market_type=MarketType.TEAM_TOTAL_ROUNDS, map_number=k, team=team, line=10.5)
            yield Market(spec, ("over", "under"), True, "over" if team == t1 else "under")
        yield Market(
            MarketSpec(market_type=MarketType.TOTAL_ROUNDS_ODD_EVEN, map_number=k), ("odd", "even"), True
        )
        yield Market(MarketSpec(market_type=MarketType.MAP_CORRECT_SCORE, map_number=k), slot_scores, True)
        yield Market(MarketSpec(market_type=MarketType.MAP_OVERTIME, map_number=k), ("yes", "no"), True)
        for rnd in (1, 13):
            spec = MarketSpec(market_type=MarketType.PISTOL_ROUND_WINNER, map_number=k, round_number=rnd)
            yield Market(spec, (t1, t2), True, t1)
        for target in (1, 5, 10, 13, 14, 16):
            spec = MarketSpec(market_type=MarketType.RACE_TO_ROUNDS, map_number=k, line=float(target))
            # N > 13 needs overtime, which gets rarer as either team gets stronger: not monotone
            yield Market(spec, (t1, t2), True, t1 if target <= 13 else None)
        for half in (1, 2):
            spec = MarketSpec(market_type=MarketType.HALF_WINNER, map_number=k, half=half)
            yield Market(spec, (t1, t2, "draw"), True, t1)
    # player markets: every scope the format supports
    scopes: list[dict[str, object]] = [{}]
    scopes += [{"map_number": k} for k in range(1, n + 1)]
    scopes += [{"maps_scope": tuple(range(1, j + 1))} for j in range(2, n + 1)]
    a, b = players[0], players[5]  # one player of each team
    for scope in scopes:
        for line in (14.5, 30.0):
            spec = MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=a.player_id, line=line, **scope)
            yield Market(spec, ("over", "under"), True)
        spec = MarketSpec(
            market_type=MarketType.PLAYER_KILLS_H2H,
            player_id=a.player_id,
            opponent_player_id=b.player_id,
            **scope,
        )
        yield Market(spec, (player_selection(a.player_id), player_selection(b.player_id)), True)
        all_players = tuple(player_selection(p.player_id) for p in players)
        yield Market(MarketSpec(market_type=MarketType.TOP_FRAGGER, **scope), all_players, True)
        for slot in (TeamSlot.TEAM1, TeamSlot.TEAM2):
            team_sel = tuple(player_selection(p.player_id) for p in players if p.team is slot)
            spec = MarketSpec(market_type=MarketType.TOP_FRAGGER, team=slot.value, **scope)
            yield Market(spec, team_sel, True)


# ---------------------------------------------------------------------------------------------- tests


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_every_market_is_priceable_and_consistent(
    case: Case, predictions: dict[str, tuple[MatchPrediction, list[PlayerInput]]]
) -> None:
    pred, players = predictions[case.name]
    assert pred.low is not None and pred.high is not None
    seen_types: set[MarketType] = set()
    n_priced = 0
    for market in _markets(pred, players):
        seen_types.add(market.spec.market_type)
        outcomes = []
        for sel in market.selections:
            try:
                out = price_outcome(market.spec, sel, pred, sigma_floor=0.0, **RULES)  # type: ignore[arg-type]
            except UnpriceableMarket as exc:  # pragma: no cover - the failure message is the point
                pytest.fail(f"{case.name}: {market.spec.key()} / {sel} unpriceable: {exc}")
            n_priced += 1
            assert math.isfinite(out.sigma) and out.sigma > 0.0, (market.spec.key(), sel)
            assert 0.0 <= out.p_win <= 1.0 + 1e-9 and 0.0 <= out.p_lose <= 1.0 + 1e-9
            outcomes.append(out)
        if market.exhaustive:
            voids = {round(o.p_void, 9) for o in outcomes}
            assert len(voids) == 1, (market.spec.key(), voids)  # the same void mass for every selection
            total = sum(o.p_win for o in outcomes)
            pushes = [o.p_push for o in outcomes]
            assert max(pushes) - min(pushes) < 1e-9, market.spec.key()
            assert total == pytest.approx(1.0 - outcomes[0].p_push - outcomes[0].p_void, abs=1e-6), (
                market.spec.key()
            )
    expected = set(MarketType) - {MarketType.OTHER}
    if case.fmt is MatchFormat.BO1:  # series-shape markets do not exist for a single map
        expected -= {MarketType.MAP_HANDICAP, MarketType.TOTAL_MAPS, MarketType.BOTH_TEAMS_WIN_A_MAP}
    if case.fmt is MatchFormat.BO2:
        expected -= {MarketType.TOTAL_MAPS}
    assert seen_types == expected
    assert n_priced > 100


def _split(pred: MatchPrediction, spec: MarketSpec, sel: str, scenario: str) -> OutcomeSplit:
    core = {"central": pred.central, "low": pred.low, "high": pred.high}[scenario]
    assert core is not None
    return price_core(spec, sel, core, fmt=pred.format, **RULES)  # type: ignore[arg-type]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_low_high_bracket_central_for_team1_outcomes(
    case: Case, predictions: dict[str, tuple[MatchPrediction, list[PlayerInput]]]
) -> None:
    """Team1-favouring outcomes: P(low) <= P(central) <= P(high), up to simulation noise.

    Match-level and map-winner probabilities are exact functions of the per-map estimates (the
    simulations are reweighted to hit them), so they must be strictly ordered; slot-level derived
    outcomes (handicaps, races, pistols, halves, team totals) carry Monte Carlo noise from the
    central run, which uses other random numbers than low/high.
    """
    pred, players = predictions[case.name]
    checked = 0
    for market in _markets(pred, players):
        if market.team1_up is None:
            continue
        lo, mid, hi = (
            _split(pred, market.spec, market.team1_up, sc).p_win for sc in ("low", "central", "high")
        )
        exact = market.spec.market_type in (MarketType.MATCH_WINNER, MarketType.MAP_WINNER)
        if exact:
            assert lo < mid < hi, (market.spec.key(), lo, mid, hi)
        else:
            tol = 0.02
            assert lo <= mid + tol and mid <= hi + tol, (market.spec.key(), lo, mid, hi)
            assert lo <= hi + 1e-9, (market.spec.key(), lo, hi)  # low/high share random numbers
        checked += 1
    assert checked > 10


def test_regulation_only_totals_are_priceable(
    predictions: dict[str, tuple[MatchPrediction, list[PlayerInput]]],
) -> None:
    """``include_overtime_in_totals=False`` (Betclic setting) prices the round-count family too."""
    pred, _ = predictions["bo3-unknown"]
    for spec, sels in (
        (MarketSpec(market_type=MarketType.TOTAL_ROUNDS, map_number=3, line=23.5), ("over", "under")),
        (MarketSpec(market_type=MarketType.ROUND_HANDICAP, map_number=2, line=-1.5), ("team1", "team2")),
        (MarketSpec(market_type=MarketType.TOTAL_ROUNDS_ODD_EVEN, map_number=1), ("odd", "even")),
    ):
        outs = [
            price_outcome(spec, s, pred, sigma_floor=0.0, include_overtime_in_totals=False, h2h_tie="push")
            for s in sels
        ]
        assert sum(o.p_win for o in outs) == pytest.approx(1.0 - outs[0].p_void - outs[0].p_push, abs=1e-6)


def test_conditional_scopes_void_mass(
    predictions: dict[str, tuple[MatchPrediction, list[PlayerInput]]],
) -> None:
    """Map 3 of a BO3 (and its multi-map player scope) is void exactly when the series ends 2-0."""
    pred, players = predictions["bo3-unknown"]
    p11 = pred.central.map_score_pmf.get("2-1", 0.0) + pred.central.map_score_pmf.get("1-2", 0.0)
    pid = players[0].player_id
    for spec in (
        MarketSpec(market_type=MarketType.MAP_WINNER, map_number=3),
        MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=pid, maps_scope=(1, 2, 3), line=40.5),
        MarketSpec(market_type=MarketType.TOP_FRAGGER, maps_scope=(1, 2, 3)),
    ):
        sel = (
            player_selection(pid)
            if spec.market_type is MarketType.TOP_FRAGGER
            else ("over" if spec.market_type is MarketType.PLAYER_KILLS else "team1")
        )
        out = price_outcome(spec, sel, pred, sigma_floor=0.0, **RULES)  # type: ignore[arg-type]
        assert out.p_void == pytest.approx(1.0 - p11, abs=1e-9)
    # the match-scope kill market is never void
    out = price_outcome(
        MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=pid, line=40.5),
        "over",
        pred,
        sigma_floor=0.0,
        **RULES,  # type: ignore[arg-type]
    )
    assert out.p_void == 0.0


def test_unused_record_type_is_importable() -> None:
    """Guard: the kill model consumes the foundation's PlayerMapRecord scoreline fields."""
    assert {"team_rounds_won", "opponent_rounds_won"} <= set(PlayerMapRecord.model_fields)
