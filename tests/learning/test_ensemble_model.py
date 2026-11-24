"""MapWinEnsemble (cs2bot.models.ensemble): accuracy, uncertainty, persistence, fallback, leakage."""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import MapSequenceProbability
from cs2bot.features import FeatureStore, build_training_rows, features_for_match_map
from cs2bot.features.rows import match_map_features
from cs2bot.models.calibration import log_loss
from cs2bot.models.ensemble import EnsembleConfig, LookAheadError, MapWinEnsemble, mirror_context
from cs2bot.models.ratings import WalkForwardRatings
from tests.learning.ensemble_helpers import FAST_ENSEMBLE, RARE_TEAM, ensemble_universe, fitted_ensemble


def _ctx(match_id: int, t1: int, t2: int, when, fmt: MatchFormat = MatchFormat.BO3) -> MatchContext:
    """A context for a match the store does not hold (ids >= 90 000).

    The id matters: ``features_for_match_map`` cuts every query at the start of ``ctx.match_id``
    when the store holds that match (a backtest over finished matches).
    """
    return MatchContext(match_id=match_id, scheduled_at=when, format=fmt, team1_id=t1, team2_id=t2)


def test_fit_report_and_holdout_quality() -> None:
    fx = fitted_ensemble()
    rep = fx.report
    assert not rep.fallback and not fx.ensemble.is_fallback
    assert rep.n_rows == len(fx.records)
    assert rep.n_holdout_rows > 50 and rep.n_train_rows > 400
    assert rep.train_end is not None and rep.holdout_start is not None and rep.train_end < rep.holdout_start
    assert rep.data_end == max(r.played_at for r in fx.records)
    assert set(rep.holdout) == {"rating", "lgbm", "stacked", "calibrated"}
    assert rep.calibrator in ("platt", "isotonic")
    assert rep.stacker["rating"] >= 0 and rep.stacker["lgbm"] >= 0
    cal = rep.holdout["calibrated"].log_loss
    rating = rep.holdout["rating"].log_loss
    assert cal is not None and rating is not None
    # beats the coin flip clearly, and is not worse than the rating model alone beyond noise
    assert cal < math.log(2) - 0.08
    assert cal <= rating + 0.02
    assert rep.best_iteration >= FAST_ENSEMBLE.min_boost_rounds
    assert rep.model_version.startswith("ens-") and rep.model_version == fx.ensemble.model_version


def test_out_of_time_predictions_beat_baseline_and_track_ratings() -> None:
    """Fit on the first 75% of the history, predict the rest from leakage-safe feature rows."""
    uni = ensemble_universe()
    records = sorted(uni.map_results(), key=lambda r: r.played_at)
    cut = records[int(len(records) * 0.75)].played_at
    train = [r for r in records if r.played_at < cut]
    store = FeatureStore.build(records, vetoes=uni.veto_records())  # queries never read past as_of
    ratings = WalkForwardRatings.fit(records)  # history-aware: answers for any as_of without leakage
    ens = MapWinEnsemble(FAST_ENSEMBLE)
    ens.fit(train, store=store, rating_provider=ratings)
    test = [r for r in records if r.played_at >= cut]
    x, ys, _ = build_training_rows(test, store=store, rating_provider=ratings)  # as of each match start
    out = ens.predict_frame(x)
    p_ens, p_rat = out["p"].to_numpy(), out["rating"].to_numpy()
    assert len(ys) > 150
    assert log_loss(ys, p_ens) < math.log(2) - 0.08
    assert log_loss(ys, p_ens) <= log_loss(ys, p_rat) + 0.03


def test_prediction_components_and_sigma() -> None:
    fx = fitted_ensemble()
    ens, store, ratings = fx.ensemble, fx.store, fx.ratings
    as_of = fx.report.data_end + timedelta(days=1)
    est = ens.predict(_ctx(99_001, 1, 2, as_of), "mirage", as_of=as_of, store=store, rating_provider=ratings)
    assert est.map_name == "mirage" and 0 < est.p_team1_win < 1
    for key in ("rating", "lgbm", "stacked", "calibrated", "sigma_rating", "sigma_model", "fallback"):
        assert key in est.components
    assert est.components["fallback"] == 0.0
    assert est.p_team1_win == pytest.approx(est.components["calibrated"])
    assert est.sigma >= 0.01
    assert est.sigma == pytest.approx(
        max(0.01, math.hypot(est.components["sigma_rating"], est.components["sigma_model"]))
    )
    assert est.components["sigma_model"] > 0  # the bootstrap models disagree a little


def test_sigma_is_larger_for_a_team_with_little_data() -> None:
    fx = fitted_ensemble()
    ens, store, ratings = fx.ensemble, fx.store, fx.ratings
    as_of = fx.report.data_end + timedelta(days=1)
    rare, common = [], []
    for opp in (2, 3, 4, 5, 6):
        for map_name in ("mirage", "inferno", "nuke"):
            kw = {"as_of": as_of, "store": store, "rating_provider": ratings}
            r = ens.predict(_ctx(90_000 + opp, RARE_TEAM, opp, as_of), map_name, **kw)
            c = ens.predict(_ctx(91_000 + opp, 1, opp, as_of), map_name, **kw)
            rare.append(r.sigma)
            common.append(c.sigma)
    assert min(rare) > 0 and min(common) > 0
    assert np.mean(rare) > 1.5 * np.mean(common)


def logit_scalar(p: float) -> float:
    return math.log(p / (1.0 - p))


def test_feature_row_and_context_paths_agree() -> None:
    fx = fitted_ensemble()
    as_of = fx.report.data_end + timedelta(hours=3)
    ctx = _ctx(99_002, 3, 7, as_of)
    row = features_for_match_map(ctx, "nuke", store=fx.store, as_of=as_of, rating_provider=fx.ratings)
    a = fx.ensemble.predict(row, "nuke", as_of=as_of)
    b = fx.ensemble.predict(ctx, "nuke", as_of=as_of, store=fx.store, rating_provider=fx.ratings)
    # final review: a context prediction is averaged with the team-swapped one (orientation-free); its
    # forward half is exactly the raw-row prediction
    assert a.p_team1_win == pytest.approx(b.components["p_forward"])
    mirrored = features_for_match_map(
        mirror_context(ctx), "nuke", store=fx.store, as_of=as_of, rating_provider=fx.ratings
    )
    m = fx.ensemble.predict(mirrored, "nuke", as_of=as_of)
    assert 1.0 - m.p_team1_win == pytest.approx(b.components["p_mirrored"])
    z = (logit_scalar(a.p_team1_win) + logit_scalar(1.0 - m.p_team1_win)) / 2.0
    assert b.p_team1_win == pytest.approx(1.0 / (1.0 + math.exp(-z)))
    assert b.sigma >= min(a.sigma, m.sigma) - 1e-12
    with pytest.raises(ValueError, match="rating"):
        fx.ensemble.predict({k: v for k, v in row.items() if not k.startswith("rating")}, "nuke", as_of=as_of)
    with pytest.raises(ValueError, match="store"):
        fx.ensemble.predict(ctx, "nuke", as_of=as_of, rating_provider=fx.ratings)


def test_look_ahead_is_refused() -> None:
    fx = fitted_ensemble()
    end = fx.report.data_end
    ctx = _ctx(99_003, 1, 2, end)
    for when in (end, end - timedelta(days=30)):
        with pytest.raises(LookAheadError):
            fx.ensemble.predict(ctx, "mirage", as_of=when, store=fx.store, rating_provider=fx.ratings)


def test_fit_with_as_of_only_uses_earlier_records() -> None:
    uni = ensemble_universe()
    records = uni.map_results()
    cut = sorted(r.played_at for r in records)[300]
    store = FeatureStore.build(records)
    ens = MapWinEnsemble(EnsembleConfig(min_rows=50, n_bootstrap=2, max_boost_rounds=60, learning_rate=0.2))
    rep = ens.fit(records, store=store, rating_provider=WalkForwardRatings.fit(records), as_of=cut)
    assert rep.data_end is not None and rep.data_end < cut
    assert rep.n_rows == sum(1 for r in records if r.played_at < cut)


def test_monotone_in_the_rating_features() -> None:
    fx = fitted_ensemble()
    as_of = fx.report.data_end + timedelta(days=1)
    base = match_map_features(
        fx.store, team1_id=4, team2_id=9, map_name="inferno", as_of=as_of, rating_provider=fx.ratings
    )
    rows = []
    for z in np.linspace(-3, 3, 25):
        row = dict(base)
        p = 1 / (1 + math.exp(-z))
        row.update({"rating_logit": z, "rating_p": p, "rating_c_map": p})
        rows.append(row)
    out = fx.ensemble.predict_frame(pd.DataFrame(rows, dtype=float))
    for col in ("lgbm", "stacked", "calibrated"):
        assert np.all(np.diff(out[col].to_numpy()) >= -1e-9), col


def test_feature_importance() -> None:
    imp = fitted_ensemble().ensemble.feature_importance()
    assert imp and sum(imp.values()) == pytest.approx(1.0)
    values = list(imp.values())
    assert values == sorted(values, reverse=True)
    assert set(fitted_ensemble().report.feature_importance) <= set(imp)


def test_save_load_round_trip(tmp_path: Path) -> None:
    fx = fitted_ensemble()
    path = fx.ensemble.save(tmp_path / "models" / "ensemble.json")
    loaded = MapWinEnsemble.load(path)
    assert loaded.model_version == fx.ensemble.model_version
    assert loaded.feature_names == fx.ensemble.feature_names
    assert loaded.data_end == fx.ensemble.data_end
    assert loaded.report is not None and loaded.report.model_dump() == fx.report.model_dump()
    assert loaded.calibrator.kind == fx.ensemble.calibrator.kind
    as_of = fx.report.data_end + timedelta(days=2)
    for t1, t2, m in ((1, 2, "mirage"), (5, RARE_TEAM, "nuke"), (8, 3, "ancient")):
        ctx = _ctx(92_000 + t1, t1, t2, as_of)
        a = fx.ensemble.predict(ctx, m, as_of=as_of, store=fx.store, rating_provider=fx.ratings)
        b = loaded.predict(ctx, m, as_of=as_of, store=fx.store, rating_provider=fx.ratings)
        assert a.p_team1_win == pytest.approx(b.p_team1_win, abs=1e-12)
        assert a.sigma == pytest.approx(b.sigma, abs=1e-12)
    with pytest.raises(ValueError, match="MapWinEnsemble"):
        MapWinEnsemble.from_dict({"kind": "other"})


def test_graceful_fallback_with_too_little_data(tmp_path: Path) -> None:
    uni = ensemble_universe()
    records = sorted(uni.map_results(), key=lambda r: r.played_at)[:40]
    store = FeatureStore.build(records)
    ratings = WalkForwardRatings.fit(records)
    ens = MapWinEnsemble(FAST_ENSEMBLE)
    rep = ens.fit(records, store=store, rating_provider=ratings)
    assert rep.fallback and ens.is_fallback and "min_rows" in (rep.fallback_reason or "")
    as_of = records[-1].played_at + timedelta(hours=1)
    ctx = _ctx(93_000, 1, 2, as_of)
    rating = ratings.predict(1, 2, "mirage", as_of)
    for est in (
        ens.predict(ctx, "mirage", as_of=as_of, store=store, rating_provider=ratings),
        ens.predict(ctx, "mirage", as_of=as_of, rating_provider=ratings),  # no store needed
    ):
        assert est.components["fallback"] == 1.0
        assert est.p_team1_win == pytest.approx(rating.p_team1_win)
        assert est.sigma == pytest.approx(max(rating.sigma, 0.01))
    loaded = MapWinEnsemble.load(ens.save(tmp_path / "fallback.json"))
    assert loaded.is_fallback
    assert loaded.predict(ctx, "mirage", as_of=as_of, rating_provider=ratings).components["fallback"] == 1.0
    with pytest.raises(ValueError, match="rating_provider"):
        ens.predict(ctx, "mirage", as_of=as_of)
    assert ens.feature_importance() == {}


def test_predict_for_sequences_mixes_picker_scenarios() -> None:
    fx = fitted_ensemble()
    as_of = fx.report.data_end + timedelta(days=1)
    ctx = _ctx(99_004, 2, 6, as_of)
    seqs = [
        MapSequenceProbability(
            maps=["mirage", "nuke", "inferno"],
            pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None],
            probability=0.6,
        ),
        MapSequenceProbability(
            maps=["nuke", "mirage", "inferno"],
            pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None],
            probability=0.4,
        ),
    ]
    est = fx.ensemble.predict_for_sequences(
        ctx, seqs, as_of=as_of, store=fx.store, rating_provider=fx.ratings
    )
    assert set(est) == {"mirage", "nuke", "inferno"}
    assert est["inferno"].picked_by is None and "p_picked_by_team1" not in est["inferno"].components
    mix = est["mirage"]
    assert mix.components["p_picked_by_team1"] == pytest.approx(0.6)
    assert mix.components["p_picked_by_team2"] == pytest.approx(0.4)
    kw = {"as_of": as_of, "store": fx.store, "rating_provider": fx.ratings}
    by1 = fx.ensemble.predict(ctx, "mirage", picked_by=TeamSlot.TEAM1, **kw)
    by2 = fx.ensemble.predict(ctx, "mirage", picked_by=TeamSlot.TEAM2, **kw)
    assert mix.p_team1_win == pytest.approx(0.6 * by1.p_team1_win + 0.4 * by2.p_team1_win)
    assert mix.sigma >= min(by1.sigma, by2.sigma) - 1e-12
