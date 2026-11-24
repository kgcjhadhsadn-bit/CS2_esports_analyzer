"""Synthetic end-to-end backtests: oracle vs random, the truth replica, the full pipeline."""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

import numpy as np
import pytest

from cs2bot.backtest import (
    BacktestConfig,
    PipelineConfig,
    PipelineTrainer,
    run_synthetic_backtest,
    synthetic_backtest_matches,
    synthetic_map_truth,
    synthetic_odds_history,
    synthetic_series_truth,
)
from cs2bot.backtest.synthetic import SeriesTruth, played_order
from cs2bot.backtest.types import HistoricalData, MatchQuery
from cs2bot.config import BankrollSettings, DecisionSettings, Settings
from cs2bot.domain.enums import MatchFormat
from cs2bot.domain.predictions import MatchPrediction
from cs2bot.models.ensemble import EnsembleConfig
from cs2bot.testing.synthetic import SynthUniverse, generate_universe

# Lower probability gate than the live default (0.75) so the oracle also bets underdogs it knows
# are mispriced; EV gate, tax, 1/4 Kelly and every cap stay the engine's.
# a value-betting experiment (EV rule on, low probabilities): Kelly staking, not the owner's confidence tiers
BACKTEST_SETTINGS = Settings(
    decision=DecisionSettings(min_probability=0.25, min_ev=0.02, max_sigma=0.2),
    bankroll=BankrollSettings(staking_mode="kelly"),
)
MONTHLY = BacktestConfig(retrain_every=timedelta(days=30))


@lru_cache(maxsize=1)
def big_universe() -> tuple[SynthUniverse, dict[int, SeriesTruth]]:
    uni = generate_universe(n_teams=12, n_matches=400, seed=1, with_kills=False)
    return uni, synthetic_series_truth(uni, n_sims=1000, seed=1)


@lru_cache(maxsize=1)
def small_universe() -> SynthUniverse:
    return generate_universe(n_teams=8, n_matches=120, seed=4, days=200, with_kills=True)


def test_truth_replica_matches_the_generator() -> None:
    uni = generate_universe(n_teams=6, n_matches=10, seed=2, with_kills=False)
    combos = [(1, 2, "mirage"), (3, 5, "nuke"), (6, 4, "ancient")]
    fast = synthetic_map_truth(uni, combos, n_sims=30_000, seed=5)
    for combo in combos:
        exact = uni.true_map_win_prob(*combo, n_sims=6000, seed=9)
        se = np.sqrt(exact * (1 - exact) / 6000) + np.sqrt(fast[combo] * (1 - fast[combo]) / 30_000)
        assert abs(fast[combo] - exact) < 4 * se + 1e-3, combo
    # symmetry: P(b beats a) = 1 - P(a beats b)
    both = synthetic_map_truth(uni, [(1, 2, "mirage"), (2, 1, "mirage")], n_sims=2000)
    assert both[(1, 2, "mirage")] + both[(2, 1, "mirage")] == pytest.approx(1.0)


def test_series_truth_and_odds_history() -> None:
    uni, truth = big_universe()
    m = uni.matches[0]
    t = truth[m.id]
    assert list(t.maps) == played_order(m) and len(t.maps) == m.format.n_maps
    assert t.p_played[0] == 1.0 and 0 < t.p_team1_win < 1
    odds = synthetic_odds_history(uni, truth, seed=3)
    quotes = odds.quotes_before(m.id, m.played_at)
    # match winner + one map-winner market per slot, two selections each, opening and closing
    n_markets = (1 if m.format is not MatchFormat.BO2 else 0) + m.format.n_maps
    assert len(quotes) == 2 * n_markets
    early = odds.quotes_before(m.id, m.played_at - timedelta(hours=1))
    assert {q.captured_at for q in early} == {m.played_at - timedelta(hours=24)}
    closing = odds.closing_quote(m.id, f"{quotes[0].spec.key()}#{quotes[0].selection_key}")
    assert closing is not None and closing.captured_at == m.played_at - timedelta(minutes=10)
    for q in quotes:  # a margin: implied probabilities of a pair sum to more than 1
        other = next(o for o in quotes if o.spec == q.spec and o.selection_key != q.selection_key)
        assert 1 / float(q.odds) + 1 / float(other.odds) > 1.0


def test_oracle_makes_money_and_beats_the_closing_line() -> None:
    uni, truth = big_universe()
    report = run_synthetic_backtest(
        uni,
        model="oracle",
        truth=truth,
        settings=BACKTEST_SETTINGS,
        config=MONTHLY,
        margin=0.05,
        opening_noise_sd=0.10,
        seed=1,
    )
    assert report.n_bets >= 100
    assert report.roi is not None and report.roi > 0.05
    assert report.flat_roi is not None and report.flat_roi > 0.05
    assert report.profit > 0 and report.final_bankroll > report.initial_bankroll
    assert report.avg_clv is not None and report.avg_clv > 0.05
    assert report.positive_clv_share is not None and report.positive_clv_share > 0.6
    assert report.n_matches_predicted == report.n_matches > 0
    assert {r.key for r in report.by_market} == {"match_winner", "map_winner"}
    assert report.n_void > 0  # map 3 of a 2-0 series voids its map-winner bets
    assert report.calibration["ece"] < 0.06  # the oracle is calibrated
    assert report.max_drawdown_pct < 0.5


def test_random_model_loses_about_the_margin_plus_tax() -> None:
    uni, truth = big_universe()
    oracle = run_synthetic_backtest(
        uni,
        model="oracle",
        truth=truth,
        settings=BACKTEST_SETTINGS,
        config=MONTHLY,
        opening_noise_sd=0.10,
        seed=1,
    )
    random_ = run_synthetic_backtest(
        uni,
        model="random",
        truth=truth,
        settings=BACKTEST_SETTINGS,
        config=MONTHLY,
        opening_noise_sd=0.10,
        seed=1,
    )
    assert random_.n_bets >= 100
    assert random_.roi is not None and random_.roi < 0
    assert random_.flat_roi is not None and random_.flat_roi < 0
    assert random_.bankroll_growth is not None and random_.bankroll_growth < 0
    assert random_.brier_all is not None and oracle.brier_all is not None
    assert random_.brier_all > oracle.brier_all + 0.05
    assert oracle.flat_roi is not None and oracle.flat_roi > random_.flat_roi + 0.15


def test_ratings_pipeline_learns_and_never_errors() -> None:
    uni, truth = big_universe()
    report = run_synthetic_backtest(
        uni,
        model="ratings",
        truth=truth,
        settings=BACKTEST_SETTINGS,
        config=MONTHLY,
        opening_noise_sd=0.10,
        seed=1,
    )
    assert report.n_prediction_errors == 0 and report.n_unpriceable == 0
    assert report.n_retrains >= 5
    assert all(v.startswith("bt-") for v in report.model_versions)
    # coin flip 0.25, the oracle about 0.19 on this universe (its maps are often close to 50/50)
    assert report.brier_all is not None and report.brier_all < 0.23
    assert report.calibration["ece"] < 0.08


def test_full_pipeline_with_simulation_runs_per_match() -> None:
    """ratings -> veto -> build_match_prediction -> price every quoted market (Addendum)."""
    uni = small_universe()
    truth = synthetic_series_truth(uni, n_sims=500, seed=2)
    report = run_synthetic_backtest(
        uni,
        model="pipeline",
        truth=truth,
        settings=BACKTEST_SETTINGS,
        pipeline_config=PipelineConfig(simulate=True, n_sims=400, scenario_n_sims=300),
        config=BacktestConfig(retrain_every=timedelta(days=365)),
        warmup_fraction=0.85,
        seed=2,
    )
    assert report.n_prediction_errors == 0 and report.n_unpriceable == 0
    assert 10 <= report.n_matches_predicted <= 25
    assert report.n_retrains == 1 and report.n_priced == report.n_quotes > 0
    # the predictor returns the simulator's MatchPrediction and uses the published veto
    data = HistoricalData(
        map_results=uni.map_results(), vetoes=uni.veto_records(), rounds=uni.round_records()
    )
    match = synthetic_backtest_matches(uni)[-1]
    t = match.start_time - timedelta(hours=1)
    predictor = PipelineTrainer(PipelineConfig(simulate=True, n_sims=400), settings=BACKTEST_SETTINGS)(
        data.until(t)
    )
    assert predictor.trained_through is not None and predictor.trained_through < t
    query = MatchQuery(
        match.match_id,
        match.start_time,
        match.format,
        match.team1_id,
        match.team2_id,
        as_of=t,
        context=match.context,
    )
    pred = predictor.predict(query)
    assert isinstance(pred, MatchPrediction) and pred.veto_known and pred.n_simulations > 0
    assert pred.created_at == t
    assert pred.central.slots[0].map_distribution == {played_order(uni.matches[-1])[0]: 1.0}
    assert pred.central.slots[0].score_pmf  # round-level markets are priceable


def test_pipeline_with_ensemble_and_unknown_veto() -> None:
    uni = small_universe()
    truth = synthetic_series_truth(uni, n_sims=500, seed=2)
    report = run_synthetic_backtest(
        uni,
        model="ensemble",
        truth=truth,
        settings=BACKTEST_SETTINGS,
        pipeline_config=PipelineConfig(
            simulate=False,
            use_ensemble=True,
            ensemble_config=EnsembleConfig(
                min_rows=60, n_bootstrap=2, max_boost_rounds=80, learning_rate=0.15
            ),
        ),
        config=BacktestConfig(retrain_every=timedelta(days=365), veto_known_at_decision=False),
        warmup_fraction=0.8,
        seed=2,
    )
    assert report.n_prediction_errors == 0
    assert report.n_matches_predicted > 10
    assert any("-ens-" in v for v in report.model_versions)
    # without the veto the map-winner markets wait for it (engine rule prefer_known_veto)
    assert report.rejection_reasons.get("waiting_for_veto", 0) > 0
    assert all(b.market_type == "match_winner" for b in report.bets)
    md = report.to_markdown()
    assert "waiting_for_veto" in md
