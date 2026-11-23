"""Series combinatorics checked against brute-force enumeration."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from cs2bot.domain.enums import MatchFormat
from cs2bot.models.sim_series import played_mask, series_outcome, series_prefix_sum_pmf, series_sum_pmf


def _brute(fmt: MatchFormat, p: list[float]) -> tuple[list[float], dict[tuple[int, int], float]]:
    n = fmt.n_maps
    need = fmt.maps_to_win
    played = [0.0] * n
    final: dict[tuple[int, int], float] = {}
    for outcome in itertools.product((True, False), repeat=n):  # outcome of each slot *if* played
        prob = float(np.prod([p[k] if outcome[k] else 1 - p[k] for k in range(n)]))
        a = b = 0
        for k in range(n):
            if need is not None and max(a, b) >= need:
                break
            played[k] += prob
            a += outcome[k]
            b += not outcome[k]
        final[(a, b)] = final.get((a, b), 0.0) + prob
    return played, final


@pytest.mark.parametrize("fmt", list(MatchFormat))
def test_series_outcome_matches_brute_force(fmt: MatchFormat) -> None:
    rng = np.random.default_rng(int(fmt.n_maps))
    for _ in range(5):
        p = list(rng.uniform(0.05, 0.95, fmt.n_maps))
        res = series_outcome(fmt, p)
        played, final = _brute(fmt, p)
        assert res.p_played == pytest.approx(tuple(played))
        assert set(res.final) == set(final)
        for k, v in final.items():
            assert res.final[k] == pytest.approx(v)
        assert sum(res.final.values()) == pytest.approx(1.0)
        assert res.p_played[0] == pytest.approx(1.0)


def test_specific_formats() -> None:
    bo3 = series_outcome(MatchFormat.BO3, [0.6, 0.7, 0.4])
    p11 = 0.6 * 0.3 + 0.4 * 0.7
    assert bo3.p_played[2] == pytest.approx(p11)
    assert bo3.score_pmf()["2-0"] == pytest.approx(0.42)
    bo2 = series_outcome(MatchFormat.BO2, [0.6, 0.7])
    assert bo2.p_draw == pytest.approx(0.6 * 0.3 + 0.4 * 0.7)
    assert bo2.p_team1_win == pytest.approx(0.42)
    assert bo2.p_played == (1.0, 1.0)
    bo1 = series_outcome(MatchFormat.BO1, [0.55, 0.9])  # extra maps are ignored
    assert bo1.p_team1_win == pytest.approx(0.55)
    with pytest.raises(ValueError):
        series_outcome(MatchFormat.BO5, [0.5, 0.5])


@pytest.mark.parametrize("fmt", [MatchFormat.BO1, MatchFormat.BO2, MatchFormat.BO3, MatchFormat.BO5])
def test_series_sum_pmf_matches_enumeration(fmt: MatchFormat) -> None:
    rng = np.random.default_rng(7)
    n = fmt.n_maps
    p = list(rng.uniform(0.2, 0.8, n))
    pw = [rng.dirichlet(np.ones(4)) for _ in range(n)]  # pmf of X_k if team1 wins map k
    pl = [rng.dirichlet(np.ones(3)) for _ in range(n)]
    got = series_sum_pmf(fmt, p, pw, pl)
    need = fmt.maps_to_win
    expected = np.zeros(4 * n + 1)
    for outcome in itertools.product((True, False), repeat=n):
        prob = float(np.prod([p[k] if outcome[k] else 1 - p[k] for k in range(n)]))
        acc = np.array([1.0])
        a = b = 0
        for k in range(n):
            if need is not None and max(a, b) >= need:
                break
            acc = np.convolve(acc, pw[k] if outcome[k] else pl[k])
            a += outcome[k]
            b += not outcome[k]
        expected[: acc.size] += prob * acc
    np.testing.assert_allclose(got, expected[: got.size], atol=1e-12)
    assert got.sum() == pytest.approx(1.0)


def test_played_mask_matches_probabilities() -> None:
    rng = np.random.default_rng(3)
    p = [0.65, 0.4, 0.55, 0.5, 0.3]
    won = rng.random((5, 200_000)) < np.asarray(p)[:, None]
    mask = played_mask(MatchFormat.BO5, won)
    res = series_outcome(MatchFormat.BO5, p)
    np.testing.assert_allclose(mask.mean(axis=1), res.p_played, atol=0.005)
    assert played_mask(MatchFormat.BO2, won[:2]).all()


@pytest.mark.parametrize("fmt", list(MatchFormat))
def test_series_prefix_sum_pmf_matches_enumeration(fmt: MatchFormat) -> None:
    """Sum of X_1..X_j conditional on map j being played, against brute-force enumeration."""
    rng = np.random.default_rng(11)
    n = fmt.n_maps
    p = list(rng.uniform(0.2, 0.8, n))
    pw = [rng.dirichlet(np.ones(4)) for _ in range(n)]
    pl = [rng.dirichlet(np.ones(3)) for _ in range(n)]
    need = fmt.maps_to_win
    p_played = series_outcome(fmt, p).p_played
    for upto in range(1, n + 1):
        mass, got = series_prefix_sum_pmf(fmt, p, pw, pl, upto)
        expected = np.zeros(4 * n + 1)
        total = 0.0
        for outcome in itertools.product((True, False), repeat=n):
            prob = float(np.prod([p[k] if outcome[k] else 1 - p[k] for k in range(n)]))
            a = b = 0
            acc = np.array([1.0])
            played = True
            for k in range(upto):
                if need is not None and max(a, b) >= need:
                    played = False
                    break
                acc = np.convolve(acc, pw[k] if outcome[k] else pl[k])
                a += outcome[k]
                b += not outcome[k]
            if played:
                expected[: acc.size] += prob * acc
                total += prob
        assert mass == pytest.approx(total) and mass == pytest.approx(p_played[upto - 1])
        np.testing.assert_allclose(got, (expected / total)[: got.size], atol=1e-12)
        assert got.sum() == pytest.approx(1.0)
    with pytest.raises(ValueError):
        series_prefix_sum_pmf(fmt, p, pw, pl, n + 1)


def test_series_prefix_sum_pmf_unplayable_map() -> None:
    """BO3 map 3 cannot be played when one team surely wins maps 1 and 2."""
    one = [np.array([0.0, 1.0])] * 3
    mass, pmf = series_prefix_sum_pmf(MatchFormat.BO3, [1.0, 1.0, 0.5], one, one, 3)
    assert mass == 0.0 and pmf.tolist() == [1.0]
