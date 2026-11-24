"""Small numerical helpers shared by the round model, map simulator, player props and match engine.

Everything here is pure numpy/scipy and deterministic. Conventions:

* a *pmf array* is a 1-D float array ``pmf[k] = P(X = k)`` for ``k = 0..len-1`` (sums to 1);
* a *pmf dict* is the JSON form used in :mod:`cs2bot.domain.predictions` (``"17" -> p``).
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Iterable, Mapping, Sequence

import numpy as np
import numpy.typing as npt
from scipy import special

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


def logit(p: float) -> float:
    """Log-odds of ``p`` (clipped away from 0/1 so it is always finite)."""
    q = min(max(float(p), 1e-12), 1.0 - 1e-12)
    return float(np.log(q / (1.0 - q)))


def expit(x: float) -> float:
    """Logistic function for scalars."""
    return float(special.expit(x))


def stable_seed(*parts: int | str | None) -> int:
    """Process-independent 63-bit seed from ints/strings (Python's ``hash`` is salted per process)."""
    words: list[int] = []
    for part in parts:
        if part is None:
            words.append(0x9E3779B9)
        elif isinstance(part, str):
            words.append(zlib.crc32(part.encode("utf-8")))
        else:
            words.append(int(part) & 0xFFFFFFFF)
            words.append((int(part) >> 32) & 0xFFFFFFFF)
    state = np.random.SeedSequence(words).generate_state(2, dtype=np.uint32)
    return int((int(state[0]) << 31) ^ int(state[1]))


def binom_inverse(u: npt.NDArray[np.floating], n: int, p: FloatArray | float) -> npt.NDArray[np.int8]:
    """Binomial(n, p) draws by inverse CDF from ONE uniform per draw (vectorised over ``p``).

    Using exactly one uniform per draw keeps the random stream aligned across calls with different
    parameters (common random numbers), which makes the simulator's map-win probability a smooth,
    almost monotone function of the strength parameter.
    """
    pp = np.clip(np.asarray(p, dtype=np.float64), 1e-9, 1.0 - 1e-9)
    q = 1.0 - pp
    ratio = pp / q
    pmf = q**n
    cdf = pmf.copy()
    k = np.zeros(np.shape(u), dtype=np.int8)
    for j in range(n):
        k += u > cdf
        pmf = pmf * ((n - j) / (j + 1.0)) * ratio
        cdf = cdf + pmf
    return k


def gamma_quadrature(cv2: float, n_nodes: int = 7) -> tuple[FloatArray, FloatArray]:
    """Gauss quadrature of a mean-one Gamma multiplier with squared coefficient of variation ``cv2``.

    Returns ``(nodes, weights)`` (weights sum to 1) such that ``sum w f(x)`` matches ``E[f(X)]`` for
    ``X ~ Gamma(shape=1/cv2, scale=cv2)`` exactly for polynomials of degree ``<= 2 n_nodes - 1`` (mean 1,
    variance ``cv2`` and skewness are reproduced). Generalised Gauss-Laguerre rule computed with the
    Golub-Welsch eigenvalue method, which (unlike ``scipy.special.roots_genlaguerre``) stays finite for
    the large shape parameters of well-estimated rates. ``cv2 <= 0`` -> a single node at 1.
    """
    if not math.isfinite(cv2) or cv2 <= 0.0 or n_nodes <= 1:
        return np.ones(1, dtype=np.float64), np.ones(1, dtype=np.float64)
    shape = 1.0 / cv2
    alpha = shape - 1.0  # generalised Laguerre weight x**alpha * exp(-x), alpha > -1
    k = np.arange(n_nodes, dtype=np.float64)
    diag = 2.0 * k + alpha + 1.0
    kk = np.arange(1, n_nodes, dtype=np.float64)
    off = np.sqrt(kk * (kk + alpha))
    jacobi = np.diag(diag) + np.diag(off, 1) + np.diag(off, -1)
    x, vec = np.linalg.eigh(jacobi)
    w = vec[0] ** 2
    w = w / w.sum()
    return np.asarray(x / shape, dtype=np.float64), np.asarray(w, dtype=np.float64)


def weighted_mean(x: npt.ArrayLike, weights: FloatArray | None = None) -> float:
    arr = np.asarray(x, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    if weights is None:
        return float(arr.mean())
    return float(np.average(arr, weights=weights))


def pmf_from_samples(values: npt.ArrayLike, weights: FloatArray | None = None) -> dict[int, float]:
    """Empirical pmf ``{value: p}`` of integer samples (optionally weighted)."""
    v = np.asarray(values, dtype=np.int64)
    if v.size == 0:
        return {}
    uniq, inv = np.unique(v, return_inverse=True)
    w = np.bincount(inv, weights=weights, minlength=uniq.size).astype(np.float64)
    w /= w.sum()
    return {int(a): float(b) for a, b in zip(uniq, w, strict=True)}


def nb_pmf_matrix(means: npt.ArrayLike, size: float, k_max: int) -> FloatArray:
    """Negative-binomial pmfs, one row per mean: shape ``(len(means), k_max + 1)``.

    Parameterisation: mean ``mu`` and size ``s`` (variance ``mu + mu**2 / s``); ``s -> inf`` is
    Poisson. Computed in log space with ``gammaln`` (vectorised); rows are renormalised over
    ``0..k_max`` (the caller chooses ``k_max`` so the truncated tail is negligible).
    """
    mu = np.maximum(np.asarray(means, dtype=np.float64).reshape(-1), 1e-12)
    k = np.arange(k_max + 1, dtype=np.float64)
    if size >= 1e7:
        logpmf = k[None, :] * np.log(mu)[:, None] - mu[:, None] - special.gammaln(k + 1.0)[None, :]
    else:
        log_p = np.log(size / (size + mu))  # log P(success) in scipy's parameterisation
        log_q = np.log(mu / (size + mu))
        logpmf = (
            special.gammaln(k + size)[None, :]
            - special.gammaln(size)
            - special.gammaln(k + 1.0)[None, :]
            + size * log_p[:, None]
            + k[None, :] * log_q[:, None]
        )
    mat = np.exp(logpmf)
    row = mat.sum(axis=1, keepdims=True)
    row[row <= 0] = 1.0
    return np.asarray(mat / row, dtype=np.float64)


def nb_support(max_mean: float, size: float, *, sd_mult: float = 12.0, cap: int = 600) -> int:
    """A ``k_max`` that keeps the NB tail mass far below 1e-10 for means up to ``max_mean``."""
    var = max_mean + max_mean * max_mean / max(size, 1e-6)
    return int(min(cap, np.ceil(max_mean + sd_mult * np.sqrt(var) + 12)))


def pmf_moments(pmf: FloatArray) -> tuple[float, float]:
    """Mean and variance of a pmf array."""
    k = np.arange(pmf.size, dtype=np.float64)
    total = float(pmf.sum())
    if total <= 0:
        return 0.0, 0.0
    mean = float((k * pmf).sum() / total)
    var = float(((k - mean) ** 2 * pmf).sum() / total)
    return mean, max(var, 0.0)


def trim_pmf(pmf: FloatArray, tail: float = 1e-12) -> FloatArray:
    """Drop the trailing tail with mass below ``tail`` and renormalise."""
    arr = np.asarray(pmf, dtype=np.float64)
    if arr.size == 0:
        return np.array([1.0])
    total = float(arr.sum())
    if total <= 0:
        return np.array([1.0])
    arr = arr / total
    rev_cum = np.cumsum(arr[::-1])[::-1]  # rev_cum[k] = P(X >= k)
    keep = np.flatnonzero(rev_cum > tail)
    last = int(keep[-1]) + 1 if keep.size else 1
    out = arr[:last]
    return out / out.sum()


def pad_add(acc: FloatArray, add: FloatArray, weight: float = 1.0) -> FloatArray:
    """``acc + weight * add`` for pmf arrays of different lengths."""
    n = max(acc.size, add.size)
    out = np.zeros(n, dtype=np.float64)
    out[: acc.size] += acc
    out[: add.size] += weight * add
    return out


def mix_pmfs(items: Iterable[tuple[float, FloatArray]]) -> FloatArray:
    """Weighted mixture of pmf arrays (weights are renormalised)."""
    acc = np.zeros(1, dtype=np.float64)
    total = 0.0
    for w, pmf in items:
        if w <= 0:
            continue
        acc = pad_add(acc, pmf, w)
        total += w
    if total <= 0:
        return np.array([1.0])
    return acc / total


def convolve(a: FloatArray, b: FloatArray) -> FloatArray:
    """Distribution of the sum of two independent non-negative integer variables."""
    return np.convolve(a, b)


def pmf_to_dict(pmf: FloatArray, *, tail: float = 1e-12) -> dict[str, float]:
    """pmf array -> ``{"k": p}`` with negligible tail entries removed (renormalised, sums to 1)."""
    arr = trim_pmf(pmf, tail)
    return {str(k): float(p) for k, p in enumerate(arr) if p > 0.0}


def mix_dicts(items: Sequence[tuple[float, Mapping[str, float]]]) -> dict[str, float]:
    """Weighted mixture of string-keyed probability dicts (weights are renormalised).

    Keys missing from a component count as 0 for that component.
    """
    total = sum(w for w, _ in items if w > 0)
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for w, d in items:
        if w <= 0:
            continue
        for key, val in d.items():
            out[key] = out.get(key, 0.0) + w * float(val) / total
    return out


def sort_score_keys(d: Mapping[str, float]) -> dict[str, float]:
    """Order ``"a-b"`` score keys by (a, b) for readable JSON."""

    def _key(s: str) -> tuple[int, int]:
        a, _, b = s.partition("-")
        try:
            return int(a), int(b)
        except ValueError:
            return (10**6, 10**6)

    return {k: d[k] for k in sorted(d, key=_key)}


__all__ = [
    "FloatArray",
    "IntArray",
    "binom_inverse",
    "convolve",
    "expit",
    "gamma_quadrature",
    "logit",
    "mix_dicts",
    "mix_pmfs",
    "nb_pmf_matrix",
    "nb_support",
    "pad_add",
    "pmf_from_samples",
    "pmf_moments",
    "pmf_to_dict",
    "sort_score_keys",
    "stable_seed",
    "trim_pmf",
    "weighted_mean",
]
