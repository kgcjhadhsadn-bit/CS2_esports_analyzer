"""Pure Glicko-2 mathematics (cs2bot.models.ratings_glicko) against the paper and brute force."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import integrate, optimize

from cs2bot.models.ratings_glicko import (
    GLICKO2_SCALE,
    expected_score,
    from_display,
    g,
    glicko2_rate,
    inflate_phi,
    logistic_normal_moments,
    logit,
    mov_score,
    mr12_map_win_probability,
    sigmoid,
    to_display,
    to_display_rd,
    volatility_update,
)

# Glickman, "Example of the Glicko-2 system": player 1500 / RD 200 / sigma 0.06, tau = 0.5, against
# (1400, 30) win, (1550, 100) loss, (1700, 300) loss.
PAPER_OPPONENTS = [(1400.0, 30.0, 1.0), (1550.0, 100.0, 0.0), (1700.0, 300.0, 0.0)]


def _paper_results() -> list[tuple[float, float, float]]:
    return [(*from_display(r, rd), s) for r, rd, s in PAPER_OPPONENTS]


def test_scale_conversion_round_trip() -> None:
    mu, phi = from_display(1500.0, 200.0)
    assert mu == pytest.approx(0.0)
    assert phi == pytest.approx(1.1513, abs=1e-4)  # value printed in the paper
    assert to_display(mu) == pytest.approx(1500.0)
    assert to_display_rd(phi) == pytest.approx(200.0)
    assert 400.0 / math.log(10.0) == pytest.approx(GLICKO2_SCALE, abs=1e-4)


def test_g_and_expected_score_match_paper_table() -> None:
    mu, _ = from_display(1500.0, 200.0)
    expected_g = [0.9955, 0.9531, 0.7242]
    expected_e = [0.639, 0.432, 0.303]
    for (mu_j, phi_j, _), g_ref, e_ref in zip(_paper_results(), expected_g, expected_e, strict=True):
        assert g(phi_j) == pytest.approx(g_ref, abs=5e-5)
        assert expected_score(mu, mu_j, phi_j) == pytest.approx(e_ref, abs=5e-4)


def test_glicko2_rate_reproduces_paper_example() -> None:
    mu, phi = from_display(1500.0, 200.0)
    new_mu, new_phi, new_sigma = glicko2_rate(mu, phi, 0.06, _paper_results(), tau=0.5)
    assert to_display(new_mu) == pytest.approx(1464.06, abs=0.01)
    assert to_display_rd(new_phi) == pytest.approx(151.52, abs=0.01)
    assert new_sigma == pytest.approx(0.05999, abs=1e-5)


def test_glicko2_rate_without_games_only_inflates_deviation() -> None:
    mu, phi, sigma = glicko2_rate(0.3, 0.8, 0.06, [], tau=0.5)
    assert mu == 0.3
    assert sigma == 0.06
    assert phi == pytest.approx(math.sqrt(0.8**2 + 0.06**2))


@pytest.mark.parametrize(
    ("phi", "v", "delta", "sigma", "tau", "periods"),
    [
        (1.1513, 1.7785, -0.4834, 0.06, 0.5, 1.0),  # the paper's numbers
        (0.4, 0.9, 2.5, 0.06, 0.5, 1.0),  # surprising results: Delta^2 > phi^2 + v
        (0.3, 3.5, 0.1, 0.12, 1.2, 1.0),
        (0.8, 2.0, 3.0, 0.06, 0.5, 0.25),  # fractional rating period
        (0.8, 2.0, 0.2, 0.06, 0.3, 3.5),
    ],
)
def test_volatility_update_maximises_the_posterior(
    phi: float, v: float, delta: float, sigma: float, tau: float, periods: float
) -> None:
    """The Illinois root equals the brute-force maximiser of the (generalised) log posterior."""
    a = math.log(sigma * sigma)

    def neg_log_posterior(x: float) -> float:
        total = phi * phi + v + periods * math.exp(x)
        return 0.5 * math.log(total) + delta * delta / (2.0 * total) + (x - a) ** 2 / (2.0 * tau * tau)

    res = optimize.minimize_scalar(
        neg_log_posterior, bounds=(a - 30.0, a + 30.0), method="bounded", options={"xatol": 1e-10}
    )
    got = volatility_update(phi2=phi * phi, v=v, delta=delta, sigma=sigma, tau=tau, periods=periods, eps=1e-9)
    assert math.log(got * got) == pytest.approx(res.x, abs=1e-4)
    if delta * delta > phi * phi + v:
        assert got > sigma  # results far outside expectation raise the volatility
    else:
        assert got <= sigma + 1e-12


def test_volatility_update_edge_cases() -> None:
    assert volatility_update(phi2=1.0, v=1.0, delta=5.0, sigma=0.06, tau=0.5, periods=0.0) == 0.06
    with pytest.raises(ValueError):
        volatility_update(phi2=1.0, v=1.0, delta=0.0, sigma=0.0, tau=0.5)


def test_inflate_phi_follows_formula_and_cap() -> None:
    assert inflate_phi(0.5, 0.06, 4.0, 2.0) == pytest.approx(math.sqrt(0.25 + 4 * 0.0036))
    assert inflate_phi(0.5, 0.06, 0.0, 2.0) == 0.5
    assert inflate_phi(1.99, 0.3, 50.0, 2.0) == 2.0  # capped at the initial deviation
    values = [inflate_phi(0.5, 0.06, c, 2.0) for c in (0.0, 1.0, 10.0, 100.0, 1e6)]
    assert values == sorted(values)
    assert values[-1] == 2.0


def test_mr12_map_win_probability_properties() -> None:
    assert mr12_map_win_probability(0.5) == pytest.approx(0.5)
    assert mr12_map_win_probability(0.0) == 0.0
    assert mr12_map_win_probability(1.0) == pytest.approx(1.0)
    grid = np.linspace(0.0, 1.0, 101)
    values = [mr12_map_win_probability(float(q)) for q in grid]
    assert values == sorted(values)
    for q in (0.3, 0.42, 0.55, 0.8):
        assert mr12_map_win_probability(q) + mr12_map_win_probability(1 - q) == pytest.approx(1.0)
    # A small per-round edge is amplified over a map.
    assert mr12_map_win_probability(0.55) > 0.65


def test_mr12_map_win_probability_matches_simulation() -> None:
    rng = np.random.default_rng(0)
    q = 0.54
    n = 20_000
    wins = 0
    for _ in range(n):
        a = b = 0
        target = 13
        while True:
            if rng.random() < q:
                a += 1
            else:
                b += 1
            if a == 12 and b == 12 and target == 13:
                target = 16
            elif target > 13 and a == target - 1 and b == target - 1:
                target += 3
            if a == target or b == target:
                break
        wins += a > b
    assert mr12_map_win_probability(q) == pytest.approx(wins / n, abs=0.012)


@pytest.mark.parametrize("weight", [0.0, 0.1, 0.5, 1.0])
def test_mov_score_win_is_always_above_half(weight: float) -> None:
    scores = [(13, k) for k in range(12)] + [(16, 14), (16, 13), (19, 17), (22, 20), (31, 29)]
    previous = None
    for won, lost in scores[:12]:
        s = mov_score(won, lost, weight)
        assert 0.5 < s <= 1.0
        assert mov_score(lost, won, weight) == pytest.approx(1.0 - s)
        if previous is not None:
            assert s <= previous + 1e-12  # closer maps never earn more than clearer ones
        previous = s
    for won, lost in scores[12:]:
        s = mov_score(won, lost, weight)
        assert 0.5 < s <= 1.0
        assert mov_score(lost, won, weight) < 0.5


def test_mov_score_special_cases() -> None:
    assert mov_score(13, 5, 0.0) == 1.0
    assert mov_score(5, 13, 0.0) == 0.0
    assert mov_score(0, 0, 0.3) == 0.5
    assert mov_score(12, 12, 0.3) == 0.5
    assert mov_score(13, 0, 1.0) == pytest.approx(1.0)
    # mild weight: a 13-11 win keeps most of the full point
    assert 0.9 < mov_score(13, 11, 0.1) < 1.0


@pytest.mark.parametrize(("mean", "var"), [(0.0, 0.0), (0.7, 0.5), (-1.5, 2.0), (0.2, 8.0), (3.0, 0.1)])
def test_logistic_normal_moments_match_numerical_integration(mean: float, var: float) -> None:
    p, sd = logistic_normal_moments(mean, var)
    if var == 0.0:
        assert p == pytest.approx(sigmoid(mean))
        assert sd == 0.0
        return
    s = math.sqrt(var)

    def density(x: float) -> float:
        return math.exp(-0.5 * ((x - mean) / s) ** 2) / (s * math.sqrt(2 * math.pi))

    m1, _ = integrate.quad(lambda x: sigmoid(x) * density(x), mean - 12 * s, mean + 12 * s, limit=200)
    m2, _ = integrate.quad(lambda x: sigmoid(x) ** 2 * density(x), mean - 12 * s, mean + 12 * s, limit=200)
    assert p == pytest.approx(m1, abs=2e-4)
    assert sd == pytest.approx(math.sqrt(m2 - m1 * m1), abs=2e-4)


def test_logistic_normal_uncertainty_pulls_probability_toward_half() -> None:
    p_certain, _ = logistic_normal_moments(1.5, 0.0)
    p_uncertain, sd = logistic_normal_moments(1.5, 4.0)
    assert 0.5 < p_uncertain < p_certain
    assert sd > 0.2
    p_sym, _ = logistic_normal_moments(0.0, 9.0)
    assert p_sym == pytest.approx(0.5, abs=1e-9)


def test_logit_and_sigmoid_are_inverse_and_finite() -> None:
    for p in (0.01, 0.3, 0.5, 0.77, 0.999):
        assert sigmoid(logit(p)) == pytest.approx(p)
    assert math.isfinite(logit(0.0))
    assert math.isfinite(logit(1.0))
    assert sigmoid(-800.0) == 0.0
    assert sigmoid(800.0) == 1.0
