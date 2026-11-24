"""Exact pricing / settlement agreement for the player markets that the main consistency test can
only bound: head-to-head kills (pricing assumes independent players) and top fragger (settlement
leaves dead heats to the bookmaker).

* Head to head: the population is the full cross product of series results x kill vectors of
  player A x kill vectors of player B, so inside every scope whose maps are fixed by the scope itself
  (``map:k``, ``maps:1,2``, ...) the two kill totals are *exactly* independent and the priced
  probabilities must equal the settled frequencies - wins, pushes on integer handicaps, voids, and
  both tie rules. The unconditional ``match`` scope of a BO3 is not independent (both totals grow
  with the number of maps played), so the match scope is checked on a BO2, where it always has two
  maps.
* Top fragger: kills are ``50 r + i`` for the i-th player, so no two players can ever be level in
  any scope (``50 dR = m di`` has no solution with ``|m di| <= 45``) and settlement must declare a
  winner for every series; the priced probabilities must equal the settled frequencies exactly, for
  the team-restricted tables too.
"""

from __future__ import annotations

import itertools
from collections import Counter

import numpy as np
import pytest

from cs2bot.domain.enums import MatchFormat
from cs2bot.domain.markets import MarketSpec, MarketType, player_selection
from cs2bot.domain.predictions import MatchPrediction
from cs2bot.domain.results import MatchActuals, SettlementOutcome
from cs2bot.engine import pricing
from cs2bot.engine.settlement import resolve_outcome
from tests.engine.engine_series import (
    PLAYER_TEAMS,
    TEAM1_IDS,
    TEAM2_IDS,
    empirical_prediction,
    simulate_series,
)

MT = MarketType
WON, LOST, VOID, PUSH, UNKNOWN = (
    SettlementOutcome.WON,
    SettlementOutcome.LOST,
    SettlementOutcome.VOID,
    SettlementOutcome.PUSH,
    SettlementOutcome.UNKNOWN,
)
PLAYERS = (*TEAM1_IDS, *TEAM2_IDS)
A, B = 102, 204  # head-to-head pair (one player of each team)
ROUND_PROBS = {MatchFormat.BO2: [0.52, 0.47], MatchFormat.BO3: [0.55, 0.45, 0.5]}


def _with_kills(series: MatchActuals, kills_by_map: list[dict[int, int]]) -> MatchActuals:
    maps = tuple(m.model_copy(update={"player_kills": kills_by_map[m.map_number - 1]}) for m in series.maps)
    return series.model_copy(update={"maps": maps})


def _h2h_population(fmt: MatchFormat) -> list[MatchActuals]:
    rng = np.random.default_rng(31 + fmt.n_maps)
    results = [simulate_series(rng, fmt, ROUND_PROBS[fmt]) for _ in range(16)]
    vectors_a = [tuple(int(k) for k in rng.integers(10, 22, fmt.n_maps)) for _ in range(9)]
    vectors_b = [tuple(int(k) for k in rng.integers(10, 22, fmt.n_maps)) for _ in range(9)]
    population = []
    for series, ka, kb in itertools.product(results, vectors_a, vectors_b):
        kills = [{**dict.fromkeys(PLAYERS, 15), A: ka[k], B: kb[k]} for k in range(fmt.n_maps)]
        population.append(_with_kills(series, kills))
    return population


def _settled(
    market: MarketSpec, selection: str, population: list[MatchActuals], **rules: object
) -> Counter[str]:
    rules = {"include_overtime_in_totals": True, "h2h_tie": "push", **rules}
    return Counter(
        resolve_outcome(market, selection, s, player_teams=PLAYER_TEAMS, **rules).value for s in population
    )


def _assert_exact(
    market: MarketSpec,
    selection: str,
    population: list[MatchActuals],
    prediction: MatchPrediction,
    **rules: object,
) -> float:
    rules = {"include_overtime_in_totals": True, "h2h_tie": "push", **rules}
    priced = pricing.price_outcome(market, selection, prediction, sigma_floor=0.0, **rules)
    freq = _settled(market, selection, population, **rules)
    n = len(population)
    label = f"{population[0].format.value} {market.key()}#{selection} {rules}"
    assert freq[UNKNOWN.value] == 0, label
    assert freq[WON.value] / n == pytest.approx(priced.p_win, abs=1e-9), label
    assert freq[PUSH.value] / n == pytest.approx(priced.p_push, abs=1e-9), label
    assert freq[VOID.value] / n == pytest.approx(priced.p_void, abs=1e-9), label
    assert freq[LOST.value] / n == pytest.approx(priced.p_lose, abs=1e-9), label
    return priced.p_win


@pytest.fixture(scope="module", params=[MatchFormat.BO2, MatchFormat.BO3], ids=lambda f: f.value)
def h2h_population(request: pytest.FixtureRequest) -> tuple[list[MatchActuals], MatchPrediction]:
    population = _h2h_population(request.param)
    return population, empirical_prediction(population, request.param)


@pytest.mark.parametrize("tie_rule", ["push", "lose"])
def test_head_to_head_kills_settle_exactly_as_priced(
    h2h_population: tuple[list[MatchActuals], MatchPrediction], tie_rule: str
) -> None:
    population, prediction = h2h_population
    fmt = prediction.format
    scopes: list[dict[str, object]] = [{"map_number": k} for k in range(1, fmt.n_maps + 1)]
    scopes += [{"maps_scope": tuple(range(1, last + 1))} for last in range(2, fmt.n_maps + 1)]
    if fmt.maps_to_win is None:  # BO2: the match always has both maps, so its totals are independent
        scopes.append({})
    pushes = 0
    for scope, line in itertools.product(scopes, (None, -2.5, -1.0, 0.0, 3.0)):
        market = MarketSpec(
            market_type=MT.PLAYER_KILLS_H2H, player_id=A, opponent_player_id=B, line=line, **scope
        )
        p_a, p_b = (
            _assert_exact(market, player_selection(pid), population, prediction, h2h_tie=tie_rule)
            for pid in (A, B)
        )
        freq = _settled(market, player_selection(A), population, h2h_tie="push")
        tie, void = freq[PUSH.value] / len(population), freq[VOID.value] / len(population)
        assert p_a + p_b == pytest.approx(1.0 - tie - void, abs=1e-9)  # ties lose for both under "lose"
        pushes += tie > 0
    assert pushes > 0  # integer handicaps (and no handicap) really produced level results


def _tie_free_population(fmt: MatchFormat, n: int) -> list[MatchActuals]:
    rng = np.random.default_rng(97 + fmt.n_maps)
    population = []
    for _ in range(n):
        series = simulate_series(rng, fmt, ROUND_PROBS.get(fmt, [0.5] * fmt.n_maps))
        kills = [
            {pid: 50 * int(rng.integers(0, 3)) + i for i, pid in enumerate(PLAYERS)}
            for _ in range(fmt.n_maps)
        ]
        population.append(_with_kills(series, kills))
    return population


@pytest.mark.parametrize("fmt", [MatchFormat.BO1, MatchFormat.BO3], ids=lambda f: f.value)
def test_top_fragger_settles_exactly_as_priced_without_dead_heats(fmt: MatchFormat) -> None:
    population = _tie_free_population(fmt, 300)
    prediction = empirical_prediction(population, fmt)
    scopes: list[dict[str, object]] = [{}]
    scopes += [{"map_number": k} for k in range(1, fmt.n_maps + 1)]
    scopes += [{"maps_scope": tuple(range(1, last + 1))} for last in range(2, fmt.n_maps + 1)]
    for scope, team in itertools.product(scopes, (None, "team1", "team2")):
        market = MarketSpec(market_type=MT.TOP_FRAGGER, team=team, **scope)
        candidates = TEAM1_IDS if team == "team1" else TEAM2_IDS if team == "team2" else PLAYERS
        total = 0.0
        for pid in candidates:
            total += _assert_exact(market, player_selection(pid), population, prediction)
        void = _settled(market, player_selection(candidates[0]), population)[VOID.value] / len(population)
        assert total == pytest.approx(1.0 - void, abs=1e-9), market.key()
