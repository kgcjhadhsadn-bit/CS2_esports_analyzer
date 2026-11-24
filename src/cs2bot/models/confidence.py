"""Calibration for the confidence rule (bet when the calibrated probability is >= 75%).

The owner bets on confidence (PLAN.md section 0): any price is taken when ``p >= 0.75`` (odds
ignored; only a win that would pay back less than the stake after the 12% stake tax is skipped), and
the stake grows with the confidence tier. A confident *wrong* probability is therefore the costliest
error, and the high-probability end of the reliability diagram matters more than the average
calibration. This module provides three things:

1. :func:`high_confidence_reliability` - the reliability table restricted to ``[0.75, 1.0]`` in bins of
   2.5 percentage points (``lo`` / ``width`` configurable), each with its count, mean prediction,
   observed rate, Wilson 95% interval and gap.
2. :class:`HighConfidenceShrinker` - a conservative, monotone adjustment of probabilities above the
   threshold. For a prediction ``p`` in bin ``b`` (``n_b`` resolved predictions, ``k_b`` hits, mean
   prediction ``m_b``)::

       skeptic(x) = t + lambda * (x - t)                      # claimed excess confidence, discounted
       r_b        = (kappa * skeptic(m_b) + k_b) / (kappa + n_b)  # Beta posterior of the hit rate
       adjusted   = min(p, p + (r_b - m_b))                   # shift by the estimated miscalibration

   then made non-decreasing in ``p`` on ``(t, 1]`` (a running minimum from the top bin down: an over-confident
   higher bin also caps the bins below it) and never above ``p``. With no history (``n_b = 0``) a
   probability keeps only ``lambda`` of its excess over ``t`` (0.95 -> 0.85 with the defaults); a
   well-populated, well-calibrated bin converges back to ``p``; an over-confident bin is pulled down to
   its observed rate - possibly below the threshold, which then blocks the bet. A thin bin (fewer than
   ``min_bin_predictions``) above an over-confident bin carries that bin's miscalibration upward (final
   review, Lens C: otherwise the empty top bins fell back to the milder no-history prior). It never
   raises a probability (under-confident bins are left alone: being too cautious only costs bets).
   ``p == t`` is adjusted too (review round 1: it used to pass unchanged, so a probability of exactly
   the threshold escaped an over-confident first bin), and a probability below ``t`` is capped at the
   adjusted value of ``t`` so the map stays monotone (``p < t`` is never bet anyway).
3. :class:`MarketTrust` / :func:`assess_market_trust` - a per-market "trusted" flag: enough resolved
   predictions overall and in the high-confidence range, calibration error within tolerance, the
   high-confidence range as a whole within tolerance (observed hit rate at least the mean prediction
   minus ``high_tolerance`` - a point estimate, so a model has to *show* its calibration), and no
   populated 2.5-pp bin significantly over-confident (Wilson upper bound below its mean prediction
   minus the tolerance). Every failed condition is listed in ``reasons``.

:class:`ConfidenceGuard` bundles a shrinker and a trust verdict per market type (fitted from
``(market_type, p, y)`` triples, e.g. the backtest's :class:`~cs2bot.backtest.report.PredictionRecord`
list) plus a pooled fallback for market types without own history; it is JSON-serialisable.

Integration (final review, Lens C): ``services.betting`` reads the guard file once per cycle and
:mod:`cs2bot.engine.confidence` applies ``guard.adjust(market_type, p)`` to every outcome before the
``min_probability`` check and the stake tier (``decision.use_confidence_guard``, default on; without a
guard file the no-history prior applies); ``decision.require_trusted_calibration`` rejects markets where
``guard.is_trusted(market_type)`` is False. The ``confidence_guard_refresh`` job rewrites the file
weekly from a fresh backtest.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
from numpy.typing import ArrayLike

from cs2bot.models.calibration import ece, wilson_interval

FORMAT_VERSION: Final[int] = 1
POOLED: Final[str] = "*"

__all__ = [
    "POOLED",
    "ConfidenceConfig",
    "ConfidenceGuard",
    "HighConfidenceBin",
    "HighConfidenceShrinker",
    "MarketTrust",
    "assess_market_trust",
    "high_confidence_reliability",
    "high_confidence_report",
]


@dataclass(frozen=True)
class ConfidenceConfig:
    """Knobs of the high-confidence calibration (defaults are deliberately conservative)."""

    threshold: float = 0.75  # the decision rule's min_probability
    bin_width: float = 0.025  # 2.5 percentage points
    skeptic_lambda: float = 0.5  # share of the claimed excess confidence kept without evidence
    prior_strength: float = 50.0  # kappa: pseudo-observations behind the skeptical prior per bin
    # trust
    min_predictions: int = 300
    min_high_predictions: int = 60
    max_ece: float = 0.04
    high_tolerance: float = 0.03
    min_bin_predictions: int = 20  # a 2.5-pp bin is checked on its own from this many predictions


DEFAULT_CONFIDENCE = ConfidenceConfig()


@dataclass(frozen=True)
class HighConfidenceBin:
    lower: float
    upper: float
    count: int
    hits: int
    mean_predicted: float
    observed_rate: float
    ci_low: float
    ci_high: float

    @property
    def gap(self) -> float:
        """observed - predicted (negative = over-confident); NaN for an empty bin."""
        return self.observed_rate - self.mean_predicted if self.count else math.nan

    def as_dict(self) -> dict[str, Any]:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "hits": self.hits,
            "mean_predicted": _num(self.mean_predicted),
            "observed_rate": _num(self.observed_rate),
            "ci_low": _num(self.ci_low),
            "ci_high": _num(self.ci_high),
            "gap": _num(self.gap),
        }


def _num(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def _clean(p: ArrayLike, y: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    pa = np.asarray(p, dtype=float).ravel()
    ya = np.asarray(y, dtype=float).ravel()
    if pa.shape != ya.shape:
        raise ValueError("p and y must have the same length")
    ok = np.isfinite(pa) & np.isfinite(ya) & (pa >= 0.0) & (pa <= 1.0)
    return pa[ok], (ya[ok] > 0.5).astype(float)


def _edges(lo: float, width: float) -> list[float]:
    n = max(1, round((1.0 - lo) / width))
    return [lo + i * width for i in range(n)] + [1.0]


def high_confidence_reliability(
    p: ArrayLike,
    y: ArrayLike,
    *,
    lo: float = 0.75,
    width: float = 0.025,
    fold: bool = False,
    include_empty: bool = True,
) -> list[HighConfidenceBin]:
    """Reliability bins over ``[lo, 1.0]`` (``width`` wide; the last bin includes 1.0).

    ``fold=True`` mirrors two-way predictions below ``1 - lo`` (``p -> 1 - p``, ``y -> 1 - y``), so a
    map / match probability of 0.10 for team1 is counted as a 0.90 prediction for team2.
    """
    pa, ya = _clean(p, y)
    if fold:
        low = pa < 1.0 - lo
        pa = np.where(low, 1.0 - pa, pa)
        ya = np.where(low, 1.0 - ya, ya)
    edges = _edges(lo, width)
    out: list[HighConfidenceBin] = []
    for i in range(len(edges) - 1):
        a, b = edges[i], edges[i + 1]
        mask = (pa >= a) & ((pa < b) if i < len(edges) - 2 else (pa <= b))
        n = int(mask.sum())
        if n == 0 and not include_empty:
            continue
        hits = int(ya[mask].sum()) if n else 0
        ci = wilson_interval(float(hits), float(n))
        out.append(
            HighConfidenceBin(
                lower=round(a, 6),
                upper=round(b, 6),
                count=n,
                hits=hits,
                mean_predicted=float(pa[mask].mean()) if n else math.nan,
                observed_rate=hits / n if n else math.nan,
                ci_low=ci[0],
                ci_high=ci[1],
            )
        )
    return out


class HighConfidenceShrinker:
    """Conservative monotone adjustment of probabilities above the threshold (module docstring)."""

    def __init__(self, config: ConfidenceConfig | None = None) -> None:
        self.config = config or DEFAULT_CONFIDENCE
        self.bins: list[HighConfidenceBin] = []
        self._centers: list[float] = []
        self._offsets: list[float] = []
        self.n_high = 0

    def _skeptic(self, x: float) -> float:
        t = self.config.threshold
        return t + self.config.skeptic_lambda * (x - t)

    def fit(self, p: ArrayLike, y: ArrayLike) -> HighConfidenceShrinker:
        cfg = self.config
        self.bins = high_confidence_reliability(p, y, lo=cfg.threshold, width=cfg.bin_width)
        self.n_high = sum(b.count for b in self.bins)
        centers: list[float] = []
        offsets: list[float] = []
        for b in self.bins:
            m = b.mean_predicted if b.count else (b.lower + b.upper) / 2.0
            rate = (cfg.prior_strength * self._skeptic(m) + b.hits) / (cfg.prior_strength + b.count)
            offset = min(0.0, rate - m)
            if offsets and b.count < cfg.min_bin_predictions:
                # final review (Lens C): a thin bin above an over-confident one inherits at least that
                # miscalibration - picks at 97 % that won 70 % say nothing good about 99 % picks, and
                # the no-history prior alone would let a 1.00 pick through at 0.87
                offset = min(offset, offsets[-1])
            centers.append(m)
            offsets.append(offset)
        self._centers, self._offsets = centers, offsets
        return self

    def adjust(self, p: float) -> float:
        """The adjusted probability: never above ``p``, non-decreasing in ``p``.

        From the threshold up the bin-wise adjustment applies (``p == t`` included). Below it ``p`` is
        kept, capped at the adjusted value of the threshold so the map stays monotone.
        """
        t = self.config.threshold
        if not math.isfinite(p):
            return p
        if not self._centers:  # unfitted: the no-evidence prior (identity below the threshold)
            return min(p, self._skeptic(p)) if p >= t else p
        # adjusted value at each bin center, made non-decreasing by a running minimum from the top
        # (conservative: an over-confident higher bin also caps the bins below it)
        values = [c + o for c, o in zip(self._centers, self._offsets, strict=True)]
        for i in range(len(values) - 2, -1, -1):
            values[i] = min(values[i], values[i + 1])
        floor = min(t, values[0])
        if p < t:
            return min(p, floor)
        xs = [t, *self._centers]
        ys = [floor, *values]
        return float(min(p, np.interp(p, xs, ys)))

    def adjust_many(self, p: ArrayLike) -> np.ndarray:
        return np.asarray([self.adjust(float(x)) for x in np.asarray(p, dtype=float).ravel()])

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.__dict__,
            "bins": [b.as_dict() for b in self.bins],
            "centers": self._centers,
            "offsets": self._offsets,
            "n_high": self.n_high,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HighConfidenceShrinker:
        shrinker = cls(ConfidenceConfig(**dict(data.get("config") or {})))
        shrinker._centers = [float(x) for x in data.get("centers", [])]
        shrinker._offsets = [float(x) for x in data.get("offsets", [])]
        if len(shrinker._centers) != len(shrinker._offsets):
            raise ValueError("centers and offsets differ in length")
        shrinker.n_high = int(data.get("n_high", 0))
        shrinker.bins = [
            HighConfidenceBin(
                lower=float(b["lower"]),
                upper=float(b["upper"]),
                count=int(b["count"]),
                hits=int(b["hits"]),
                mean_predicted=math.nan if b.get("mean_predicted") is None else float(b["mean_predicted"]),
                observed_rate=math.nan if b.get("observed_rate") is None else float(b["observed_rate"]),
                ci_low=math.nan if b.get("ci_low") is None else float(b["ci_low"]),
                ci_high=math.nan if b.get("ci_high") is None else float(b["ci_high"]),
            )
            for b in data.get("bins", [])
        ]
        return shrinker


@dataclass(frozen=True)
class MarketTrust:
    market_type: str
    trusted: bool
    n: int
    n_high: int
    ece: float | None
    high_mean_predicted: float | None
    high_observed_rate: float | None
    high_ci_high: float | None
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_type": self.market_type,
            "trusted": self.trusted,
            "n": self.n,
            "n_high": self.n_high,
            "ece": self.ece,
            "high_mean_predicted": self.high_mean_predicted,
            "high_observed_rate": self.high_observed_rate,
            "high_ci_high": self.high_ci_high,
            "reasons": list(self.reasons),
        }


def assess_market_trust(
    market_type: str, p: ArrayLike, y: ArrayLike, *, config: ConfidenceConfig | None = None
) -> MarketTrust:
    """Is the model's confidence on this market backed by enough, well-calibrated history?"""
    cfg = config or DEFAULT_CONFIDENCE
    pa, ya = _clean(p, y)
    n = int(pa.size)
    high = pa >= cfg.threshold
    n_high = int(high.sum())
    reasons: list[str] = []
    e = float(ece(ya, pa)) if n else math.nan
    hm = float(pa[high].mean()) if n_high else math.nan
    ho = float(ya[high].mean()) if n_high else math.nan
    ci_high = wilson_interval(float(ya[high].sum()), float(n_high))[1] if n_high else math.nan
    if n < cfg.min_predictions:
        reasons.append(f"only {n} resolved predictions (< {cfg.min_predictions})")
    if n_high < cfg.min_high_predictions:
        reasons.append(f"only {n_high} predictions >= {cfg.threshold:.2f} (< {cfg.min_high_predictions})")
    if n and not e <= cfg.max_ece:
        reasons.append(f"ECE {e:.3f} > {cfg.max_ece:.3f}")
    if n_high and not ho >= hm - cfg.high_tolerance:
        reasons.append(
            f"over-confident above {cfg.threshold:.2f}: observed {ho:.3f} (95% upper {ci_high:.3f}) "
            f"vs predicted {hm:.3f} (tolerance {cfg.high_tolerance:.3f})"
        )
    for b in high_confidence_reliability(pa, ya, lo=cfg.threshold, width=cfg.bin_width, include_empty=False):
        if b.count >= cfg.min_bin_predictions and b.ci_high < b.mean_predicted - cfg.high_tolerance:
            reasons.append(
                f"bin [{b.lower:.3f}, {b.upper:.3f}) over-confident: observed {b.observed_rate:.3f} "
                f"(95% upper {b.ci_high:.3f}) vs predicted {b.mean_predicted:.3f} over {b.count}"
            )
    return MarketTrust(
        market_type=market_type,
        trusted=not reasons,
        n=n,
        n_high=n_high,
        ece=_num(e),
        high_mean_predicted=_num(hm),
        high_observed_rate=_num(ho),
        high_ci_high=_num(ci_high),
        reasons=tuple(reasons),
    )


def high_confidence_report(
    p: ArrayLike, y: ArrayLike, *, config: ConfidenceConfig | None = None, fold: bool = False
) -> dict[str, Any]:
    """JSON-friendly summary: the 2.5-pp bins above the threshold, totals and the shrinker's effect."""
    cfg = config or DEFAULT_CONFIDENCE
    bins = high_confidence_reliability(p, y, lo=cfg.threshold, width=cfg.bin_width, fold=fold)
    n = sum(b.count for b in bins)
    hits = sum(b.hits for b in bins)
    mean_p = sum(b.mean_predicted * b.count for b in bins if b.count) / n if n else math.nan
    return {
        "threshold": cfg.threshold,
        "bin_width": cfg.bin_width,
        "n": n,
        "hit_rate": _num(hits / n) if n else None,
        "mean_predicted": _num(mean_p),
        "gap": _num(hits / n - mean_p) if n else None,
        "bins": [b.as_dict() for b in bins],
    }


@dataclass
class ConfidenceGuard:
    """Per-market shrinkers + trust flags, with a pooled fallback (``"*"``) for unseen markets."""

    config: ConfidenceConfig = field(default_factory=lambda: DEFAULT_CONFIDENCE)
    shrinkers: dict[str, HighConfidenceShrinker] = field(default_factory=dict)
    trust: dict[str, MarketTrust] = field(default_factory=dict)

    @classmethod
    def fit(
        cls, records: Iterable[tuple[str, float, int]], *, config: ConfidenceConfig | None = None
    ) -> ConfidenceGuard:
        """Fit from ``(market_type, p_win, won)`` triples of resolved predictions (void/push excluded)."""
        cfg = config or DEFAULT_CONFIDENCE
        by_market: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
        for market, p, y in records:
            for key in (str(market), POOLED):
                by_market[key][0].append(float(p))
                by_market[key][1].append(float(y))
        guard = cls(config=cfg)
        for key, (ps, ys) in by_market.items():
            guard.shrinkers[key] = HighConfidenceShrinker(cfg).fit(ps, ys)
            guard.trust[key] = assess_market_trust(key, ps, ys, config=cfg)
        if POOLED not in guard.shrinkers:
            guard.shrinkers[POOLED] = HighConfidenceShrinker(cfg)
            guard.trust[POOLED] = assess_market_trust(POOLED, [], [], config=cfg)
        return guard

    @classmethod
    def from_predictions(
        cls, predictions: Sequence[Any], *, config: ConfidenceConfig | None = None
    ) -> ConfidenceGuard:
        """From objects with ``market_type``, ``p`` and ``y`` (backtest ``PredictionRecord``)."""
        return cls.fit(((r.market_type, r.p, r.y) for r in predictions), config=config)

    def adjust(self, market_type: str, p: float) -> float:
        """Conservatively adjusted probability.

        The market's own shrinker; while its high-confidence history is thin (fewer than
        ``min_high_predictions``) the more conservative of its own and the pooled adjustment, so an
        over-confidence seen across all markets is not ignored for a market with little history.
        """
        own = self.shrinkers.get(str(market_type))
        pooled = self.shrinkers.get(POOLED)
        if own is None and pooled is None:
            return HighConfidenceShrinker(self.config).adjust(p)
        if own is None:
            return pooled.adjust(p) if pooled is not None else p
        value = own.adjust(p)
        if pooled is not None and pooled is not own and own.n_high < self.config.min_high_predictions:
            value = min(value, pooled.adjust(p))
        return value

    def is_trusted(self, market_type: str) -> bool:
        """True only when the market itself passed :func:`assess_market_trust` (never via the pool)."""
        t = self.trust.get(str(market_type))
        return bool(t is not None and t.trusted)

    def reasons(self, market_type: str) -> tuple[str, ...]:
        t = self.trust.get(str(market_type))
        return t.reasons if t is not None else ("no resolved predictions for this market",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "config": self.config.__dict__,
            "shrinkers": {k: v.to_dict() for k, v in self.shrinkers.items()},
            "trust": {k: v.as_dict() for k, v in self.trust.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConfidenceGuard:
        if int(data.get("format_version", 0)) != FORMAT_VERSION:
            raise ValueError("unsupported confidence guard format")
        cfg = ConfidenceConfig(**dict(data.get("config") or {}))
        guard = cls(config=cfg)
        guard.shrinkers = {
            k: HighConfidenceShrinker.from_dict(v) for k, v in data.get("shrinkers", {}).items()
        }
        for k, t in data.get("trust", {}).items():
            guard.trust[k] = MarketTrust(
                market_type=str(t["market_type"]),
                trusted=bool(t["trusted"]),
                n=int(t["n"]),
                n_high=int(t["n_high"]),
                ece=t.get("ece"),
                high_mean_predicted=t.get("high_mean_predicted"),
                high_observed_rate=t.get("high_observed_rate"),
                high_ci_high=t.get("high_ci_high"),
                reasons=tuple(t.get("reasons", ())),
            )
        return guard

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: str | Path) -> ConfidenceGuard:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
