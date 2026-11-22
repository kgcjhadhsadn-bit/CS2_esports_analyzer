"""Pure Glicko-2 mathematics used by :mod:`cs2bot.models.ratings`.

Reference: Mark E. Glickman, *Example of the Glicko-2 system* (Boston University, revision of
March 22, 2022). Notation follows the paper, on the Glicko-2 scale:

* ``mu = (r - 1500) / 173.7178`` and ``phi = RD / 173.7178``; ``sigma`` is the volatility;
* ``g(phi) = 1 / sqrt(1 + 3 phi^2 / pi^2)`` discounts an opponent whose rating is uncertain;
* ``E(mu, mu_j, phi_j) = 1 / (1 + exp(-g(phi_j) (mu - mu_j)))`` is the expected score;
* ``v = [sum_j g(phi_j)^2 E_j (1 - E_j)]^-1`` is the variance of the rating estimated from the game
  outcomes alone and ``Delta = v sum_j g(phi_j) (s_j - E_j)`` the estimated improvement;
* the new volatility ``sigma'`` maximises
  ``L(x) = -1/2 log(phi^2 + v + e^x) - Delta^2 / (2 (phi^2 + v + e^x)) - (x - a)^2 / (2 tau^2)``
  over ``x = log(sigma'^2)`` (``a = log(sigma^2)``); its derivative is the paper's ``f(x)`` and the
  root is bracketed and found with the Illinois (regula falsi) algorithm;
* ``phi* = sqrt(phi^2 + sigma'^2)``, ``phi' = 1 / sqrt(1/phi*^2 + 1/v)`` and
  ``mu' = mu + phi'^2 sum_j g(phi_j) (s_j - E_j)``.

:func:`glicko2_rate` is a faithful implementation of the paper (one rating period, several games
evaluated against the opponents' pre-period ratings) and is verified against the paper's worked
example in the tests.

Generalisation used by the online model: a rating period of *fractional* length ``c`` (the number
of rating periods elapsed since the team's previous game). The random-walk variance accumulated
over the gap is ``c * sigma^2``, so the likelihood above becomes
``L(x) = -1/2 log(phi^2 + v + c e^x) - Delta^2 / (2 (phi^2 + v + c e^x)) - (x - a)^2 / (2 tau^2)``
whose derivative is ``f_c(x) = c e^x (Delta^2 - phi^2 - v - c e^x) / (2 (phi^2 + v + c e^x)^2) -
(x - a) / tau^2``. For ``c = 1`` this is exactly the paper's ``f``; for ``c = 0`` (two games at the
same instant) the data carry no information about the volatility and ``sigma' = sigma``.

The module also holds two CS2-specific helpers: the exact MR12 map-win probability for i.i.d.
rounds (used by the margin-of-victory transform) and the moments of a logistic-normal variable
(the predictive win probability and its standard deviation under rating uncertainty).
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy import special

FloatArray = npt.NDArray[np.float64]

#: Conversion factor between the Glicko and Glicko-2 scales (``400 / ln 10``).
GLICKO2_SCALE: Final[float] = 173.7178
#: Glicko display rating of ``mu = 0``.
DISPLAY_BASE: Final[float] = 1500.0

_PI2: Final[float] = math.pi * math.pi

# Gauss-Hermite (probabilists') rule for expectations under a standard normal.
_GH_NODES, _GH_WEIGHTS = np.polynomial.hermite_e.hermegauss(48)
_GH_WEIGHTS = _GH_WEIGHTS / math.sqrt(2.0 * math.pi)


def to_display(mu: float) -> float:
    """Glicko-2 ``mu`` -> Glicko display rating ``r = 1500 + 173.7178 mu``."""
    return DISPLAY_BASE + GLICKO2_SCALE * mu


def to_display_rd(phi: float) -> float:
    """Glicko-2 ``phi`` -> Glicko rating deviation ``RD = 173.7178 phi``."""
    return GLICKO2_SCALE * phi


def from_display(r: float, rd: float) -> tuple[float, float]:
    """Glicko ``(r, RD)`` -> Glicko-2 ``(mu, phi)``."""
    return (r - DISPLAY_BASE) / GLICKO2_SCALE, rd / GLICKO2_SCALE


def g(phi: float) -> float:
    """Glicko-2 ``g(phi) = 1 / sqrt(1 + 3 phi^2 / pi^2)``."""
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / _PI2)


def sigmoid(x: float) -> float:
    """Numerically stable logistic function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def expected_score(mu: float, mu_j: float, phi_j: float) -> float:
    """Glicko-2 ``E(mu, mu_j, phi_j)``: expected score against an opponent with uncertain rating."""
    return sigmoid(g(phi_j) * (mu - mu_j))


def volatility_update(
    *,
    phi2: float,
    v: float,
    delta: float,
    sigma: float,
    tau: float,
    periods: float = 1.0,
    eps: float = 1e-6,
    max_iter: int = 200,
) -> float:
    """New volatility ``sigma'`` (Step 5 of the paper, Illinois algorithm).

    ``phi2`` is the pre-period rating variance ``phi^2`` (the part of the prior variance that does
    not depend on the volatility), ``periods`` the length ``c`` of the rating period in units of the
    configured period (``c = 1`` reproduces the paper; see the module docstring).
    """
    if periods <= 0.0:
        return sigma
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    a = math.log(sigma * sigma)
    d2 = delta * delta
    tau2 = tau * tau

    def f(x: float) -> float:
        ex = periods * math.exp(x)
        denom = phi2 + v + ex
        return ex * (d2 - phi2 - v - ex) / (2.0 * denom * denom) - (x - a) / tau2

    big_a = a
    if d2 > phi2 + v:
        big_b = math.log((d2 - phi2 - v) / periods)
    else:
        k = 1
        while f(a - k * tau) < 0.0:
            k += 1
            if k > 10_000:  # pragma: no cover - f(x) -> +inf as x -> -inf, cannot happen
                break
        big_b = a - k * tau
    f_a = f(big_a)
    f_b = f(big_b)
    for _ in range(max_iter):
        if abs(big_b - big_a) <= eps:
            break
        if f_b == f_a:  # pragma: no cover - degenerate flat bracket
            break
        big_c = big_a + (big_a - big_b) * f_a / (f_b - f_a)
        f_c = f(big_c)
        if f_c * f_b <= 0.0:
            big_a, f_a = big_b, f_b
        else:
            f_a /= 2.0
        big_b, f_b = big_c, f_c
    return math.exp(big_a / 2.0)


def glicko2_rate(
    mu: float,
    phi: float,
    sigma: float,
    results: list[tuple[float, float, float]] | tuple[tuple[float, float, float], ...],
    *,
    tau: float,
    eps: float = 1e-6,
) -> tuple[float, float, float]:
    """One standard Glicko-2 rating period (Steps 3-7 of the paper).

    ``results`` holds ``(mu_j, phi_j, s_j)`` for every game of the period. A player without games
    only has the rating deviation inflated: ``phi' = sqrt(phi^2 + sigma^2)``.
    Returns ``(mu', phi', sigma')`` on the Glicko-2 scale.
    """
    if not results:
        return mu, math.sqrt(phi * phi + sigma * sigma), sigma
    inv_v = 0.0
    score_sum = 0.0
    for mu_j, phi_j, s_j in results:
        gj = g(phi_j)
        e = sigmoid(gj * (mu - mu_j))
        inv_v += gj * gj * e * (1.0 - e)
        score_sum += gj * (s_j - e)
    v = 1.0 / inv_v
    delta = v * score_sum
    new_sigma = volatility_update(phi2=phi * phi, v=v, delta=delta, sigma=sigma, tau=tau, eps=eps)
    phi_star2 = phi * phi + new_sigma * new_sigma
    new_phi = 1.0 / math.sqrt(1.0 / phi_star2 + 1.0 / v)
    new_mu = mu + new_phi * new_phi * score_sum
    return new_mu, new_phi, new_sigma


def inflate_phi(phi: float, sigma: float, periods: float, phi_cap: float) -> float:
    """Rating deviation after ``periods`` rating periods without games, capped at ``phi_cap``.

    ``phi_new = min(phi_cap, sqrt(phi^2 + sigma^2 * periods))`` (Step 6 of the paper applied to a
    gap of fractional length; the cap keeps long-inactive teams from becoming *less* certain than a
    brand-new team).
    """
    if periods <= 0.0:
        return min(phi, phi_cap) if phi > phi_cap else phi
    return min(phi_cap, math.sqrt(phi * phi + sigma * sigma * periods))


# ------------------------------------------------------------------------------------- CS2 helpers


@lru_cache(maxsize=4096)
def _mr12_cached(q: float) -> float:
    p = q
    r = 1.0 - q
    # Regulation: first to 13 rounds out of at most 24 (MR12).
    win_reg = sum(math.comb(12 + k, k) * p**13 * r**k for k in range(12))
    tie = math.comb(24, 12) * p**12 * r**12
    # MR3 overtime blocks: first to 4 out of 6; 3-3 starts another block.
    w = sum(math.comb(3 + k, k) * p**4 * r**k for k in range(3))
    lose = sum(math.comb(3 + k, k) * r**4 * p**k for k in range(3))
    p_ot = w / (w + lose) if (w + lose) > 0.0 else 0.5
    return win_reg + tie * p_ot


def mr12_map_win_probability(q: float) -> float:
    """Exact P(win an MR12 map with MR3 overtime) when every round is won independently with ``q``.

    Increasing in ``q`` with ``F(0) = 0``, ``F(0.5) = 0.5``, ``F(1) = 1`` and ``F(1 - q) = 1 - F(q)``.
    Used to turn an observed round share into an *implied* map-win probability (rounds are not
    really independent in CS2 - economy and momentum - so the transform is only blended in mildly).
    """
    q = min(max(float(q), 0.0), 1.0)
    return _mr12_cached(round(q, 6))


def mov_score(team_rounds: int, opponent_rounds: int, mov_weight: float) -> float:
    """Actual score ``s`` of a map result with an optional margin-of-victory component.

    ``s = (1 - lambda) * 1[win] + lambda * F_MR12(round share)`` with ``lambda = mov_weight``. The
    winner of a map always has a round share above one half and ``F_MR12`` is increasing with
    ``F_MR12(0.5) = 0.5``, so a win always gives ``s > 0.5`` (and a loss ``s < 0.5``); the two teams'
    scores sum to one. ``mov_weight = 0`` is the plain win/loss score of Glicko-2.
    """
    total = team_rounds + opponent_rounds
    if total <= 0 or team_rounds == opponent_rounds:
        return 0.5
    win = 1.0 if team_rounds > opponent_rounds else 0.0
    if mov_weight <= 0.0:
        return win
    implied = mr12_map_win_probability(team_rounds / total)
    s = (1.0 - mov_weight) * win + mov_weight * implied
    # Guard the invariant against floating point: a win is strictly above one half.
    if win == 1.0:
        return max(s, 0.5 + 1e-9)
    return min(s, 0.5 - 1e-9)


def logistic_normal_moments(mean: float, var: float) -> tuple[float, float]:
    """Mean and standard deviation of ``p = sigmoid(X)`` with ``X ~ N(mean, var)``.

    Computed with a 48-node Gauss-Hermite rule (exact to ~1e-10 for the logistic function). The
    mean is the Bayesian predictive win probability; the standard deviation is the uncertainty of
    ``p`` induced by the rating uncertainty.
    """
    sd = math.sqrt(max(var, 0.0))
    if sd < 1e-12:
        p = sigmoid(mean)
        return p, 0.0
    z: FloatArray = mean + sd * _GH_NODES
    vals: FloatArray = special.expit(z)
    m1 = float(np.dot(_GH_WEIGHTS, vals))
    m2 = float(np.dot(_GH_WEIGHTS, vals * vals))
    return m1, math.sqrt(max(m2 - m1 * m1, 0.0))


def logit(p: float) -> float:
    """Log-odds, clipped away from 0 and 1 so the result is always finite."""
    q = min(max(p, 1e-12), 1.0 - 1e-12)
    return math.log(q / (1.0 - q))
