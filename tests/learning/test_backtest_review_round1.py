"""Review round 1 regressions: contained step failures in the walk-forward engine, ensemble fit safety."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from cs2bot.backtest import (
    BacktestMatch,
    FlatStaker,
    HistoricalData,
    InMemoryOddsHistory,
    LeakageError,
    MatchQuery,
    ProbabilityTable,
    ThresholdDecider,
    TrainingSet,
    run_backtest,
)
from cs2bot.config import Settings
from cs2bot.domain.betting import BankrollState, BetDecision, PricedOutcome
from cs2bot.domain.markets import MarketSpec
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import MapWinEstimate
from cs2bot.domain.results import MatchActuals, SettlementOutcome
from cs2bot.features import FeatureStore
from cs2bot.models.ensemble import EnsembleConfig, MapWinEnsemble
from cs2bot.models.ratings import WalkForwardRatings
from tests.learning.ensemble_helpers import ensemble_universe
from tests.learning.test_backtest_engine import MW, T0, TablePredictor, _match, _op, _quote

World = tuple[HistoricalData, list[BacktestMatch], InMemoryOddsHistory]


@pytest.fixture
def world() -> World:
    """Two BO3s (team1 wins match 1 2-0, loses match 2 1-2), team1 match-winner priced at 2.00."""
    m1, r1 = _match(1, T0, [True, True])
    m2, r2 = _match(2, T0 + timedelta(days=1), [True, False, False])
    history = InMemoryOddsHistory()
    for m in (m1, m2):
        history.set_kickoff(m.match_id, m.start_time)
        history.add(m.match_id, _quote(MW, "team1", "2.00", m.start_time - timedelta(hours=20)))
    return HistoricalData(map_results=r1 + r2, matches=[m1, m2]), [m1, m2], history


def _table_trainer(training: TrainingSet) -> TablePredictor:
    return TablePredictor(dict([_op(MW, "team1", 0.7)]))


def _run(world: World, settings: Settings, **kw: Any) -> Any:
    data, _, history = world
    kw.setdefault("trainer", _table_trainer)
    kw.setdefault("decider", ThresholdDecider())
    kw.setdefault("staker", FlatStaker(Decimal("10")))
    return run_backtest(data, odds=history, settings=settings, **kw)


# ------------------------------------------------------------------ contained step failures


def test_failing_resolver_settles_as_unknown_refund(world: World, settings: Settings) -> None:
    def resolver(spec: MarketSpec, selection: str, actuals: MatchActuals) -> SettlementOutcome:
        raise RuntimeError("corrupt round history")

    report = _run(world, settings, resolver=resolver)
    assert report.n_bets == 2 and report.n_unknown == 2
    assert report.n_step_errors == 2
    assert report.profit == Decimal("0.00")  # unknown = full refund
    assert any("steps failed" in w for w in report.warnings)


def test_failing_pricer_decider_and_staker_do_not_abort(world: World, settings: Settings) -> None:
    def pricer(prediction: Any, quote: OddsQuote) -> Any:
        raise TypeError("bad prediction object")

    def decider(priced: PricedOutcome, ctx: Any) -> BetDecision:
        raise RuntimeError("decision bug")

    def staker(decision: BetDecision, state: BankrollState) -> Decimal:
        raise ZeroDivisionError("staking bug")

    r1 = _run(world, settings, pricer=pricer)
    assert r1.n_priced == 0 and r1.n_unpriceable == 2 and r1.n_step_errors == 2
    r2 = _run(world, settings, decider=decider)
    assert r2.n_accepted == 0 and r2.rejection_reasons == {"decision_error": 2}
    r3 = _run(world, settings, staker=staker)
    assert r3.n_accepted == 2 and r3.n_bets == 0 and r3.rejection_reasons == {"staking_error": 2}
    assert r3.flat_n == 2  # the decisions still count for the flat (1-unit) metrics


def test_stake_is_capped_at_the_free_balance(world: World, settings: Settings) -> None:
    def greedy(decision: BetDecision, state: BankrollState) -> Decimal:
        return state.balance * 10

    report = _run(world, settings, staker=greedy)
    assert report.n_bets == 2
    first = report.bets[0]
    assert first.stake == report.initial_bankroll  # everything, never more
    assert report.final_bankroll >= 0


def test_failed_training_skips_then_keeps_the_previous_model(world: World, settings: Settings) -> None:
    calls: list[datetime] = []

    def flaky(training: TrainingSet) -> TablePredictor:
        calls.append(training.cutoff)
        if len(calls) != 2:
            raise RuntimeError("LightGBM exploded")
        return _table_trainer(training)

    from cs2bot.backtest import BacktestConfig

    data, (m1, m2), history = world
    m3, r3 = _match(3, T0 + timedelta(days=2), [False, False])
    history.set_kickoff(3, m3.start_time)
    history.add(3, _quote(MW, "team1", "2.00", m3.start_time - timedelta(hours=20)))
    data = HistoricalData(map_results=[*data.map_results, *r3], matches=[m1, m2, m3])
    report = run_backtest(
        data,
        trainer=flaky,
        odds=history,
        settings=settings,
        config=BacktestConfig(retrain_every=timedelta(hours=1)),
        decider=ThresholdDecider(),
        staker=FlatStaker(Decimal("10")),
    )
    # match 1: no model yet -> skipped; match 2: trained; match 3: retrain fails -> previous model
    assert len(calls) == 3
    assert report.n_training_errors == 2 and report.n_matches_skipped == 1
    assert report.n_retrains == 1 and report.n_matches_predicted == 2
    assert report.n_bets == 2
    assert any("previous model was kept" in w for w in report.warnings)


def test_leakage_raised_by_a_trainer_or_an_update_propagates(world: World, settings: Settings) -> None:
    def cheat(training: TrainingSet) -> TablePredictor:
        raise LeakageError("peeked at the result")

    with pytest.raises(LeakageError):
        _run(world, settings, trainer=cheat)

    class Peeking(TablePredictor):
        def observe(self, training: TrainingSet) -> None:
            raise LeakageError("online update read the future")

    with pytest.raises(LeakageError):
        _run(world, settings, trainer=lambda training: Peeking(dict([_op(MW, "team1", 0.7)])))


def test_failing_online_update_keeps_the_model(world: World, settings: Settings) -> None:
    class Brittle(TablePredictor):
        def observe(self, training: TrainingSet) -> None:
            raise RuntimeError("update failed")

    report = _run(world, settings, trainer=lambda training: Brittle(dict([_op(MW, "team1", 0.7)])))
    assert report.n_training_errors == 1 and report.n_matches_predicted == 2 and report.n_bets == 2


def test_unpriceable_counts_are_unchanged_for_query_objects(world: World, settings: Settings) -> None:
    """A prediction the pricer does not understand is unpriceable, not a step error."""

    class Weird:
        def predict(self, query: MatchQuery) -> object:
            return object()

    report = _run(world, settings, trainer=lambda training: Weird())
    assert report.n_unpriceable == 2 and report.n_step_errors == 0
    table = ProbabilityTable(match_id=1, as_of=T0, outcomes={})
    assert table.get(MW, "team1") is None


# ------------------------------------------------------------------ ensemble fit safety


def _small_records(n: int = 600) -> list[Any]:
    return sorted(ensemble_universe().map_results(), key=lambda r: r.played_at)[:n]


_TINY = EnsembleConfig(
    min_rows=100,
    n_bootstrap=0,
    max_boost_rounds=40,
    early_stopping_rounds=10,
    learning_rate=0.2,
    n_cv_folds=2,
    holdout_days=60,
    sigma_floor=0.01,
)


def test_training_failure_falls_back_to_the_ratings(monkeypatch: pytest.MonkeyPatch) -> None:
    records = _small_records()
    store = FeatureStore.build(records)
    ratings = WalkForwardRatings.fit(records)
    ens = MapWinEnsemble(_TINY)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("lightgbm: bad data")

    monkeypatch.setattr(ens, "_fit_core", boom)
    report = ens.fit(records, store=store, rating_provider=ratings)
    assert report.fallback and ens.is_fallback
    assert "training failed" in (report.fallback_reason or "")
    as_of = records[-1].played_at + timedelta(hours=1)
    est = ens.predict({"rating_p": 0.6, "rating_sigma": 0.03}, "mirage", as_of=as_of)
    assert est.components["fallback"] == 1.0 and est.p_team1_win == pytest.approx(0.6)


def test_fit_is_atomic_when_the_rows_cannot_be_built() -> None:
    records = _small_records(60)
    store = FeatureStore.build(records)
    ratings = WalkForwardRatings.fit(records)
    ens = MapWinEnsemble(_TINY)
    first = ens.fit(records, store=store, rating_provider=ratings)
    assert first.fallback  # 60 maps < min_rows

    class Refusing:
        def predict(self, *args: Any, **kwargs: Any) -> MapWinEstimate:
            raise ValueError("as_of is not after the last processed game")

    more = _small_records(200)
    with pytest.raises(ValueError, match="last processed"):
        ens.fit(more, store=FeatureStore.build(more), rating_provider=Refusing())
    assert ens.report is first and ens.data_end == first.data_end
    assert ens.model_version == first.model_version


def test_fit_report_warnings_are_not_duplicated() -> None:
    records = _small_records()
    store = FeatureStore.build(records)
    ratings = WalkForwardRatings.fit(records)
    cfg = _TINY.model_copy(update={"min_oof_rows": 10**6})  # the stacker always keeps its priors
    report = MapWinEnsemble(cfg).fit(records, store=store, rating_provider=ratings)
    assert not report.fallback and report.refit_on_all
    stacker = [w for w in report.warnings if "out-of-fold rows" in w]
    assert len(stacker) == 2  # one of the pre-holdout model (prefixed), one of the final model
    assert sum(w.startswith("pre-holdout model:") for w in stacker) == 1
    assert len(report.warnings) == len(set(report.warnings))


# ------------------------------------------------------------------ database loading


def test_contexts_are_only_loaded_for_the_predicted_matches(session: Any, settings: Settings) -> None:
    from cs2bot.backtest import load_backtest_matches
    from cs2bot.testing.synthetic import generate_universe, populate_db

    uni = generate_universe(n_teams=6, n_matches=30, seed=8, days=60, with_kills=False)
    populate_db(session, uni, with_rounds=False, with_kills=False)
    session.flush()
    cut = uni.matches[20].played_at
    matches = load_backtest_matches(session, settings=settings, context_since=cut)
    assert len(matches) == 30
    assert all((m.context is not None) == (m.start_time >= cut) for m in matches)


# ------------------------------------------------------------------ online rating updates


def test_out_of_order_results_are_replayed_exactly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """observe() with overlapping matches gives exactly the ratings of a refit, without refitting."""
    from cs2bot.backtest import PipelineConfig, PipelineTrainer
    from cs2bot.backtest import pipeline as pipeline_module

    uni = ensemble_universe()
    records = sorted(uni.map_results(), key=lambda r: (r.played_at, r.match_id, r.map_number))
    base, later = records[:300], records[300:340]

    def training(recs: list[Any]) -> TrainingSet:
        return TrainingSet(
            cutoff=max(r.played_at for r in recs) + timedelta(hours=2), map_results=tuple(recs)
        )

    predictor = PipelineTrainer(PipelineConfig(simulate=False), settings=settings)(training(base))
    refits: list[int] = []
    original_fit = pipeline_module.WalkForwardRatings.fit

    def counting_fit(recs: Any, config: Any = None) -> WalkForwardRatings:
        refits.append(1)
        return original_fit(recs, config)

    monkeypatch.setattr(pipeline_module.WalkForwardRatings, "fit", counting_fit)
    predictor.observe(training(base + later[20:]))  # in order: incremental updates
    model_before = predictor.ratings.model
    predictor.observe(training(base + later))  # later[:20] arrive late: replayed from the retrain state
    assert predictor.ratings.model is not model_before and refits == []  # no refit from scratch
    refit = original_fit(base + later)
    as_of = later[-1].played_at + timedelta(hours=3)
    for t1, t2, m in ((1, 2, "mirage"), (3, 9, "nuke"), (12, 5, "inferno"), (7, 15, "ancient")):
        a = predictor.ratings.predict(t1, t2, m, as_of)
        b = refit.predict(t1, t2, m, as_of)
        assert a.p_team1_win == pytest.approx(b.p_team1_win, abs=1e-12)
        assert a.sigma == pytest.approx(b.sigma, abs=1e-12)
    mid = later[25].played_at  # the history-aware answers for past dates agree too
    a = predictor.ratings.predict(1, 2, "mirage", mid)
    assert a.p_team1_win == pytest.approx(refit.predict(1, 2, "mirage", mid).p_team1_win, abs=1e-12)
    assert predictor.trained_through == later[-1].played_at

    # a late result played before the retrain's latest game forces a full refit - still exact
    late = base[-1].model_copy(update={"match_id": 10**6, "map_number": 1})
    late = late.model_copy(update={"played_at": base[-1].played_at - timedelta(minutes=5)})
    predictor.observe(training([*base, *later, late]))
    assert refits == [1]
    full = original_fit([*base, *later, late])
    a, b = predictor.ratings.predict(1, 2, "mirage", as_of), full.predict(1, 2, "mirage", as_of)
    assert a.p_team1_win == pytest.approx(b.p_team1_win, abs=1e-12)
