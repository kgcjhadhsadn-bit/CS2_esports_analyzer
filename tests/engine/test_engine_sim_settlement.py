"""End to end: the real map simulator's own games, settled one by one, must reproduce the prices.

``cs2bot.models.map_sim`` plays every simulated map round by round and summarises the plays into the
distributions that ``MapSlotPrediction`` carries (score pmf, pistols, halves, race to N, overtime).
Here the individual plays are turned back into finished maps (``MatchActuals`` with the full round
history) and settled with :func:`cs2bot.engine.settlement.resolve_outcome`. Pricing the slot that
the simulator published must give *exactly* the settled frequencies (with the simulator's importance
weights after ``reweighted``, which is how the match engine calibrates every map), for every map-slot
market type, both overtime rules and every race target. This checks the producer's definitions
(half 2 = regulation rounds 13-24, pistol round 13, "first to N", "neither reaches N") against the
settlement rules, not against a second hand-written simulator.

The simulator logs the 24 regulation rounds only. The overtime rounds of a map are rebuilt from its
final score and the simulator's own race records: of all orders that fit the final score, the first
one that reproduces every recorded "first to N" result is used (one always exists for a consistent
play; the test fails if none does).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import combinations

import numpy as np
import pytest

from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.predictions import MapSlotPrediction, MatchPrediction, PredictionCore
from cs2bot.domain.results import MapActual, MatchActuals, SettlementOutcome
from cs2bot.engine import pricing
from cs2bot.engine.settlement import resolve_outcome
from cs2bot.models.map_sim import MapSimInput, MapSimOutput, MapSimulator

MT = MarketType
T1, T2 = TeamSlot.TEAM1, TeamSlot.TEAM2
WON, LOST, VOID, PUSH, UNKNOWN = (
    SettlementOutcome.WON,
    SettlementOutcome.LOST,
    SettlementOutcome.VOID,
    SettlementOutcome.PUSH,
    SettlementOutcome.UNKNOWN,
)
N_SIMS = 2000
#: only the first two overtime periods can decide a race target <= 16; later periods alternate
_SEARCHED_OT_PERIODS = 2


def _first_to(history: tuple[TeamSlot, ...], target: int) -> int:
    """1 / 2 = team1 / team2 reached ``target`` first, 0 = nobody did (the simulator's coding)."""
    a = b = 0
    for winner in history:
        a, b = (a + 1, b) if winner is T1 else (a, b + 1)
        if a == target:
            return 1
        if b == target:
            return 2
    return 0


def _tied_periods() -> list[tuple[TeamSlot, ...]]:
    """Every order of a 3-3 overtime period (nobody reaches 4)."""
    return [tuple(T1 if i in idx else T2 for i in range(6)) for idx in combinations(range(6), 3)]


def _final_periods(winner: TeamSlot, loser_rounds: int) -> list[tuple[TeamSlot, ...]]:
    """Every order of a deciding period won 4-k: the winner's fourth round comes last."""
    loser = T2 if winner is T1 else T1
    n = 3 + loser_rounds
    return [
        (*(loser if i in idx else winner for i in range(n)), winner)
        for idx in combinations(range(n), loser_rounds)
    ]


def _overtime_tails(a: int, b: int) -> Iterator[tuple[TeamSlot, ...]]:
    """Every overtime round order that ends the map at ``a``-``b`` from 12-12 (the first two
    periods enumerated, later tied periods alternating)."""
    periods = (max(a, b) - 16) // 3 + 1
    winner = T1 if a > b else T2
    loser_rounds = min(a, b) - 12 - 3 * (periods - 1)
    options: list[list[tuple[TeamSlot, ...]]] = []
    for k in range(periods):
        if k == periods - 1:
            options.append(_final_periods(winner, loser_rounds))
        elif k < _SEARCHED_OT_PERIODS:
            options.append(_tied_periods())
        else:
            options.append([(T1, T2) * 3])

    def walk(k: int, prefix: tuple[TeamSlot, ...]) -> Iterator[tuple[TeamSlot, ...]]:
        if k == len(options):
            yield prefix
            return
        for period in options[k]:
            yield from walk(k + 1, prefix + period)

    yield from walk(0, ())


def _maps(out: MapSimOutput) -> list[MapActual]:
    """Every simulated play of ``out`` as a finished map with its full round history."""
    log = out.round_log
    assert log is not None
    rec = out.records
    maps: list[MapActual] = []
    for i in range(out.n_sims):
        a, b = int(rec.team1_rounds[i]), int(rec.team2_rounds[i])
        played = log["active"][:, i]
        regulation = tuple(T1 if won else T2 for won in log["team1_won"][played, i])
        history = regulation
        if a + b > pricing.REGULATION_ROUNDS:
            assert regulation.count(T1) == regulation.count(T2) == 12
            wanted = {t: int(rec.race[j, i]) for j, t in enumerate(out.race_targets)}
            history = next(
                (
                    regulation + tail
                    for tail in _overtime_tails(a, b)
                    if all(_first_to(regulation + tail, t) == w for t, w in wanted.items())
                ),
                (),
            )
            assert history, f"play {i} ({a}-{b}): no round order matches the simulator's race records"
        assert history.count(T1) == a and len(history) == a + b
        maps.append(
            MapActual(
                map_number=1, map_name=out.map_name, team1_rounds=a, team2_rounds=b, round_winners=history
            )
        )
    return maps


def _prediction(out: MapSimOutput) -> MatchPrediction:
    """A BO1 whose only slot is exactly what the simulator published."""
    slot = MapSlotPrediction(
        map_number=1,
        p_played=1.0,
        map_distribution={out.map_name: 1.0},
        p_team1_win=out.p_team1_win,
        score_pmf=out.score_pmf,
        p_overtime=out.p_overtime,
        p_team1_pistol=out.p_team1_pistol,
        p_team1_half=out.p_team1_half,
        p_half_draw=out.p_half_draw,
        p_team1_race_to=out.p_team1_race_to,
        p_neither_race_to=out.p_neither_race_to,
    )
    core = PredictionCore(
        p_team1_win=out.p_team1_win,
        map_score_pmf={"1-0": out.p_team1_win, "0-1": 1.0 - out.p_team1_win},
        slots=[slot],
    )
    return MatchPrediction(
        match_id=1,
        created_at=datetime(2026, 11, 20, 18, 0, tzinfo=UTC),
        model_version="map-sim",
        format=MatchFormat.BO1,
        team1_id=1,
        team2_id=2,
        central=core,
        sigma_team1_win=0.03,
    )


def _markets(out: MapSimOutput) -> list[tuple[MarketSpec, tuple[str, ...]]]:
    teams, over_under = ("team1", "team2"), ("over", "under")

    def spec(market_type: MarketType, **kw: object) -> MarketSpec:
        return MarketSpec(market_type=market_type, map_number=1, **kw)

    scores = (*out.score_pmf, "13-0", "0-13", "19-17", "25-23")
    markets: list[tuple[MarketSpec, tuple[str, ...]]] = [
        (MarketSpec(market_type=MT.MATCH_WINNER), teams),
        (MarketSpec(market_type=MT.CORRECT_MAP_SCORE), ("1-0", "0-1")),
        (spec(MT.MAP_WINNER), teams),
        (spec(MT.TOTAL_ROUNDS_ODD_EVEN), ("odd", "even")),
        (spec(MT.MAP_CORRECT_SCORE), tuple(dict.fromkeys(scores))),
        (spec(MT.MAP_OVERTIME), ("yes", "no")),
    ]
    markets += [(spec(MT.ROUND_HANDICAP, line=x), teams) for x in (-5.5, -3.0, -2.5, 0.0, 2.0, 4.5)]
    markets += [(spec(MT.TOTAL_ROUNDS, line=x), over_under) for x in (18.5, 20.0, 21.5, 24.0, 24.5, 26.5, 30)]
    markets += [
        (spec(MT.TEAM_TOTAL_ROUNDS, team=team, line=x), over_under)
        for team in teams
        for x in (8.5, 10.0, 12.0, 13.0, 14.5, 16.0)
    ]
    markets += [(spec(MT.PISTOL_ROUND_WINNER, round_number=r), teams) for r in (1, 13)]
    markets += [(spec(MT.HALF_WINNER, half=h), (*teams, "draw")) for h in (1, 2)]
    markets += [(spec(MT.RACE_TO_ROUNDS, line=float(t)), teams) for t in out.race_targets]
    return markets


@pytest.fixture(scope="module")
def simulated() -> tuple[MapSimOutput, list[MapActual]]:
    out = MapSimulator().simulate(
        MapSimInput("mirage", strength_diff=0.15), N_SIMS, rng=7, record_rounds=True
    )
    return out, _maps(out)


@pytest.mark.parametrize("include_overtime", [True, False], ids=["with overtime", "regulation only"])
def test_simulated_plays_settle_exactly_as_priced(
    simulated: tuple[MapSimOutput, list[MapActual]], include_overtime: bool
) -> None:
    """Both as simulated (equal weights) and after ``reweighted`` (the match engine's calibration)."""
    raw, maps = simulated
    rules: dict[str, object] = {"include_overtime_in_totals": include_overtime, "h2h_tie": "push"}
    actuals = [
        MatchActuals(match_id=1, format=MatchFormat.BO1, team1_id=1, team2_id=2, maps=(m,)) for m in maps
    ]
    variants = []
    for out in (raw, raw.reweighted(0.64)):
        weights = np.full(out.n_sims, 1.0 / out.n_sims) if out.weights is None else out.weights
        assert weights.sum() == pytest.approx(1.0)
        variants.append((out, weights, _prediction(out)))
    assert variants[1][0].p_team1_win == pytest.approx(0.64)
    checked = 0
    for market, selections in _markets(raw):
        totals = [0.0, 0.0]
        for selection in selections:
            settled = [resolve_outcome(market, selection, a, **rules) for a in actuals]
            for v, (_, weights, prediction) in enumerate(variants):
                priced = pricing.price_outcome(market, selection, prediction, sigma_floor=0.0, **rules)
                mass = dict.fromkeys(SettlementOutcome, 0.0)
                for w, outcome in zip(weights, settled, strict=True):
                    mass[outcome] += float(w)
                label = f"{market.key()}#{selection} (overtime {include_overtime}, variant {v})"
                assert mass[UNKNOWN] == 0.0, label
                assert mass[WON] == pytest.approx(priced.p_win, abs=1e-9), label
                assert mass[PUSH] == pytest.approx(priced.p_push, abs=1e-9), label
                assert mass[VOID] == pytest.approx(priced.p_void, abs=1e-9), label
                assert mass[LOST] == pytest.approx(priced.p_lose, abs=1e-9), label
                totals[v] += priced.p_win
                checked += 1
        if market.market_type is not MT.MAP_CORRECT_SCORE:  # the score list is not exhaustive
            refund = priced.p_push + priced.p_void  # the same for every selection and both variants here
            assert totals[1] == pytest.approx(1.0 - refund, abs=1e-9), market.key()
    assert checked > 200


def test_the_simulated_population_exercises_every_rule(
    simulated: tuple[MapSimOutput, list[MapActual]],
) -> None:
    """Guard: overtimes (incl. a second one), level second halves, voids for N > 13 and races that
    needed the overtime order all occur, so the exact agreement above is not vacuous."""
    out, maps = simulated
    assert sum(1 for m in maps if m.went_to_overtime) > 100
    assert any(max(m.team1_rounds, m.team2_rounds) >= 19 for m in maps)
    assert 0.05 < out.p_half_draw["2"] < 0.5
    assert 0.5 < out.p_neither_race_to["14"] < 1.0
    assert any(min(m.team1_rounds, m.team2_rounds) >= 13 for m in maps)  # race to 13 needed the history
