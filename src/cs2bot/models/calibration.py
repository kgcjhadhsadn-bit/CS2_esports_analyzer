"""Probability calibration and calibration metrics.

Calibrators map a model's raw probability ``p`` to a calibrated probability. They are fitted on a
**time-ordered holdout** (the latest window of finished maps the model never trained on), never on
the training data itself, so the correction they learn is the model's real out-of-sample bias.

* :class:`IsotonicCalibrator` - monotone, non-parametric (pool-adjacent-violators via scikit-learn);
  the fitted step function is stored as its breakpoints and evaluated by linear interpolation
  between them (the same rule scikit-learn's ``IsotonicRegression.predict`` uses). Needs a few
  hundred holdout points to be reliable.
* :class:`PlattCalibrator` - logistic regression on the logit of ``p``
  (``p' = sigmoid(a * logit(p) + b)``), fitted with Platt's smoothed targets
  ``(N+ + 1) / (N+ + 2)`` and ``1 / (N- + 2)`` and a small ridge on ``(a - 1, b)``. Two parameters,
  so it is the safer choice for small holdouts.
* :class:`IdentityCalibrator` - no correction (used when the holdout is too small).

Every calibrator clips its output to ``[eps, 1 - eps]``: a calibrated probability of exactly 0 or 1
would make the log-loss infinite and the Kelly stake absurd.

Metrics (``y`` are 0/1 outcomes, ``p`` the predicted probabilities of ``y == 1``): :func:`brier`,
:func:`log_loss`, :func:`ece`, :func:`reliability_table` (bins with Wilson confidence intervals),
:func:`sharpness` and :func:`calibration_report`, which bundles them (plus the Murphy decomposition
of the Brier score) into a JSON-serialisable dict for the dashboard and the backtest report.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, Protocol, runtime_checkable

import numpy as np
from numpy.typing import ArrayLike, NDArray
from pydantic import BaseModel, ConfigDict
from scipy.optimize import minimize

FloatArray = NDArray[np.float64]

DEFAULT_EPS: Final[float] = 1e-4
_LOG_LOSS_EPS: Final[float] = 1e-15
BinStrategy = Literal["uniform", "quantile"]

__all__ = [
    "DEFAULT_EPS",
    "AnyCalibrator",
    "Calibrator",
    "IdentityCalibrator",
    "IsotonicCalibrator",
    "PlattCalibrator",
    "ReliabilityBin",
    "brier",
    "calibration_report",
    "calibrator_from_dict",
    "ece",
    "fit_logistic",
    "load_calibrator",
    "log_loss",
    "logit",
    "mce",
    "reliability_table",
    "sharpness",
    "sigmoid",
    "wilson_interval",
]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def logit(p: ArrayLike, eps: float = 1e-6) -> FloatArray:
    """``log(p / (1 - p))`` with ``p`` clipped to ``[eps, 1 - eps]``."""
    q = np.clip(np.asarray(p, dtype=float), eps, 1.0 - eps)
    return np.asarray(np.log(q / (1.0 - q)), dtype=float)


def sigmoid(x: ArrayLike) -> FloatArray:
    """Numerically stable logistic function."""
    z = np.asarray(x, dtype=float)
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def _as_pair(y: ArrayLike, p: ArrayLike) -> tuple[FloatArray, FloatArray]:
    """Validate and flatten ``(y, p)``; raises ``ValueError`` on mismatched or invalid input."""
    ya = np.asarray(y, dtype=float).ravel()
    pa = np.asarray(p, dtype=float).ravel()
    if ya.shape != pa.shape:
        raise ValueError(f"y and p must have the same length, got {ya.size} and {pa.size}")
    if ya.size and (not np.all(np.isfinite(ya)) or not np.all(np.isfinite(pa))):
        raise ValueError("y and p must be finite")
    if ya.size and (ya.min() < 0.0 or ya.max() > 1.0):
        raise ValueError("outcomes y must lie in [0, 1]")
    if pa.size and (pa.min() < 0.0 or pa.max() > 1.0):
        raise ValueError("probabilities p must lie in [0, 1]")
    return ya, pa


def _check_eps(eps: float) -> float:
    if not 0.0 < eps < 0.5:
        raise ValueError(f"eps must be in (0, 0.5), got {eps!r}")
    return float(eps)


# ---------------------------------------------------------------------------------------------
# calibrators
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class Calibrator(Protocol):
    """Common interface of the calibrators (all are JSON-persistable)."""

    kind: str
    eps: float

    def fit(self, p: ArrayLike, y: ArrayLike, sample_weight: ArrayLike | None = None) -> Calibrator: ...

    def transform(self, p: ArrayLike) -> FloatArray: ...

    def to_dict(self) -> dict[str, Any]: ...


class _BaseCalibrator:
    kind: str = "base"

    def __init__(self, *, eps: float = DEFAULT_EPS) -> None:
        self.eps = _check_eps(eps)
        self.fitted = False
        self.n_fit = 0

    def _clip(self, values: FloatArray) -> FloatArray:
        return np.clip(values, self.eps, 1.0 - self.eps)

    def transform(self, p: ArrayLike) -> FloatArray:  # pragma: no cover - overridden
        raise NotImplementedError

    def __call__(self, p: float) -> float:
        """Calibrate a single probability."""
        return float(self.transform(np.asarray([p], dtype=float))[0])

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def save(self, path: str | Path) -> Path:
        """Write the calibrator as JSON (no pickle)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return target


class IdentityCalibrator(_BaseCalibrator):
    """No correction apart from clipping to ``[eps, 1 - eps]``."""

    kind = "identity"

    def fit(self, p: ArrayLike, y: ArrayLike, sample_weight: ArrayLike | None = None) -> IdentityCalibrator:
        _, pa = _as_pair(y, p)
        self.n_fit = int(pa.size)
        self.fitted = True
        return self

    def transform(self, p: ArrayLike) -> FloatArray:
        return self._clip(np.asarray(p, dtype=float))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "eps": self.eps, "n_fit": self.n_fit}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IdentityCalibrator:
        cal = cls(eps=float(data.get("eps", DEFAULT_EPS)))
        cal.n_fit = int(data.get("n_fit", 0))
        cal.fitted = True
        return cal

    @classmethod
    def load(cls, path: str | Path) -> IdentityCalibrator:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


class IsotonicCalibrator(_BaseCalibrator):
    """Monotone non-decreasing calibration map (isotonic regression).

    Outside the range of the fitted probabilities the map is constant (the end values), inside it
    is the piecewise-linear interpolation of the pooled steps. With fewer than two distinct fitted
    probabilities the calibrator falls back to the observed mean outcome (a constant map).
    """

    kind = "isotonic"

    def __init__(self, *, eps: float = DEFAULT_EPS) -> None:
        super().__init__(eps=eps)
        self.x_: FloatArray = np.zeros(0)
        self.y_: FloatArray = np.zeros(0)

    def fit(self, p: ArrayLike, y: ArrayLike, sample_weight: ArrayLike | None = None) -> IsotonicCalibrator:
        from sklearn.isotonic import IsotonicRegression

        ya, pa = _as_pair(y, p)
        if pa.size == 0:
            raise ValueError("cannot fit a calibrator on zero samples")
        w = None if sample_weight is None else np.asarray(sample_weight, dtype=float).ravel()
        if w is not None and w.shape != pa.shape:
            raise ValueError("sample_weight must have the same length as p")
        if np.unique(pa).size < 2:
            mean = float(np.average(ya, weights=w))
            self.x_ = np.asarray([float(pa[0])], dtype=float)
            self.y_ = self._clip(np.asarray([mean], dtype=float))
        else:
            iso = IsotonicRegression(
                y_min=self.eps, y_max=1.0 - self.eps, increasing=True, out_of_bounds="clip"
            )
            iso.fit(pa, ya, sample_weight=w)
            self.x_ = np.asarray(iso.X_thresholds_, dtype=float)
            self.y_ = self._clip(np.asarray(iso.y_thresholds_, dtype=float))
        self.n_fit = int(pa.size)
        self.fitted = True
        return self

    def transform(self, p: ArrayLike) -> FloatArray:
        if not self.fitted:
            raise RuntimeError("IsotonicCalibrator is not fitted")
        pa = np.asarray(p, dtype=float)
        if self.x_.size == 1:
            return self._clip(np.full(pa.shape, float(self.y_[0])))
        return self._clip(np.interp(pa, self.x_, self.y_))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "eps": self.eps,
            "n_fit": self.n_fit,
            "x": [float(v) for v in self.x_],
            "y": [float(v) for v in self.y_],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IsotonicCalibrator:
        cal = cls(eps=float(data.get("eps", DEFAULT_EPS)))
        x = np.asarray(data["x"], dtype=float)
        y = np.asarray(data["y"], dtype=float)
        if x.shape != y.shape or x.size == 0:
            raise ValueError("isotonic calibrator needs matching, non-empty x / y breakpoints")
        cal.x_, cal.y_ = x, y
        cal.n_fit = int(data.get("n_fit", 0))
        cal.fitted = True
        return cal

    @classmethod
    def load(cls, path: str | Path) -> IsotonicCalibrator:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


class PlattCalibrator(_BaseCalibrator):
    """``p' = sigmoid(a * logit(p) + b)`` fitted by penalised maximum likelihood.

    ``ridge`` shrinks ``(a, b)`` towards the identity map ``(1, 0)``; Platt's target smoothing keeps
    the fit finite when the holdout is perfectly separable.
    """

    kind = "platt"

    def __init__(self, *, eps: float = DEFAULT_EPS, ridge: float = 1e-3, smooth_targets: bool = True) -> None:
        super().__init__(eps=eps)
        if ridge < 0:
            raise ValueError("ridge must be >= 0")
        self.ridge = float(ridge)
        self.smooth_targets = smooth_targets
        self.a = 1.0
        self.b = 0.0

    def fit(self, p: ArrayLike, y: ArrayLike, sample_weight: ArrayLike | None = None) -> PlattCalibrator:
        ya, pa = _as_pair(y, p)
        if pa.size == 0:
            raise ValueError("cannot fit a calibrator on zero samples")
        w = np.ones_like(pa) if sample_weight is None else np.asarray(sample_weight, dtype=float).ravel()
        if w.shape != pa.shape:
            raise ValueError("sample_weight must have the same length as p")
        target = ya.copy()
        if self.smooth_targets:
            n_pos = float(np.sum(w * (ya >= 0.5)))
            n_neg = float(np.sum(w * (ya < 0.5)))
            hi, lo = (n_pos + 1.0) / (n_pos + 2.0), 1.0 / (n_neg + 2.0)
            target = np.where(ya >= 0.5, hi, lo)
        x = logit(pa)
        a, b = fit_logistic(x[:, None], target, weights=w, ridge=self.ridge, center=np.asarray([1.0]))
        self.a, self.b = float(a[0]), float(b)
        self.n_fit = int(pa.size)
        self.fitted = True
        return self

    def transform(self, p: ArrayLike) -> FloatArray:
        if not self.fitted:
            raise RuntimeError("PlattCalibrator is not fitted")
        return self._clip(sigmoid(self.a * logit(p) + self.b))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "eps": self.eps,
            "n_fit": self.n_fit,
            "a": self.a,
            "b": self.b,
            "ridge": self.ridge,
            "smooth_targets": self.smooth_targets,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlattCalibrator:
        cal = cls(
            eps=float(data.get("eps", DEFAULT_EPS)),
            ridge=float(data.get("ridge", 1e-3)),
            smooth_targets=bool(data.get("smooth_targets", True)),
        )
        cal.a, cal.b = float(data["a"]), float(data["b"])
        cal.n_fit = int(data.get("n_fit", 0))
        cal.fitted = True
        return cal

    @classmethod
    def load(cls, path: str | Path) -> PlattCalibrator:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


AnyCalibrator = IdentityCalibrator | IsotonicCalibrator | PlattCalibrator


def calibrator_from_dict(data: Mapping[str, Any]) -> AnyCalibrator:
    """Rebuild any calibrator from its :meth:`to_dict` output (dispatch on ``kind``)."""
    kind = data.get("kind")
    if kind == IsotonicCalibrator.kind:
        return IsotonicCalibrator.from_dict(data)
    if kind == PlattCalibrator.kind:
        return PlattCalibrator.from_dict(data)
    if kind == IdentityCalibrator.kind:
        return IdentityCalibrator.from_dict(data)
    raise ValueError(f"unknown calibrator kind {kind!r}")


def load_calibrator(path: str | Path) -> AnyCalibrator:
    """Load a calibrator saved with ``save(path)``, whatever its kind."""
    return calibrator_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def fit_logistic(
    x: ArrayLike,
    y: ArrayLike,
    *,
    weights: ArrayLike | None = None,
    ridge: float = 0.0,
    center: ArrayLike | None = None,
    lower_bounds: Sequence[float | None] | None = None,
) -> tuple[FloatArray, float]:
    """Penalised logistic regression ``P(y=1) = sigmoid(x @ coef + intercept)`` (L-BFGS-B).

    ``y`` may be soft targets in [0, 1]. ``ridge`` penalises ``||coef - center||^2`` (the intercept
    is not penalised); ``lower_bounds`` optionally bounds each coefficient from below (e.g. ``0`` to
    keep a stacking weight non-negative). Returns ``(coef, intercept)``.
    """
    xa = np.asarray(x, dtype=float)
    if xa.ndim == 1:
        xa = xa[:, None]
    ya = np.asarray(y, dtype=float).ravel()
    n, k = xa.shape
    if ya.size != n:
        raise ValueError("x and y must have the same number of rows")
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float).ravel()
    c0 = np.zeros(k) if center is None else np.asarray(center, dtype=float).ravel()
    total_w = float(w.sum()) or 1.0

    def objective(theta: FloatArray) -> tuple[float, FloatArray]:
        coef, intercept = theta[:k], theta[k]
        z = xa @ coef + intercept
        # log(1 + exp(z)) - y z, stable
        loss = np.sum(w * (np.logaddexp(0.0, z) - ya * z)) / total_w
        resid = (sigmoid(z) - ya) * w / total_w
        grad = np.empty(k + 1)
        grad[:k] = xa.T @ resid + 2.0 * ridge * (coef - c0)
        grad[k] = float(resid.sum())
        loss += ridge * float(np.sum((coef - c0) ** 2))
        return float(loss), grad

    bounds: list[tuple[float | None, float | None]] = [
        (None if lower_bounds is None else lower_bounds[i], None) for i in range(k)
    ]
    bounds.append((None, None))
    start = np.concatenate([np.maximum(c0, [b if b is not None else -np.inf for b, _ in bounds[:k]]), [0.0]])
    res = minimize(objective, start, jac=True, method="L-BFGS-B", bounds=bounds)
    theta = np.asarray(res.x, dtype=float)
    return theta[:k], float(theta[k])


# ---------------------------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------------------------


def brier(y: ArrayLike, p: ArrayLike) -> float:
    """Mean squared error of the probabilities (``nan`` for empty input)."""
    ya, pa = _as_pair(y, p)
    if ya.size == 0:
        return float("nan")
    return float(np.mean((pa - ya) ** 2))


def log_loss(y: ArrayLike, p: ArrayLike, *, eps: float = _LOG_LOSS_EPS) -> float:
    """Mean negative log-likelihood (natural log), ``p`` clipped to ``[eps, 1 - eps]``."""
    ya, pa = _as_pair(y, p)
    if ya.size == 0:
        return float("nan")
    q = np.clip(pa, eps, 1.0 - eps)
    return float(-np.mean(ya * np.log(q) + (1.0 - ya) * np.log(1.0 - q)))


def sharpness(p: ArrayLike) -> float:
    """Spread of the predictions: their standard deviation (0 = always the same probability).

    A calibrated model is better the sharper it is; a constant base-rate forecast is calibrated but
    has sharpness 0. ``nan`` for empty input.
    """
    pa = np.asarray(p, dtype=float).ravel()
    if pa.size == 0:
        return float("nan")
    return float(np.std(pa))


def wilson_interval(successes: float, n: float, *, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (default 95%). ``(nan, nan)`` for ``n == 0``."""
    if n <= 0:
        return float("nan"), float("nan")
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(max(phat * (1.0 - phat) / n + z * z / (4.0 * n * n), 0.0)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


class ReliabilityBin(BaseModel):
    """One bin of a reliability diagram."""

    model_config = ConfigDict(frozen=True)

    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float
    ci_low: float
    ci_high: float

    @property
    def gap(self) -> float:
        """``observed_rate - mean_predicted`` (positive = the model is under-confident here)."""
        return self.observed_rate - self.mean_predicted


def _bin_edges(p: FloatArray, n_bins: int, strategy: BinStrategy) -> FloatArray:
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    if strategy == "uniform":
        return np.linspace(0.0, 1.0, n_bins + 1)
    if strategy == "quantile":
        edges = np.quantile(p, np.linspace(0.0, 1.0, n_bins + 1)) if p.size else np.linspace(0, 1, n_bins + 1)
        edges = np.unique(edges)
        edges[0], edges[-1] = 0.0, 1.0
        if edges.size < 2:
            return np.asarray([0.0, 1.0])
        return np.asarray(edges, dtype=float)
    raise ValueError(f"unknown binning strategy {strategy!r} (use 'uniform' or 'quantile')")


def _assign_bins(p: FloatArray, edges: FloatArray) -> NDArray[np.int64]:
    # right-closed except the first bin; p == 1 lands in the last bin
    idx = np.searchsorted(edges, p, side="left") - 1
    return np.asarray(np.clip(idx, 0, edges.size - 2), dtype=np.int64)


def reliability_table(
    y: ArrayLike, p: ArrayLike, n_bins: int = 10, *, strategy: BinStrategy = "uniform"
) -> list[ReliabilityBin]:
    """Reliability-diagram bins (empty bins are omitted), each with a 95% Wilson interval."""
    ya, pa = _as_pair(y, p)
    if ya.size == 0:
        return []
    edges = _bin_edges(pa, n_bins, strategy)
    idx = _assign_bins(pa, edges)
    out: list[ReliabilityBin] = []
    for b in range(edges.size - 1):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        obs = float(ya[mask].mean())
        lo, hi = wilson_interval(float(ya[mask].sum()), n)
        out.append(
            ReliabilityBin(
                lower=float(edges[b]),
                upper=float(edges[b + 1]),
                count=n,
                mean_predicted=float(pa[mask].mean()),
                observed_rate=obs,
                ci_low=lo,
                ci_high=hi,
            )
        )
    return out


def ece(y: ArrayLike, p: ArrayLike, n_bins: int = 10, strategy: BinStrategy = "uniform") -> float:
    """Expected calibration error: ``sum_b (n_b / N) * |mean p_b - observed rate_b|``."""
    bins = reliability_table(y, p, n_bins, strategy=strategy)
    total = sum(b.count for b in bins)
    if total == 0:
        return float("nan")
    return float(sum(b.count * abs(b.observed_rate - b.mean_predicted) for b in bins) / total)


def mce(y: ArrayLike, p: ArrayLike, n_bins: int = 10, strategy: BinStrategy = "uniform") -> float:
    """Maximum calibration error over the non-empty bins."""
    bins = reliability_table(y, p, n_bins, strategy=strategy)
    if not bins:
        return float("nan")
    return float(max(abs(b.observed_rate - b.mean_predicted) for b in bins))


def _finite_or_none(value: float) -> float | None:
    return value if math.isfinite(value) else None


def calibration_report(y: ArrayLike, p: ArrayLike, n_bins: int = 10) -> dict[str, Any]:
    """Every calibration metric in one JSON-serialisable dict (``None`` where undefined).

    Keys: ``n``, ``base_rate``, ``mean_predicted``, ``brier``, ``log_loss``, ``ece`` (uniform bins),
    ``ece_quantile``, ``mce``, ``sharpness``, ``brier_climatology`` (Brier of always predicting the
    base rate), ``brier_skill`` (``1 - brier / brier_climatology``), the Murphy decomposition
    ``reliability`` - ``resolution`` + ``uncertainty`` (on the uniform bins; it equals the Brier score
    up to the within-bin spread of p) and ``bins`` (the reliability table as dicts).
    """
    ya, pa = _as_pair(y, p)
    n = int(ya.size)
    if n == 0:
        return {
            "n": 0,
            "base_rate": None,
            "mean_predicted": None,
            "brier": None,
            "log_loss": None,
            "ece": None,
            "ece_quantile": None,
            "mce": None,
            "sharpness": None,
            "brier_climatology": None,
            "brier_skill": None,
            "reliability": None,
            "resolution": None,
            "uncertainty": None,
            "bins": [],
        }
    base = float(ya.mean())
    bins = reliability_table(ya, pa, n_bins)
    b_score = brier(ya, pa)
    clim = base * (1.0 - base)
    rel = sum(b.count * (b.mean_predicted - b.observed_rate) ** 2 for b in bins) / n
    res = sum(b.count * (b.observed_rate - base) ** 2 for b in bins) / n
    return {
        "n": n,
        "base_rate": base,
        "mean_predicted": float(pa.mean()),
        "brier": b_score,
        "log_loss": log_loss(ya, pa),
        "ece": ece(ya, pa, n_bins, "uniform"),
        "ece_quantile": ece(ya, pa, n_bins, "quantile"),
        "mce": mce(ya, pa, n_bins),
        "sharpness": sharpness(pa),
        "brier_climatology": clim,
        "brier_skill": _finite_or_none(1.0 - b_score / clim) if clim > 0 else None,
        "reliability": float(rel),
        "resolution": float(res),
        "uncertainty": clim,
        "bins": [b.model_dump() for b in bins],
    }
