"""deep_data: high-confidence reliability, the conservative shrinker, per-market trust, the guard."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from cs2bot.backtest import run_synthetic_backtest
from cs2bot.models.confidence import (
    POOLED,
    ConfidenceConfig,
    ConfidenceGuard,
    HighConfidenceShrinker,
    assess_market_trust,
    high_confidence_reliability,
    high_confidence_report,
)
from cs2bot.testing.synthetic import generate_universe


def _calibrated(n: int, seed: int, lo: float = 0.0, hi: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    p = rng.uniform(lo, hi, n)
    return p, (rng.random(n) < p).astype(float)


def test_reliability_bins_cover_075_to_1_in_25pp_steps() -> None:
    p = np.array([0.74, 0.75, 0.76, 0.799, 0.80, 0.99, 1.0, 0.1])
    y = np.array([1, 1, 0, 1, 1, 1, 1, 0])
    bins = high_confidence_reliability(p, y)
    assert len(bins) == 10
    assert [b.lower for b in bins][:3] == [0.75, 0.775, 0.8] and bins[-1].upper == 1.0
    assert [b.count for b in bins] == [2, 1, 1, 0, 0, 0, 0, 0, 0, 2]
    assert bins[0].hits == 1 and bins[0].observed_rate == 0.5 and bins[-1].observed_rate == 1.0
    assert math.isnan(bins[3].gap) and bins[0].ci_low < 0.5 < bins[0].ci_high
    folded = high_confidence_reliability(p, y, fold=True)  # 0.1 / lost -> 0.9 / won
    assert sum(b.count for b in folded) == 7
    rep = high_confidence_report(p, y)
    assert rep["n"] == 6 and rep["threshold"] == 0.75 and len(rep["bins"]) == 10


def test_shrinker_without_history_keeps_only_part_of_the_excess_confidence() -> None:
    s = HighConfidenceShrinker()
    assert s.adjust(0.95) == pytest.approx(0.85)
    assert s.adjust(0.70) == 0.70 and s.adjust(0.75) == 0.75
    fitted_empty = HighConfidenceShrinker().fit([], [])
    assert fitted_empty.adjust(0.95) == pytest.approx(0.85, abs=0.01)


def test_shrinker_trusts_well_populated_calibrated_bins_and_punishes_over_confidence() -> None:
    p, y = _calibrated(40_000, 1, 0.75, 1.0)
    good = HighConfidenceShrinker().fit(p, y)
    for x in (0.78, 0.85, 0.92, 0.97):
        assert x - 0.015 <= good.adjust(x) <= x
    # over-confident model: claims 0.95 but wins 80 %
    rng = np.random.default_rng(2)
    bad_p = np.full(2000, 0.95)
    bad_y = (rng.random(2000) < 0.80).astype(float)
    bad = HighConfidenceShrinker().fit(bad_p, bad_y)
    assert bad.adjust(0.95) <= 0.82
    # under-confident bins are never raised
    under = HighConfidenceShrinker().fit(np.full(3000, 0.8), np.ones(3000))
    assert under.adjust(0.8) <= 0.8


def test_shrinker_is_monotone_never_raises_and_round_trips() -> None:
    rng = np.random.default_rng(3)
    p = rng.uniform(0.75, 1.0, 1500)
    y = (rng.random(1500) < np.clip(p - 0.08 * (p > 0.9), 0, 1)).astype(float)
    s = HighConfidenceShrinker(ConfidenceConfig(prior_strength=20)).fit(p, y)
    grid = np.linspace(0.5, 1.0, 401)
    adjusted = s.adjust_many(grid)
    assert np.all(adjusted <= grid + 1e-12)
    above = grid > 0.75  # non-decreasing above the threshold (at or below it p is returned as is)
    assert np.all(np.diff(adjusted[above]) >= -1e-9)
    assert np.array_equal(adjusted[~above], grid[~above])
    copy = HighConfidenceShrinker.from_dict(s.to_dict())
    assert np.allclose(copy.adjust_many(grid), adjusted)


def test_market_trust_needs_history_and_calibration() -> None:
    p, y = _calibrated(4000, 4)
    ok = assess_market_trust("match_winner", p, y)
    assert ok.trusted and ok.reasons == () and ok.n == 4000 and ok.n_high > 900
    few = assess_market_trust("map_winner", p[:100], y[:100])
    assert not few.trusted and any("resolved predictions" in r for r in few.reasons)
    over_p = np.concatenate([p, np.full(500, 0.95)])
    over_y = np.concatenate([y, (np.random.default_rng(5).random(500) < 0.75).astype(float)])
    over = assess_market_trust("total_maps", over_p, over_y)
    assert not over.trusted and any("over-confident" in r for r in over.reasons)


def test_guard_fit_adjust_trust_and_persistence(tmp_path: Path) -> None:
    p, y = _calibrated(5000, 6)
    records = [("match_winner", float(a), int(b)) for a, b in zip(p, y, strict=True)]
    records += [("map_winner", 0.9, 1), ("map_winner", 0.9, 0)]
    guard = ConfidenceGuard.fit(records)
    assert guard.is_trusted("match_winner") and not guard.is_trusted("map_winner")
    assert not guard.is_trusted("player_kills") and guard.reasons("player_kills")
    assert POOLED in guard.shrinkers
    assert guard.adjust("match_winner", 0.9) == pytest.approx(0.9, abs=0.02)
    # a market with (almost) no own history uses its own sparse bins; an unseen one the pooled shrinker
    assert guard.adjust("unknown_market", 0.9) == pytest.approx(guard.shrinkers[POOLED].adjust(0.9))
    path = guard.save(tmp_path / "guard.json")
    loaded = ConfidenceGuard.load(path)
    assert loaded.is_trusted("match_winner") and not loaded.is_trusted("map_winner")
    assert loaded.adjust("map_winner", 0.93) == pytest.approx(guard.adjust("map_winner", 0.93))
    with pytest.raises(ValueError):
        ConfidenceGuard.from_dict({"format_version": 99})


def test_backtest_report_carries_high_confidence_and_trust() -> None:
    uni = generate_universe(n_teams=8, n_matches=80, seed=4, days=150, with_kills=False)
    report = run_synthetic_backtest(uni, model="oracle", truth_sims=300)
    assert report.high_confidence["threshold"] == 0.75 and len(report.high_confidence["bins"]) == 10
    assert POOLED in report.market_trust and "match_winner" in report.market_trust
    assert report.predictions and "predictions" not in report.model_dump(mode="json")
    guard = ConfidenceGuard.from_predictions(report.predictions)
    assert set(guard.trust) == set(report.market_trust)
