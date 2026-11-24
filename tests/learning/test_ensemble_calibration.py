"""Calibrators and calibration metrics (cs2bot.models.calibration)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from cs2bot.models.calibration import (
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    brier,
    calibration_report,
    calibrator_from_dict,
    ece,
    fit_logistic,
    load_calibrator,
    log_loss,
    logit,
    mce,
    reliability_table,
    sharpness,
    sigmoid,
    wilson_interval,
)


def _overconfident(n: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """True p, outcomes, and a predictor that doubles the true logit (badly over-confident)."""
    rng = np.random.default_rng(seed)
    p_true = rng.uniform(0.08, 0.92, size=n)
    y = (rng.random(n) < p_true).astype(float)
    q = sigmoid(2.0 * logit(p_true))
    return p_true, y, q


# ------------------------------------------------------------------------------------- metrics


def test_brier_and_log_loss_known_values() -> None:
    y = [1, 0, 1, 0]
    p = [0.8, 0.3, 0.6, 0.1]
    assert brier(y, p) == pytest.approx((0.04 + 0.09 + 0.16 + 0.01) / 4)
    expected = -(math.log(0.8) + math.log(0.7) + math.log(0.6) + math.log(0.9)) / 4
    assert log_loss(y, p) == pytest.approx(expected)
    # perfect / coin-flip forecasts
    assert brier([1, 0], [1.0, 0.0]) == 0.0
    assert log_loss([1, 0], [0.5, 0.5]) == pytest.approx(math.log(2))
    # clipping keeps the log-loss finite
    assert math.isfinite(log_loss([1], [0.0]))


def test_ece_mce_and_reliability_table_by_hand() -> None:
    # bin [0.1, 0.2): p = .15, .15 -> observed 1/2 ; bin [0.8, 0.9): p = .85 x3 -> observed 3/3
    y = [1, 0, 1, 1, 1]
    p = [0.15, 0.15, 0.85, 0.85, 0.85]
    bins = reliability_table(y, p, n_bins=10)
    assert [(b.count, round(b.mean_predicted, 6), b.observed_rate) for b in bins] == [
        (2, 0.15, 0.5),
        (3, 0.85, 1.0),
    ]
    assert bins[0].lower == pytest.approx(0.1) and bins[0].upper == pytest.approx(0.2)
    expected_ece = (2 * 0.35 + 3 * 0.15) / 5
    assert ece(y, p, n_bins=10) == pytest.approx(expected_ece)
    assert mce(y, p, n_bins=10) == pytest.approx(0.35)
    assert bins[0].gap == pytest.approx(0.35)
    for b in bins:
        assert b.ci_low <= b.observed_rate <= b.ci_high


def test_quantile_bins_hold_equal_counts() -> None:
    rng = np.random.default_rng(1)
    p = rng.uniform(0, 1, 1000)
    y = (rng.random(1000) < p).astype(float)
    bins = reliability_table(y, p, n_bins=5, strategy="quantile")
    assert len(bins) == 5
    assert all(190 <= b.count <= 210 for b in bins)
    assert ece(y, p, 5, "quantile") < 0.06
    with pytest.raises(ValueError, match="strategy"):
        reliability_table(y, p, 5, strategy="weird")  # type: ignore[arg-type]


def test_probability_one_lands_in_the_last_bin() -> None:
    bins = reliability_table([1, 0], [1.0, 0.0], n_bins=4)
    assert [b.count for b in bins] == [1, 1]
    assert bins[-1].upper == 1.0 and bins[-1].observed_rate == 1.0


def test_wilson_interval_known_values() -> None:
    lo, hi = wilson_interval(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)
    lo, hi = wilson_interval(0, 20)
    assert lo == 0.0 and hi == pytest.approx(0.1611, abs=1e-4)
    assert all(math.isnan(v) for v in wilson_interval(0, 0))


def test_sharpness_and_empty_inputs() -> None:
    assert sharpness([0.5, 0.5, 0.5]) == 0.0
    assert sharpness([0.2, 0.8]) == pytest.approx(0.3)
    assert math.isnan(brier([], [])) and math.isnan(log_loss([], [])) and math.isnan(ece([], []))
    assert reliability_table([], []) == []
    rep = calibration_report([], [])
    assert rep["n"] == 0 and rep["brier"] is None and rep["bins"] == []


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        brier([1, 0], [0.5])
    with pytest.raises(ValueError, match="lie in"):
        brier([1], [1.5])
    with pytest.raises(ValueError, match="lie in"):
        log_loss([2], [0.5])
    with pytest.raises(ValueError, match="finite"):
        ece([1], [float("nan")])


def test_calibration_report_contents_and_murphy_decomposition() -> None:
    _, y, q = _overconfident(4000, seed=2)
    rep = calibration_report(y, q, n_bins=10)
    for key in (
        "n",
        "base_rate",
        "mean_predicted",
        "brier",
        "log_loss",
        "ece",
        "ece_quantile",
        "mce",
        "sharpness",
        "brier_climatology",
        "brier_skill",
        "reliability",
        "resolution",
        "uncertainty",
        "bins",
    ):
        assert key in rep
    assert rep["n"] == 4000
    # Murphy: Brier ~ reliability - resolution + uncertainty (exact up to within-bin spread of p)
    approx = rep["reliability"] - rep["resolution"] + rep["uncertainty"]
    assert abs(approx - rep["brier"]) < 0.01
    assert rep["ece"] > 0.05  # the over-confident predictor is visibly miscalibrated
    assert rep["sharpness"] > sharpness(sigmoid(0.5 * logit(q)))


# ---------------------------------------------------------------------------------- calibrators


@pytest.mark.parametrize("cls", [IsotonicCalibrator, PlattCalibrator])
def test_calibrators_fix_a_miscalibrated_predictor(cls: type) -> None:
    _, y_fit, q_fit = _overconfident(4000, seed=10)
    _, y_test, q_test = _overconfident(4000, seed=11)
    cal = cls().fit(q_fit, y_fit)
    fixed = cal.transform(q_test)
    before, after = ece(y_test, q_test), ece(y_test, fixed)
    assert before > 0.07
    assert after < before / 2
    assert after < 0.04
    assert log_loss(y_test, fixed) < log_loss(y_test, q_test) - 0.02
    assert brier(y_test, fixed) < brier(y_test, q_test)


def test_platt_recovers_the_true_slope() -> None:
    _, y, q = _overconfident(20000, seed=3)
    cal = PlattCalibrator().fit(q, y)
    assert cal.a == pytest.approx(0.5, abs=0.05)
    assert cal.b == pytest.approx(0.0, abs=0.06)


def test_isotonic_is_monotone_and_clipped() -> None:
    _, y, q = _overconfident(3000, seed=4)
    cal = IsotonicCalibrator(eps=0.01).fit(q, y)
    grid = np.linspace(0, 1, 501)
    out = cal.transform(grid)
    assert np.all(np.diff(out) >= -1e-12)
    assert out.min() >= 0.01 and out.max() <= 0.99
    assert 0.01 <= cal(0.0) <= cal(1.0) <= 0.99


@pytest.mark.parametrize("cls", [IsotonicCalibrator, PlattCalibrator, IdentityCalibrator])
def test_every_calibrator_clips_to_eps(cls: type) -> None:
    cal = cls(eps=0.02).fit([0.0, 0.1, 0.9, 1.0], [0, 0, 1, 1])
    out = cal.transform(np.asarray([0.0, 1e-9, 0.5, 1 - 1e-9, 1.0]))
    assert out.min() >= 0.02 and out.max() <= 0.98


@pytest.mark.parametrize("cls", [IsotonicCalibrator, PlattCalibrator, IdentityCalibrator])
def test_save_load_round_trip(cls: type, tmp_path: Path) -> None:
    _, y, q = _overconfident(1500, seed=5)
    cal = cls().fit(q, y)
    path = cal.save(tmp_path / "cal" / f"{cls.kind}.json")
    probe = np.linspace(0.01, 0.99, 37)
    for loaded in (cls.load(path), load_calibrator(path), calibrator_from_dict(cal.to_dict())):
        assert type(loaded) is cls
        assert np.allclose(loaded.transform(probe), cal.transform(probe))
        assert loaded.n_fit == 1500


def test_degenerate_fits() -> None:
    # all predictions identical -> constant map at the observed rate
    iso = IsotonicCalibrator().fit([0.4, 0.4, 0.4, 0.4], [1, 0, 1, 1])
    assert iso.transform([0.1, 0.9]) == pytest.approx([0.75, 0.75])
    # perfectly separable data stays finite thanks to Platt's smoothed targets
    platt = PlattCalibrator().fit([0.2, 0.3, 0.7, 0.8], [0, 0, 1, 1])
    assert math.isfinite(platt.a) and 0 < platt(0.5) < 1
    with pytest.raises(ValueError, match="zero samples"):
        IsotonicCalibrator().fit([], [])
    with pytest.raises(RuntimeError, match="not fitted"):
        PlattCalibrator().transform([0.5])
    with pytest.raises(ValueError, match="eps"):
        IsotonicCalibrator(eps=0.7)
    with pytest.raises(ValueError, match="unknown calibrator"):
        calibrator_from_dict({"kind": "nope"})


def test_fit_logistic_bounds_and_weights() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(3000, 2))
    y = (rng.random(3000) < sigmoid(1.5 * x[:, 0] - 0.7 * x[:, 1] + 0.2)).astype(float)
    coef, b = fit_logistic(x, y)
    assert coef == pytest.approx([1.5, -0.7], abs=0.15) and b == pytest.approx(0.2, abs=0.12)
    coef_pos, _ = fit_logistic(x, y, lower_bounds=[0.0, 0.0])
    assert coef_pos[1] >= 0.0 and coef_pos[1] < 1e-6
    # duplicating a row is the same as giving it weight 2
    xw, yw = x[:200], y[:200]
    c1, b1 = fit_logistic(np.vstack([xw, xw[:1]]), np.concatenate([yw, yw[:1]]))
    w = np.ones(200)
    w[0] = 2.0
    c2, b2 = fit_logistic(xw, yw, weights=w)
    assert c1 == pytest.approx(c2, abs=1e-4) and b1 == pytest.approx(b2, abs=1e-4)
