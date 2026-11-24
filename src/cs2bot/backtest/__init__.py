"""cs2bot.backtest - leakage-free walk-forward backtests (see docs/dev/reports/ensemble_backtest.md).

* :func:`run_backtest` - the engine: chronological matches, retrain every N days on data strictly
  before the decision time, price every quote on offer, decide, stake, settle, report.
* :func:`run_synthetic_backtest` - the same on a synthetic universe with known truth.
* :func:`run_db_backtest` - the same over the real database (stored Betclic prices).
* :class:`PipelineTrainer` - ratings / ensemble -> veto -> match prediction as a trainer.
* :class:`BacktestReport` - metrics (ROI, CLV, drawdown, Brier, calibration, breakdowns) and
  ``to_markdown()``.
"""

from cs2bot.backtest.db import load_backtest_matches, load_historical_data, run_db_backtest
from cs2bot.backtest.odds_history import DbOddsHistory, InMemoryOddsHistory, OddsHistory
from cs2bot.backtest.pipeline import (
    PipelineConfig,
    PipelinePredictor,
    PipelineTrainer,
    series_match_prediction,
)
from cs2bot.backtest.policy import (
    Decider,
    DecisionContext,
    EngineDecider,
    EnginePricer,
    EngineResolver,
    EngineStaker,
    FlatStaker,
    Pricer,
    Resolver,
    Staker,
    ThresholdDecider,
)
from cs2bot.backtest.report import BacktestReport, BetRecord, BreakdownRow, CurvePoint, max_drawdown
from cs2bot.backtest.runner import BacktestConfig, SimulatedBankroll, run_backtest
from cs2bot.backtest.synthetic import (
    OracleTrainer,
    RandomTrainer,
    run_synthetic_backtest,
    synthetic_backtest_matches,
    synthetic_map_truth,
    synthetic_odds_history,
    synthetic_series_truth,
)
from cs2bot.backtest.types import (
    BacktestMatch,
    HistoricalData,
    LeakageError,
    MatchQuery,
    Predictor,
    ProbabilityTable,
    Trainer,
    TrainingSet,
    assert_no_leakage,
    outcome_key,
)

__all__ = [
    "BacktestConfig",
    "BacktestMatch",
    "BacktestReport",
    "BetRecord",
    "BreakdownRow",
    "CurvePoint",
    "DbOddsHistory",
    "Decider",
    "DecisionContext",
    "EngineDecider",
    "EnginePricer",
    "EngineResolver",
    "EngineStaker",
    "FlatStaker",
    "HistoricalData",
    "InMemoryOddsHistory",
    "LeakageError",
    "MatchQuery",
    "OddsHistory",
    "OracleTrainer",
    "PipelineConfig",
    "PipelinePredictor",
    "PipelineTrainer",
    "Predictor",
    "Pricer",
    "ProbabilityTable",
    "RandomTrainer",
    "Resolver",
    "SimulatedBankroll",
    "Staker",
    "ThresholdDecider",
    "Trainer",
    "TrainingSet",
    "assert_no_leakage",
    "load_backtest_matches",
    "load_historical_data",
    "max_drawdown",
    "outcome_key",
    "run_backtest",
    "run_db_backtest",
    "run_synthetic_backtest",
    "series_match_prediction",
    "synthetic_backtest_matches",
    "synthetic_map_truth",
    "synthetic_odds_history",
    "synthetic_series_truth",
]
