"""deep_data: the ensemble screens the optional feature groups and reports high-confidence reliability."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from cs2bot.backtest.deep_eval import compare_feature_sets, markdown_summary, synthetic_deep_data
from cs2bot.backtest.synthetic import synthetic_backtest_matches
from cs2bot.models.ensemble import EnsembleConfig, MapWinEnsemble
from cs2bot.testing.synthetic import generate_universe
from tests.learning.ensemble_helpers import FAST_ENSEMBLE, fitted_ensemble

QUICK = EnsembleConfig(
    n_bootstrap=1,
    learning_rate=0.1,
    max_boost_rounds=120,
    early_stopping_rounds=15,
    n_cv_folds=2,
    holdout_days=60,
    min_rows=60,
)


def test_fit_report_has_screening_and_high_confidence_sections() -> None:
    fx = fitted_ensemble()
    rep = fx.report
    assert not rep.fallback
    assert "deep" in rep.feature_groups["groups"]  # map-derived deep columns exist without DeepData
    deep = rep.feature_groups["groups"]["deep"]
    assert deep["n_columns"] > 20 and isinstance(deep["kept"], bool)
    feats = fx.ensemble.feature_names
    assert any(c.startswith("diff_dp_") for c in feats) == deep["kept"]
    assert rep.high_confidence["threshold"] == 0.75 and len(rep.high_confidence["bins"]) == 10
    assert rep.high_confidence["n"] == sum(b["count"] for b in rep.high_confidence["bins"])
    # the persisted report carries both sections
    restored = MapWinEnsemble.from_dict(fx.ensemble.to_dict())
    assert restored.report is not None and restored.report.feature_groups == rep.feature_groups


def test_live_prediction_ignores_columns_the_model_did_not_keep() -> None:
    fx = fitted_ensemble()
    ctx = synthetic_backtest_matches(fx.universe)[-1].context
    assert ctx is not None
    as_of = ctx.scheduled_at + timedelta(days=1)
    est = fx.ensemble.predict(
        ctx, ctx.known_veto[0].map_name, as_of=as_of, store=fx.store, rating_provider=fx.ratings
    )
    assert 0.0 < est.p_team1_win < 1.0


def test_screen_keeps_only_groups_that_improve_out_of_fold_log_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    ens = MapWinEnsemble(QUICK)
    x = pd.DataFrame(
        {
            "rating_p": [0.5] * 4,
            "rating_logit": [0.0] * 4,
            "diff_dp_rest_days": [1.0, 2.0, 3.0, 4.0],
            "stage_final": [0.0, 1.0, 0.0, 0.0],
            "mkt_p_last": [np.nan] * 4,  # no data at all: dropped without a trial
            "sharp_p_last": [np.nan] * 4,
        }
    )
    scores = {frozenset({"rating_p", "rating_logit"}): 0.60}

    def fake(frame: pd.DataFrame, y: Any, meta: Any) -> np.ndarray:
        cols = frozenset(frame.columns)
        loss = scores.get(cols, 0.55 if "diff_dp_rest_days" in cols else 0.70)
        return np.full(len(frame), loss)

    monkeypatch.setattr(ens, "_stacked_oof_losses", fake)
    drop, summary = ens._screen_feature_groups(x, np.zeros(4), pd.DataFrame())
    assert set(drop) == {"mkt_p_last", "sharp_p_last"}
    assert summary["groups"]["deep"]["kept"] and "market" not in summary["groups"]
    assert summary["groups"]["deep"]["gain"] == pytest.approx(0.05) and summary["base_log_loss"] == 0.60
    scores[frozenset({"rating_p", "rating_logit"})] = 0.50  # now the deep block makes it worse
    drop, summary = ens._screen_feature_groups(x, np.zeros(4), pd.DataFrame())
    assert {"diff_dp_rest_days", "stage_final"} <= set(drop) and not summary["groups"]["deep"]["kept"]
    off = MapWinEnsemble(QUICK.model_copy(update={"screen_feature_groups": False}))
    assert off._screen(x, np.zeros(4), pd.DataFrame()) == []


@pytest.mark.slow
def test_compare_feature_sets_on_a_synthetic_universe() -> None:
    uni = generate_universe(n_teams=10, n_matches=260, seed=9, days=240, with_kills=True)
    data = synthetic_deep_data(uni, seed=9, truth_sims=300)
    report = compare_feature_sets(uni, deep=data, ensemble_config=QUICK.model_copy(update={"n_cv_folds": 3}))
    names = [v.name for v in report.variants]
    assert names == ["baseline", "deep", "deep_market"]
    base, deep = report.by_name("baseline"), report.by_name("deep")
    assert base.log_loss is not None and deep.log_loss is not None
    # the screen guarantees the deep variant is the baseline plus only groups that helped out of fold
    if not deep.report or not any(g["kept"] for g in deep.report.feature_groups["groups"].values()):
        assert deep.log_loss == pytest.approx(base.log_loss)
    text = markdown_summary(report)
    assert "| baseline |" in text and "gain share" in text
    assert FAST_ENSEMBLE.screen_feature_groups  # default on


def test_paired_gain_requires_the_gain_to_beat_its_noise() -> None:
    from cs2bot.models.ensemble import _paired_gain

    rng = np.random.default_rng(0)
    base = rng.normal(0.5, 0.2, 400)
    ids = np.repeat(np.arange(200), 2)
    noisy = base - 0.002 + rng.normal(0, 0.2, 400)  # tiny gain, huge noise
    gain, se = _paired_gain(base, noisy, ids)
    assert se > abs(gain)
    clear = base - 0.01 + rng.normal(0, 0.001, 400)
    gain, se = _paired_gain(base, clear, ids)
    assert gain == pytest.approx(0.01, abs=1e-3) and se < 0.001
    nan = np.full(4, np.nan)
    assert all(np.isnan(_paired_gain(nan, nan, np.arange(4))))
