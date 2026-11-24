# ensemble_backtest: implementation report

Tests: `.venv/bin/python -m pytest tests/learning -q -n 2` - 132 passed, 2 skipped (PostgreSQL tests of the predecessor, need `CS2BOT_TEST_DATABASE_URL`), 0 failed, about 60 s; this package's 61 tests (`test_ensemble_calibration.py`, `test_ensemble_model.py`, `test_backtest_engine.py`, `test_backtest_synthetic.py`, `test_backtest_db.py`) take about 40 s with `-n 2`. `tests/foundation`: 37 passed. `ruff check`, `ruff format --check` and `mypy` are clean on `src/cs2bot/models/ensemble.py`, `src/cs2bot/models/calibration.py`, `src/cs2bot/backtest/` and `tests/learning`.

## Summary

Nothing of this package existed when the work started (only an empty `backtest/__init__.py`). Built from scratch:

- **`cs2bot.models.calibration`**: isotonic, Platt and identity calibrators (fit / transform / save / load as JSON, output clipped to `[eps, 1 - eps]`). Metrics: `brier`, `log_loss`, `ece` (uniform or quantile bins), `mce`, `reliability_table` (bins with 95% Wilson intervals), `sharpness`, and `calibration_report`, which also gives Brier skill and the Murphy decomposition. In the tests, a predictor that doubles the true logit has ECE about 0.10; after either calibrator it is below 0.04, and Platt recovers the true slope 0.5.
- **`cs2bot.models.ensemble.MapWinEnsemble`** combines the rating provider with LightGBM:
  - LightGBM is trained on the `cs2bot.features` rows (mirrored), with a monotone-increasing constraint on `rating_logit`, `rating_p` and `rating_c_map`, early stopping on a chronological split, and small-data regularisation (depth 3, 7 leaves, L2 10, 63 bins, row and feature subsampling).
  - A logistic stacker with non-negative weights combines the two, fitted on out-of-fold predictions from expanding-window time-series CV.
  - The stacked output is calibrated on the latest `calibration_holdout_days` window: isotonic from 300 holdout maps, Platt below that, identity below 30. The core is then refitted on all rows and the calibrator kept.
  - sigma = sqrt(sigma_rating_part^2 + sigma_model^2), floored at `models.sigma_floor`. sigma_rating_part is the rating model's `logit_sd` pushed through the stacker weight; sigma_model is the spread of a 5-model match-bootstrap LightGBM ensemble.
  - With too little data it falls back to the rating provider (`components["fallback"] = 1`).
  - Results on a 500-match synthetic universe (holdout of 171 maps): log-loss 0.479 for the ratings alone, 0.469 for LightGBM, 0.471 stacked. A team with 2 matches of history gets more than 1.5 times the sigma of an established team.
- **`cs2bot.backtest`** is the walk-forward engine. For every match, in kick-off order:
  1. Bets whose result is known by the decision time `t = kick-off - decision_lead` are settled.
  2. The model is retrained every `retrain_every` on `HistoricalData.until(t)`, i.e. only results already known at `t`. A match's results become visible one `result_delay` after its last map started. Between retrains, `observe` keeps the ratings current.
  3. The match is predicted, every quote on offer at `t` is priced and decided, the stake is sized on a simulated bankroll, and bets are settled when the result is known.
  - The defaults are the real engine: `engine.pricing.price_outcome`, `engine.decision.evaluate`, `engine.bankroll.size_stake`, `engine.settlement.resolve_outcome`, and the Polish tax maths of `engine.ev` (12% stake tax, 10% big-win tax, refunds). All of them are injectable.
  - Leakage is checked in code:
    - `TrainingSet` refuses any record at or after its cutoff.
    - The engine refuses a predictor whose `trained_through` is at or after `t`, and a prediction stamped (`as_of` / `created_at`) after `t`.
    - `LookAheadError` from the ensemble propagates; it is never swallowed.
  - The Addendum's full pipeline is available: `PipelineTrainer` runs ratings or the ensemble, then the veto distribution, then `build_match_prediction`, and prices every quoted market. `series_match_prediction` is an exact, fast alternative for series-level markets.
  - `run_synthetic_backtest` sanity-checks everything on a synthetic universe with known truth. The truth comes from a vectorised replica of the generator, checked against `SynthUniverse.true_map_win_prob`. On 6 seeds (12 teams, 400 matches, margin 5%, opening noise 0.10):
    - oracle: ROI +8% to +41%, average CLV +46% to +56%;
    - random model: ROI -5% to -49%.
  - `run_db_backtest` runs the same over the real database, using the stored Betclic snapshots (`DbOddsHistory`) and `datasets.load_match_actuals`.

## Public API

Nothing is re-exported from `cs2bot.models.__init__` (shared file). Import `cs2bot.models.ensemble` and `cs2bot.models.calibration` directly. `cs2bot.backtest` re-exports everything listed below.

### `cs2bot.models.calibration`

```python
DEFAULT_EPS = 1e-4
class IsotonicCalibrator(*, eps: float = 1e-4)                    # kind = "isotonic"
class PlattCalibrator(*, eps: float = 1e-4, ridge: float = 1e-3, smooth_targets: bool = True)   # kind = "platt"; .a, .b
class IdentityCalibrator(*, eps: float = 1e-4)                    # kind = "identity"
    .fit(p: ArrayLike, y: ArrayLike, sample_weight: ArrayLike | None = None) -> Self
    .transform(p: ArrayLike) -> np.ndarray                       # clipped to [eps, 1 - eps]
    .__call__(p: float) -> float
    .save(path) -> Path ; Cls.load(path) ; .to_dict() ; Cls.from_dict(d) ; .n_fit ; .fitted
AnyCalibrator = IdentityCalibrator | IsotonicCalibrator | PlattCalibrator
calibrator_from_dict(data: Mapping) -> AnyCalibrator ; load_calibrator(path) -> AnyCalibrator
brier(y, p) -> float ; log_loss(y, p, *, eps=1e-15) -> float ; sharpness(p) -> float      # nan for empty input
ece(y, p, n_bins=10, strategy: "uniform" | "quantile" = "uniform") -> float ; mce(y, p, n_bins=10, strategy="uniform") -> float
reliability_table(y, p, n_bins=10, *, strategy="uniform") -> list[ReliabilityBin]   # empty bins omitted
class ReliabilityBin(BaseModel): lower, upper, count, mean_predicted, observed_rate, ci_low, ci_high; .gap
wilson_interval(successes, n, *, z=1.96) -> tuple[float, float]
calibration_report(y, p, n_bins=10) -> dict   # n, base_rate, mean_predicted, brier, log_loss, ece, ece_quantile, mce,
                                              # sharpness, brier_climatology, brier_skill, reliability, resolution,
                                              # uncertainty, bins (list of ReliabilityBin dicts); None where undefined
fit_logistic(x, y, *, weights=None, ridge=0.0, center=None, lower_bounds=None) -> tuple[np.ndarray, float]
logit(p, eps=1e-6) ; sigmoid(x)
```
`y` is 0/1 (or in [0, 1]), `p` the probability of `y == 1`. Mismatched lengths, non-finite values or values outside [0, 1] raise `ValueError`.

### `cs2bot.models.ensemble`

```python
class EnsembleConfig(BaseModel)   # frozen; main knobs: mirror=True, min_rows=150, n_cv_folds=4, holdout_days=None
                                  # (-> settings.models.calibration_holdout_days), max_holdout_fraction=0.3,
                                  # min_holdout_rows=30, min_isotonic_rows=300, refit_on_all=True, learning_rate=0.05,
                                  # max_boost_rounds=500, early_stopping_rounds=40, num_leaves=7, max_depth=3,
                                  # min_data_in_leaf=30, lambda_l2=10, max_bin=63, monotone_features=("rating_logit",
                                  # "rating_p", "rating_c_map"), drop_features=(), n_bootstrap=5,
                                  # sigma_floor=None (-> settings.models.sigma_floor), num_threads=1, seed=20231
class LookAheadError(ValueError)
class HoldoutMetrics(BaseModel): n, log_loss, brier, ece
class FitReport(BaseModel): model_version, fitted_at, fallback, fallback_reason, n_rows, n_train_rows, n_holdout_rows,
    n_features, train_start, train_end, holdout_start, holdout_end, data_end, best_iteration, n_cv_folds,
    stacker {"rating","lgbm","intercept"}, calibrator, holdout {"rating","lgbm","stacked","calibrated"} -> HoldoutMetrics,
    log_loss_gain_vs_rating, feature_importance (top 25), refit_on_all, warnings

class MapWinEnsemble:
    def __init__(self, config: EnsembleConfig | None = None, *, settings: Settings | ModelSettings | None = None)
    def fit(self, map_results: Iterable[MapResultRecord], *, store: FeatureStore, rating_provider: RatingProvider,
            as_of: datetime | None = None) -> FitReport
    def predict(self, features_or_ctx: Mapping[str, float] | MatchContext, map_name: str, *, as_of: datetime,
                store: FeatureStore | None = None, rating_provider: RatingProvider | None = None,
                picked_by: TeamSlot | Literal["auto"] | None = "auto") -> MapWinEstimate
    def predict_for_sequences(self, ctx: MatchContext, sequences: Sequence[MapSequenceProbability], *, as_of: datetime,
                              store: FeatureStore | None = None, rating_provider: RatingProvider | None = None
                              ) -> dict[str, MapWinEstimate]          # the map_estimates of build_match_prediction
    def predict_frame(self, frame: pd.DataFrame) -> pd.DataFrame     # batch: rating, lgbm, stacked, calibrated, p,
                                                                     # sigma_rating, sigma_model, sigma
    def feature_importance(self, *, importance_type: "gain" | "split" = "gain") -> dict[str, float]  # shares, sorted
    def save(self, path) -> Path ; MapWinEnsemble.load(path) ; .to_dict() ; MapWinEnsemble.from_dict(d)
    # attributes / properties: report, model_version, data_end, is_fallback, feature_names, calibrator,
    #                          holdout_days, sigma_floor, config
```
- `fit` builds the rows with `features.build_training_rows`. `rating_provider` must answer for past dates: `WalkForwardRatings`, or `MapRatingModel` fitted with `record_history=True`.
- `predict`:
  - Raises `LookAheadError` when `as_of <= data_end` (the latest map trained on).
  - With a `MatchContext` it builds the row through `features_for_match_map`. Give an upcoming match an id the store does not hold: for a stored match every feature is cut at its start.
  - `components` keys: `rating`, `lgbm`, `stacked`, `calibrated`, `sigma_rating`, `sigma_model`, `fallback` (0 / 1).
  - In fallback mode it returns the provider's p and sigma (floored); it needs no store, only `rating_provider`.

Wiring (nightly training, live prediction):

```python
from cs2bot.features import FeatureStore
from cs2bot.models.ensemble import MapWinEnsemble
from cs2bot.models.ratings import WalkForwardRatings

store = FeatureStore.from_session(session, until=now)
ratings = WalkForwardRatings.fit(store.map_results)
ens = MapWinEnsemble(settings=settings)
report = ens.fit(store.map_results, store=store, rating_provider=ratings)      # store report.model_dump() in model_runs
ens.save(settings.models.artifacts_dir / f"{ens.model_version}.json")
# per match (ctx from datasets.load_match_context, sequences from VetoModel.predict_sequences)
map_estimates = ens.predict_for_sequences(ctx, sequences, as_of=now, store=store, rating_provider=ratings)
prediction = build_match_prediction(..., veto_distribution=sequences, map_estimates=map_estimates, ...)
```

### `cs2bot.backtest` - data contracts (`types.py`)

```python
class LeakageError(RuntimeError)
outcome_key(spec: MarketSpec, selection: str) -> str                  # "<spec key>#<selection>"
assert_no_leakage(records: Iterable, cutoff: datetime, *, what="training data") -> None   # played_at < cutoff
@dataclass(frozen=True) class BacktestMatch(match_id, start_time, format, team1_id, team2_id, actuals: MatchActuals | None = None,
                                            context: MatchContext | None = None, tier=Tier.UNRANKED, is_lan=False,
                                            end_time: datetime | None = None)
@dataclass(frozen=True) class MatchQuery(match_id, start_time, format, team1_id, team2_id, as_of, context=None,
                                         tier=Tier.UNRANKED, is_lan=False); .to_context() -> MatchContext
@dataclass(frozen=True) class TrainingSet(cutoff, map_results=(), vetoes=(), rounds=(), player_maps=(), matches=())
    # constructor raises LeakageError for any record at/after cutoff; .latest_time
@dataclass class HistoricalData(map_results=(), vetoes=(), rounds=(), player_maps=(), matches=(), result_delay=timedelta(hours=1))
    .until(cutoff) -> TrainingSet ; .visible_at(record) -> datetime ; .match_end(match) -> datetime
class Predictor(Protocol): def predict(self, query: MatchQuery) -> Any   # optional: trained_through: datetime | None,
                                                                         #           observe(training: TrainingSet) -> None
class Trainer(Protocol): def __call__(self, training: TrainingSet) -> Predictor
@dataclass(frozen=True) class ProbabilityTable(match_id, as_of, outcomes: Mapping[str, OutcomeProbability],
                                               model_version="table", data_completeness=1.0, veto_known=True); .get(spec, selection)
```

### `cs2bot.backtest` - odds (`odds_history.py`)

```python
class OddsHistory(Protocol):
    def quotes_before(self, match_id: int, t: datetime) -> list[OddsQuote]     # latest AVAILABLE price per outcome, strictly before t
    def closing_quote(self, match_id: int, key: str) -> OddsQuote | None       # last available price strictly before kick-off
class InMemoryOddsHistory(kickoffs: dict[int, datetime] | None = None): .add(match_id, quote) ; .add_many(...) ;
                                                                        .set_kickoff(match_id, t) ; .match_ids()
class DbOddsHistory(session): ... ; .kickoff(match_id) -> datetime | None
```
- `DbOddsHistory` reads `odds_snapshots` of mapped markets and selections of events linked `AUTO` or `CONFIRMED`.
- An unavailable snapshot means no price.
- Kick-off is the earlier of `matches.scheduled_at` and the event's `starts_at`.
- When two linked events quote the same outcome, the first one is used.

### `cs2bot.backtest` - steps (`policy.py`)

```python
Pricer = Callable[[Any, OddsQuote], OutcomeProbability]
Decider = Callable[[PricedOutcome, DecisionContext], BetDecision]
Staker = Callable[[BetDecision, BankrollState], Decimal]
Resolver = Callable[[MarketSpec, str, MatchActuals], SettlementOutcome]
@dataclass(frozen=True) class DecisionContext(match: MatchQuery, now, veto_known: bool, data_completeness: float, already_bet_keys=frozenset())
class EnginePricer(*, sigma_floor=0.01, betclic=None)        # MatchPrediction -> engine.pricing.price_outcome; ProbabilityTable /
                                                             # Mapping -> lookup; MapWinEstimate -> map 1 / BO1 match winner
class EngineDecider(settings: DecisionSettings, betclic: BetclicSettings, *, max_quote_age_s: float | None = None)
                                                             # engine.decision.evaluate; link CONFIRMED (1.0), status not checked
class ThresholdDecider(*, min_probability=0.0, min_ev=0.0)
class EngineStaker(settings: BankrollSettings, betclic: BetclicSettings)   # engine.bankroll.size_stake
class FlatStaker(stake=Decimal("10"))
class EngineResolver(betclic: BetclicSettings | None = None) # engine.settlement.resolve_outcome
```

### `cs2bot.backtest` - engine and report (`runner.py`, `report.py`)

```python
class BacktestConfig(BaseModel):   # frozen
    decision_lead=timedelta(hours=1); retrain_every=timedelta(days=7); max_quote_age=timedelta(hours=48)
    veto_known_at_decision=True; data_completeness=1.0; start=None; end=None; min_training_maps=0
    initial_bankroll: Decimal | None = None (-> settings.bankroll.starting_bankroll); n_calibration_bins=10
    keep_unplaced_decisions=True; max_report_bets=None; extra={}

def run_backtest(data: HistoricalData, *, trainer: Trainer, odds: OddsHistory, config: BacktestConfig | None = None,
                 settings: Settings | None = None, pricer: Pricer | None = None, decider: Decider | None = None,
                 staker: Staker | None = None, resolver: Resolver | None = None,
                 matches: Sequence[BacktestMatch] | None = None,
                 progress: Callable[[int, int], None] | None = None) -> BacktestReport

class SimulatedBankroll(initial: Decimal, tax: engine.ev.TaxModel, timezone="Europe/Warsaw")   # .state(now) -> BankrollState
class BacktestReport(BaseModel):
    created_at, start, end, model_versions, settings_summary,
    n_matches, n_matches_predicted, n_matches_skipped, n_retrains, n_prediction_errors, n_quotes, n_priced, n_unpriceable,
    n_accepted, n_bets, n_won, n_lost, n_void, n_push, n_unknown, hit_rate,
    initial_bankroll, final_bankroll, total_staked, total_returned, profit, roi, bankroll_growth, max_drawdown, max_drawdown_pct,
    flat_n, flat_profit, flat_roi, n_clv, avg_clv, positive_clv_share, avg_clv_flat,
    n_predictions, brier_all, log_loss_all, n_bet_predictions, brier_bets, log_loss_bets, calibration (calibration_report dict),
    profit_curve: list[CurvePoint(time, bankroll, profit)], by_market / by_month: list[BreakdownRow], rejection_reasons: dict[str, int],
    bets: list[BetRecord], warnings
    def to_markdown(self, *, max_bets: int = 20) -> str
class BetRecord(BaseModel): match_id, decided_at, settled_at, market_type, spec_key, selection, odds, closing_odds, clv, p_win, p_settled,
                            sigma, ev, stake, outcome ("won"/"lost"/"void"/"push"/"unknown"), payout, pnl, flat_pnl, model_version; .placed
class BreakdownRow(BaseModel): key, n_bets, n_won, n_lost, n_refunded, staked, profit, roi, hit_rate, avg_odds, avg_clv, flat_n, flat_profit,
                               flat_roi, n_predictions, brier
max_drawdown(values: Sequence[Decimal]) -> tuple[Decimal, float]
```
Definitions:
- ROI = profit / total staked.
- Flat metrics bet 1 unit on every decision the decider accepted, before staking caps.
- CLV = bet odds / closing odds - 1.
- Brier and log-loss use `p(win | settled)` against won or lost, over all priced outcomes (`*_all`) or over the bets placed (`*_bets`).
- `rejection_reasons` counts the engine reason codes (`low_ev`, `low_probability`, `waiting_for_veto`, ...) plus `staking` (accepted, but the staker gave 0).

### `cs2bot.backtest` - full pipeline (`pipeline.py`)

```python
class PipelineConfig(BaseModel):   # frozen
    simulate=True; n_sims=2000; scenario_n_sims=None; use_ensemble=False; ensemble_config=None; rating_config=None
    veto_config=None; veto_top_k=50; player_props=False; online_ratings=True; seed=20231; model_version_prefix="bt"
class PipelineTrainer(config: PipelineConfig | None = None, *, settings: Settings | None = None)   # Trainer
class PipelinePredictor:   # returned by the trainer
    trained_through, model_version, ratings (WalkForwardRatings), veto, round_model, kill_model, ensemble, store
    def predict(self, query: MatchQuery) -> MatchPrediction
    def observe(self, training: TrainingSet) -> None
    def veto_distribution(self, query) -> tuple[list[MapSequenceProbability], bool]   # (sequences, veto_known)
    def map_estimates(self, query, sequences) -> dict[str, MapWinEstimate]
def series_match_prediction(*, match_id, fmt, team1_id, team2_id, veto_distribution, map_estimates, model_version, created_at,
                            veto_known=False, data_completeness=0.0, logit_shift=0.0) -> MatchPrediction
```
- **Trainer:** fits `WalkForwardRatings`, the `VetoModel` (with the ratings' map advantage), the `RoundModel` when simulating, the `PlayerKillModel` with `player_props`, and the ensemble on a `FeatureStore` built from the training set with `use_ensemble`. Context flags and LLM shifts are not part of the backtest.
- **Predictor:**
  - The published veto steps in the query's context condition the veto distribution.
  - The map pool comes from the context, else from `features.meta.detect_active_map_pool`, else from settings.
  - The predictor calls `build_match_prediction(seed=stable_seed(config.seed, match_id), created_at=as_of, as_of=as_of)`.
- **`series_match_prediction`:** an exact series calculation (no round simulation). Round-level markets are unpriceable with it (`UnpriceableMarket`, counted as `n_unpriceable`).

### `cs2bot.backtest` - synthetic and database runs

```python
def run_synthetic_backtest(universe: SynthUniverse, *, model: "oracle" | "random" | "ratings" | "pipeline" | "ensemble" | Trainer = "oracle",
    markets=("match_winner", "map_winner"), margin=0.06, opening_noise_sd=0.06, closing_noise_sd=0.02, config=None, settings=None,
    pipeline_config=None, truth_sims=2000, truth: dict[int, SeriesTruth] | None = None, seed=0, warmup_fraction=0.3,
    pricer=None, decider=None, staker=None, resolver=None, matches=None) -> BacktestReport
synthetic_map_truth(universe, combos: Iterable[tuple[int, int, str]], *, n_sims=4000, seed=0) -> dict[(t1, t2, map), float]
synthetic_series_truth(universe, *, n_sims=4000, seed=0) -> dict[int, SeriesTruth]   # maps, p_maps, p_played, p_team1_win, p_draw
synthetic_backtest_matches(universe) -> list[BacktestMatch]     # actuals (round winners, kills) + context (lineups, full veto)
synthetic_odds_history(universe, truth, *, markets=..., margin=0.06, opening_noise_sd=0.06, closing_noise_sd=0.02,
                       opening_lead=24h, closing_lead=10min, seed=0) -> InMemoryOddsHistory
class OracleTrainer(truth, formats, *, markets=..., sigma=0.0) ; class RandomTrainer(truth, formats, *, markets=..., seed=0)

def load_backtest_matches(session, *, settings, since=None, until=None, in_scope_only=True, with_context=True) -> list[BacktestMatch]
def load_historical_data(session, *, matches, since=None, until=None, in_scope_only=True, with_rounds=True,
                         with_player_maps=False, result_delay=timedelta(hours=1)) -> HistoricalData
def run_db_backtest(session, *, settings, since, until, history_since=None, config=None, pipeline_config=None, trainer=None,
                    odds=None, in_scope_only=True, progress=None) -> BacktestReport
```
`load_backtest_matches` keeps FINISHED matches whose `load_match_actuals(...).complete` is true. Their context comes from `load_match_context(now=kick-off, include_context_flags=False)`. `run_db_backtest` trains from `history_since`, default `settings.scope.backfill_start`.

Wiring for the CLI (`cs2bot backtest --from --to`, P12) and the owner's sanity check:

```python
from cs2bot.backtest import run_db_backtest, run_synthetic_backtest, PipelineConfig, BacktestConfig
report = run_db_backtest(session, settings=settings, since=date_from, until=date_to,
                         config=BacktestConfig(retrain_every=timedelta(days=7)),
                         pipeline_config=PipelineConfig(simulate=True, n_sims=2000, use_ensemble=True))
print(report.to_markdown()); store report.model_dump(mode="json") in model_runs.metrics (kind="backtest")

from cs2bot.testing.synthetic import generate_universe
report = run_synthetic_backtest(generate_universe(n_teams=12, n_matches=400, seed=1), model="oracle")
```

## Known limitations

- **No real data was used.** All numbers come from synthetic universes. Real Tier 1/2 data will be smaller and noisier; re-check the ensemble's holdout gain over the ratings and tune `EnsembleConfig` (`n_cv_folds`, `min_rows`, regularisation). On synthetic data the stacked model beats the ratings alone by about 0.005-0.01 log-loss. The calibrated numbers are sometimes slightly worse than the stacked ones: Platt or isotonic on fewer than 200 holdout maps adds noise.
- **The calibrator is fitted on the holdout before the final refit** on all rows (`refit_on_all=True`). That is standard practice but assumes the refitted model is miscalibrated the same way. Set `refit_on_all=False` to keep the exact model the calibrator saw.
- **sigma uses the delta method:** logit-space spreads mapped at the calibrated p. The calibration map's own slope and its estimation error are ignored. The rating part depends on the provider exposing `logit_sd` (MapRatingModel does); otherwise `sigma / (p (1 - p))`. Teams without any history get a large sigma (about 0.5), so the decision rule (`max_sigma` 0.05) never bets them.
- **LightGBM speed:** about 5-8 s per ensemble fit on 1,000 maps (mirrored, about 250 features, one thread). A backtest with `use_ensemble=True` and weekly retrains over a year costs a few minutes; the full simulation (`simulate=True`) costs about 0.3 s per match at `n_sims=2000`.
- **Backtest assumptions:**
  - The decider assumes a CONFIRMED link, the configured data completeness (default 1.0) and no context veto. The backtest measures model, pricing and staking, not the data gates or the LLM layer.
  - A quote is "on offer" for `max_quote_age` (48 h) after its last snapshot.
  - The veto published in the context is used at decision time by default. The DB context holds the final veto: set `veto_known_at_decision=False` to price as if the veto were unknown.
  - The loss-limit pause is not simulated as a manual pause. The engine's loss guards still block stakes until the period rolls over.
  - Bets that settlement answers `UNKNOWN` (missing round history, for example) are refunded in the backtest and reported in `n_unknown`.
- **Detecting a cheating trainer relies on `trained_through`.** The engine can only pass a trainer past data (blocked) and verify what the trainer reports (detected). A trainer that secretly reads future data through a closure and reports nothing cannot be detected. All built-in trainers report honestly.
- **The synthetic truth** (`synthetic_map_truth`) replicates `cs2bot.testing.synthetic._play_map`. `test_truth_replica_matches_the_generator` catches drift if the generator changes. It is exact only for universes generated with `drift_sd_per_30d = 0`.
- **`DbOddsHistory`** issues one query per selection and time point. That is fine for hundreds of matches and slow for tens of thousands.

## Foundation change requests

- `src/cs2bot/models/__init__.py` (shared): optionally re-export `MapWinEnsemble`, `EnsembleConfig`, `FitReport`, `LookAheadError` and the calibrators. Not required; consumers import the submodules.
- `docs/ARCHITECTURE.md`, P7 section: replace the ensemble, calibration and backtest bullets with the signatures above. Specifically: `MapWinEnsemble.fit(map_results, *, store, rating_provider, as_of=None) -> FitReport`, `predict_for_sequences`, `run_backtest` / `run_db_backtest` / `run_synthetic_backtest`, `PipelineTrainer`, `OddsHistory`, `LeakageError`.
- `src/cs2bot/config.py` `ModelSettings` (optional): `ensemble_min_rows: int = 150` and `backtest_retrain_days: int = 7`, so the owner can change them without code. Both live in `EnsembleConfig` / `BacktestConfig` today.

## New dependency requests

None. LightGBM, scikit-learn, SciPy and pandas were already dependencies.
