"""Map-win ensemble: rating model + LightGBM, stacked, isotonic-calibrated, with uncertainty.

Pipeline (:meth:`MapWinEnsemble.fit`):

1. **Rows.** :func:`cs2bot.features.build_training_rows` turns every finished map into a
   leakage-safe feature row computed as of the start of its match (the match itself excluded), with
   the rating provider's features (``rating_p``, ``rating_logit``, ``rating_sigma``, ...). With
   ``mirror=True`` every map is added a second time with the teams swapped (label flipped), which
   doubles a small data set and removes any team1/team2 order bias. Both copies of a map always
   land in the same fold (folds are made of whole matches).
2. **Holdout.** The latest ``holdout_days`` (default ``settings.models.calibration_holdout_days``)
   of matches are held out; if that window holds more than ``max_holdout_fraction`` of the matches,
   the latest ``max_holdout_fraction`` of matches are used instead.
3. **Core model on the earlier part** (:class:`_Core`):

   * expanding-window time-series CV: the matches are cut into ``n_cv_folds + 1`` chronological
     blocks; fold ``k`` trains LightGBM on blocks ``0..k-1`` and predicts block ``k``, which gives
     out-of-fold (OOF) predictions for blocks ``1..K``;
   * every LightGBM model uses early stopping on a chronological validation split (the latest
     ``validation_fraction`` of its training matches), a monotone-increasing constraint on the
     rating features (``rating_logit`` and friends, see ``EnsembleConfig.monotone_features``) and
     small-data regularisation (shallow trees, large leaves, L2, row/feature subsampling);
   * the **stacker** is a logistic regression ``sigmoid(w_r * rating_logit + w_l * logit(p_lgbm) + b)``
     fitted on the OOF rows with non-negative weights;
   * the final LightGBM is refitted on all rows of the part with the early-stopped number of
     rounds, plus ``n_bootstrap`` models on match-level bootstrap resamples (for ``sigma_model``).

4. **Calibration.** The core trained before the holdout predicts the holdout; an
   :class:`~cs2bot.models.calibration.IsotonicCalibrator` is fitted on those stacked predictions
   (Platt below ``min_isotonic_rows`` holdout maps, identity below ``min_holdout_rows``). The
   :class:`FitReport` compares the holdout log-loss / Brier / ECE of the rating model alone, LightGBM
   alone, the stacked and the calibrated probabilities (the calibrated numbers are cross-fitted
   over the holdout, so they are honest out-of-sample estimates).
5. With ``refit_on_all`` (default) the core is refitted on every row, holdout included, and the
   calibrator is kept - the most recent results are the most informative ones.

Uncertainty (all in logit space, mapped to probability units at the calibrated ``p`` with the delta
method ``dp = p (1 - p) dz``):

* ``sigma_rating_part = p (1 - p) * w_r * sd_rating_logit``, where ``sd_rating_logit`` is the rating
  model's ``logit_sd`` component (or ``sigma / (p (1 - p))`` when the provider has no such component):
  the rating model's own uncertainty, propagated through the stacker weight;
* ``sigma_model = p (1 - p) * std_b(z_b)`` where ``z_b`` is the stacked logit with the LightGBM
  probability of bootstrap model ``b``;
* ``sigma = max(sigma_floor, sqrt(sigma_rating_part^2 + sigma_model^2))``.

Teams with little data get a large Glicko deviation, hence a large ``sd_rating_logit``, and feature
values the bootstrap models disagree on - both inflate sigma.

Orientation (final review, Lens B): mirrored training rows do not make a LightGBM model antisymmetric -
trees split on ``t1_x`` and ``t2_x`` separately, so ``f(A vs B)`` and ``1 - f(B vs A)`` can differ by
several points for an even match (measured: 0.44 vs 0.39 on a synthetic 50/50 map), and which team is
"team1" is an accident of the source (PandaScore's opponent order). With ``EnsembleConfig.symmetric``
(default) :meth:`MapWinEnsemble.predict` therefore also predicts the same map from the mirrored
:class:`~cs2bot.domain.match.MatchContext` (teams, lineups, veto slots and start sides swapped) and
averages the two calibrated answers in logit space: ``z = (logit(p_ab) + logit(1 - p_ba)) / 2``. The
result is exactly orientation-free; half the gap between the two answers is added (in quadrature) to
``sigma_model``, since it is model uncertainty. Raw feature rows (a mapping instead of a context) are
predicted as given.

Graceful degradation: with fewer than ``min_rows`` labelled maps (or without rating features) the
ensemble does not train LightGBM; :meth:`MapWinEnsemble.predict` then returns the rating provider's
own estimate with ``components["fallback"] = 1.0``.

Persistence: :meth:`MapWinEnsemble.save` writes one JSON file (LightGBM boosters as their text
dump, the stacker, the calibrator, the feature list, the training window, the fit report). No
pickle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike
from pydantic import BaseModel, ConfigDict, Field

from cs2bot.config import ModelSettings, Settings, get_settings
from cs2bot.domain.enums import TeamSlot
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import MapSequenceProbability, MapWinEstimate
from cs2bot.domain.records import MapResultRecord
from cs2bot.features.rows import (
    RATING_LOGIT_FEATURE,
    RatingProvider,
    build_training_rows,
    features_for_match_map,
    rating_estimate,
)
from cs2bot.models.calibration import (
    AnyCalibrator,
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    brier,
    calibrator_from_dict,
    ece,
    fit_logistic,
    log_loss,
    logit,
    sigmoid,
)
from cs2bot.models.confidence import high_confidence_report
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc, utcnow

if TYPE_CHECKING:
    import lightgbm as lgb

    from cs2bot.features.store import FeatureStore

logger = logging.getLogger(__name__)

FORMAT_VERSION: Final[int] = 1
_P_CLIP: Final[float] = 1e-4
_MAX_RATING_SD_LOGIT: Final[float] = 4.0
_RATING_SD_COMPONENT: Final[str] = "rating_c_logit_sd"

__all__ = [
    "EnsembleConfig",
    "FitReport",
    "HoldoutMetrics",
    "LookAheadError",
    "MapWinEnsemble",
    "mirror_context",
]


class LookAheadError(ValueError):
    """A prediction was requested as of a time the training data had already passed."""


class EnsembleConfig(BaseModel):
    """Hyper-parameters of :class:`MapWinEnsemble` (defaults are tuned for small data sets)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # data / folds
    mirror: bool = True
    min_rows: int = Field(default=150, ge=10)  # labelled (non-mirrored) maps needed to train LightGBM
    n_cv_folds: int = Field(default=4, ge=1)
    min_oof_rows: int = 40  # below: the stacker keeps its prior weights
    holdout_days: int | None = None  # None -> settings.models.calibration_holdout_days
    max_holdout_fraction: float = Field(default=0.3, gt=0.0, lt=1.0)
    min_holdout_rows: int = 30  # below: identity calibration
    min_isotonic_rows: int = 300  # below: Platt calibration (isotonic is noisy on few maps)
    calibration_cv_folds: int = 5
    refit_on_all: bool = True
    # LightGBM
    validation_fraction: float = Field(default=0.2, gt=0.0, lt=0.9)
    early_stopping_rounds: int = 40
    max_boost_rounds: int = 500
    min_boost_rounds: int = 10
    learning_rate: float = 0.05
    max_bin: int = 63  # coarse histograms: faster and a mild regulariser on small data
    num_leaves: int = 7
    max_depth: int = 3
    min_data_in_leaf: int = 30
    feature_fraction: float = 0.7
    bagging_fraction: float = 0.8
    bagging_freq: int = 1
    lambda_l1: float = 0.0
    lambda_l2: float = 10.0
    min_gain_to_split: float = 0.0
    monotone_features: tuple[str, ...] = (RATING_LOGIT_FEATURE, "rating_p", "rating_c_map")
    drop_features: tuple[str, ...] = ()
    # deep_data: optional feature groups (name, column prefixes), each kept only when it improves the
    # stacked out-of-fold log-loss of the pre-holdout data by at least ``screen_min_gain``
    screen_feature_groups: bool = True
    feature_groups: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("deep", ("diff_dp_", "t1_dp_", "t2_dp_", "dp_", "stage_", "stakes_level")),
        ("market", ("mkt_", "sharp_")),
    )
    screen_min_gain: float = 0.0005
    screen_z: float = 1.0  # the gain must also exceed this many standard errors (paired, per match)
    num_threads: int = 1
    seed: int = 20231
    # stacker / uncertainty
    stacker_ridge: float = 1e-3
    prior_stack_weights: tuple[float, float] = (0.5, 0.5)  # (rating, lgbm) when too few OOF rows
    n_bootstrap: int = Field(default=5, ge=0)
    sigma_floor: float | None = None  # None -> settings.models.sigma_floor
    calibration_eps: float = 1e-3
    # deep_data review: pseudo-wins and pseudo-losses added to the two END plateaus of the isotonic map
    # (rule of succession). Isotonic regression gives a top block of maps that were all won the value
    # 1 - eps (0.999) whatever its size - a confident wrong probability waiting to happen. With 1.0 a
    # top block of 30 won maps calibrates to 31/32 = 0.969. 0 = plain isotonic.
    calibration_end_prior: float = Field(default=1.0, ge=0.0)
    # average every context prediction with the team-swapped one (orientation-free p, see module docstring)
    symmetric: bool = True


class HoldoutMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    log_loss: float | None
    brier: float | None
    ece: float | None


class FitReport(BaseModel):
    """What :meth:`MapWinEnsemble.fit` did, and how well each stage did on the time-ordered holdout."""

    model_version: str
    fitted_at: datetime
    fallback: bool
    fallback_reason: str | None = None
    n_rows: int  # labelled maps (one orientation)
    n_train_rows: int = 0
    n_holdout_rows: int = 0
    n_features: int = 0
    train_start: datetime | None = None
    train_end: datetime | None = None
    holdout_start: datetime | None = None
    holdout_end: datetime | None = None
    data_end: datetime | None = None  # latest map used (predictions must be strictly after it)
    best_iteration: int = 0
    n_cv_folds: int = 0
    stacker: dict[str, float] = Field(default_factory=dict)  # rating, lgbm, intercept
    calibrator: str = "identity"
    holdout: dict[str, HoldoutMetrics] = Field(default_factory=dict)  # rating, lgbm, stacked, calibrated
    log_loss_gain_vs_rating: float | None = None  # rating log-loss minus calibrated log-loss (> 0 = better)
    feature_importance: dict[str, float] = Field(default_factory=dict)  # top features (gain share)
    refit_on_all: bool = False
    warnings: list[str] = Field(default_factory=list)
    # deep_data: reliability of the cross-fitted calibrated holdout in [0.75, 1] (2.5-pp bins, both
    # teams' sides folded in; see cs2bot.models.confidence.high_confidence_report)
    high_confidence: dict[str, Any] = Field(default_factory=dict)
    # deep_data: the optional feature groups screened on the pre-holdout data (kept or dropped)
    feature_groups: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------------------------
# internal core (one fitted LightGBM + stacker + bootstrap set)
# ---------------------------------------------------------------------------------------------


@dataclass
class _Core:
    features: list[str]
    booster: lgb.Booster
    bootstrap: list[lgb.Booster]
    stack_coef: tuple[float, float]
    stack_intercept: float
    best_iteration: int
    n_cv_folds: int
    n_oof: int
    warnings: list[str] = field(default_factory=list)


def _lgb() -> Any:
    import lightgbm

    return lightgbm


def _match_blocks(meta: pd.DataFrame, n_blocks: int) -> np.ndarray:
    """Block index per row: whole matches, chronological, ``n_blocks`` nearly equal blocks."""
    order = (
        meta[["match_id", "as_of"]]
        .drop_duplicates("match_id")
        .sort_values(["as_of", "match_id"], kind="mergesort")["match_id"]
        .to_numpy()
    )
    n_blocks = max(1, min(n_blocks, len(order)))
    block_of: dict[Any, int] = {}
    for b, chunk in enumerate(np.array_split(order, n_blocks)):
        for mid in chunk:
            block_of[mid] = b
    return np.asarray([block_of[m] for m in meta["match_id"].to_numpy()], dtype=int)


def _chronological_split(meta: pd.DataFrame, fraction: float) -> tuple[np.ndarray, np.ndarray]:
    """Boolean masks (fit, validation): the latest ``fraction`` of matches are the validation set."""
    order = (
        meta[["match_id", "as_of"]]
        .drop_duplicates("match_id")
        .sort_values(["as_of", "match_id"], kind="mergesort")["match_id"]
        .to_numpy()
    )
    n_val = max(1, round(len(order) * fraction)) if len(order) > 1 else 0
    val_ids = set(order[len(order) - n_val :].tolist()) if n_val else set()
    is_val = meta["match_id"].isin(val_ids).to_numpy()
    return ~is_val, is_val


class MapWinEnsemble:
    """LightGBM + rating stacking with isotonic calibration and a bootstrap uncertainty estimate."""

    def __init__(
        self, config: EnsembleConfig | None = None, *, settings: Settings | ModelSettings | None = None
    ):
        self.config = config or EnsembleConfig()
        models = settings.models if isinstance(settings, Settings) else settings
        self._model_settings: ModelSettings = models or get_settings().models
        self._core: _Core | None = None
        self._calibrator: AnyCalibrator = IdentityCalibrator(eps=self.config.calibration_eps).fit([0.5], [1])
        self.report: FitReport | None = None
        self._high_confidence: dict[str, Any] = {}
        self._group_screen: dict[str, Any] = {}
        self.model_version = "ensemble-unfitted"
        self.data_end: datetime | None = None

    # ------------------------------------------------------------------ properties
    @property
    def holdout_days(self) -> int:
        return (
            self.config.holdout_days
            if self.config.holdout_days is not None
            else self._model_settings.calibration_holdout_days
        )

    @property
    def sigma_floor(self) -> float:
        return (
            self.config.sigma_floor
            if self.config.sigma_floor is not None
            else self._model_settings.sigma_floor
        )

    @property
    def is_fallback(self) -> bool:
        """True when predictions come from the rating provider alone (not fitted / too little data)."""
        return self._core is None

    @property
    def feature_names(self) -> list[str]:
        return list(self._core.features) if self._core is not None else []

    @property
    def calibrator(self) -> AnyCalibrator:
        return self._calibrator

    # ------------------------------------------------------------------ LightGBM helpers
    def _params(self, n_rows: int, features: Sequence[str], seed: int) -> dict[str, Any]:
        cfg = self.config
        mono = set(cfg.monotone_features)
        return {
            "objective": "binary",
            "metric": "binary_logloss",
            "learning_rate": cfg.learning_rate,
            "num_leaves": cfg.num_leaves,
            "max_depth": cfg.max_depth,
            "min_data_in_leaf": int(min(cfg.min_data_in_leaf, max(5, n_rows // 20))),
            "feature_fraction": cfg.feature_fraction,
            "bagging_fraction": cfg.bagging_fraction,
            "bagging_freq": cfg.bagging_freq,
            "lambda_l1": cfg.lambda_l1,
            "lambda_l2": cfg.lambda_l2,
            "min_gain_to_split": cfg.min_gain_to_split,
            "monotone_constraints": [1 if f in mono else 0 for f in features],
            "monotone_constraints_method": "advanced",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": cfg.num_threads,
            "max_bin": cfg.max_bin,
        }

    def _train_early_stopped(
        self, x: np.ndarray, y: np.ndarray, meta: pd.DataFrame, features: list[str], seed: int
    ) -> tuple[lgb.Booster, int]:
        """Train with early stopping on the chronologically latest matches; returns (booster, rounds)."""
        lgbm = _lgb()
        cfg = self.config
        fit_mask, val_mask = _chronological_split(meta, cfg.validation_fraction)
        params = self._params(int(fit_mask.sum()), features, seed)
        if val_mask.sum() == 0 or fit_mask.sum() == 0 or len(np.unique(y[fit_mask])) < 2:
            rounds = cfg.min_boost_rounds
            booster = lgbm.train(
                params, lgbm.Dataset(x, label=y, feature_name=features), num_boost_round=rounds
            )
            return booster, rounds
        dtrain = lgbm.Dataset(x[fit_mask], label=y[fit_mask], feature_name=features)
        dval = lgbm.Dataset(x[val_mask], label=y[val_mask], feature_name=features, reference=dtrain)
        booster = lgbm.train(
            params,
            dtrain,
            num_boost_round=cfg.max_boost_rounds,
            valid_sets=[dval],
            callbacks=[lgbm.early_stopping(cfg.early_stopping_rounds, verbose=False)],
        )
        rounds = max(cfg.min_boost_rounds, int(booster.best_iteration or cfg.min_boost_rounds))
        return booster, rounds

    def _train_fixed(
        self, x: np.ndarray, y: np.ndarray, features: list[str], rounds: int, seed: int
    ) -> lgb.Booster:
        lgbm = _lgb()
        params = self._params(len(y), features, seed)
        return lgbm.train(params, lgbm.Dataset(x, label=y, feature_name=features), num_boost_round=rounds)

    @staticmethod
    def _rating_logit(frame: pd.DataFrame) -> np.ndarray:
        if RATING_LOGIT_FEATURE in frame.columns:
            z = frame[RATING_LOGIT_FEATURE].to_numpy(dtype=float)
        else:
            z = logit(frame["rating_p"].to_numpy(dtype=float), _P_CLIP)
        return np.asarray(np.nan_to_num(z, nan=0.0), dtype=float)

    def _oof_predictions(
        self, x: np.ndarray, y: np.ndarray, meta: pd.DataFrame, features: list[str]
    ) -> tuple[np.ndarray, int]:
        """Expanding-window CV -> out-of-fold LightGBM predictions (NaN for the first block)."""
        cfg = self.config
        n_matches = int(meta["match_id"].nunique())
        n_folds = max(1, min(cfg.n_cv_folds, n_matches // 10))
        blocks = _match_blocks(meta, n_folds + 1)
        oof = np.full(len(y), np.nan)
        for k in range(1, n_folds + 1):
            train_mask = blocks < k
            test_mask = blocks == k
            if train_mask.sum() < 20 or test_mask.sum() == 0 or len(np.unique(y[train_mask])) < 2:
                continue
            booster, _ = self._train_early_stopped(
                x[train_mask], y[train_mask], meta.loc[train_mask], features, cfg.seed + k
            )
            oof[test_mask] = booster.predict(x[test_mask], num_iteration=booster.best_iteration or None)
        return oof, n_folds

    def _stacked_oof_losses(self, x_frame: pd.DataFrame, y: np.ndarray, meta: pd.DataFrame) -> np.ndarray:
        """Per-row log-loss of the stacked (rating + LightGBM) out-of-fold predictions of a feature set.

        NaN for rows without an out-of-fold prediction (first CV block) and for mirrored rows; all NaN
        when there are too few rows to fit the stacker. The rows with a value depend only on the CV
        blocks, so two feature sets of the same frame are compared on the same rows.
        """
        out = np.full(len(y), np.nan)
        features = list(x_frame.columns)
        oof, _ = self._oof_predictions(x_frame.to_numpy(dtype=float), y, meta, features)
        have = np.isfinite(oof) & ~meta["mirrored"].to_numpy()
        if int(have.sum()) < self.config.min_oof_rows or len(np.unique(y[have])) < 2:
            return out
        rating_z = self._rating_logit(x_frame)
        design = np.column_stack([rating_z[have], logit(oof[have], _P_CLIP)])
        c, b = fit_logistic(design, y[have], ridge=self.config.stacker_ridge, lower_bounds=[0.0, 0.0])
        p = np.clip(sigmoid(design @ np.asarray(c, dtype=float) + float(b)), 1e-15, 1 - 1e-15)
        yh = y[have]
        out[have] = -(yh * np.log(p) + (1.0 - yh) * np.log(1.0 - p))
        return out

    def _screen_feature_groups(
        self, x_frame: pd.DataFrame, y: np.ndarray, meta: pd.DataFrame
    ) -> tuple[list[str], dict[str, Any]]:
        """Greedy forward selection of optional feature groups on out-of-fold log-loss.

        The classic features are always kept. Each optional group (``EnsembleConfig.feature_groups``:
        the deep_data block and the market block) is added only when it lowers the stacked
        out-of-fold log-loss of the current set by at least ``screen_min_gain`` **and** by at least
        ``screen_z`` standard errors of the paired per-match loss difference (a gain inside the noise
        is not trusted). Runs on the rows it is given (the pre-holdout part), so the holdout metrics
        stay honest. Returns the columns to drop and a summary for the fit report.
        """
        cfg = self.config
        cols = [c for c in x_frame.columns if c not in set(cfg.drop_features)]
        groups: dict[str, list[str]] = {}
        assigned: set[str] = set()
        for name, prefixes in cfg.feature_groups:
            members = [c for c in cols if c.startswith(tuple(prefixes)) and c not in assigned]
            assigned.update(members)
            # a constant column (all NaN, or 0 counts such as mkt_n_moves without any market data)
            # cannot change a tree model: a group of only such columns is not worth a CV pass
            with_data = [c for c in members if x_frame[c].nunique(dropna=True) > 1]
            if with_data:
                groups[name] = with_data
        summary: dict[str, Any] = {"groups": {}, "base_log_loss": None}
        if not groups:
            return sorted(assigned), summary
        current = [c for c in cols if c not in assigned]
        match_ids = meta["match_id"].to_numpy() if "match_id" in meta else np.arange(len(y))
        best = self._stacked_oof_losses(x_frame[current], y, meta)
        summary["base_log_loss"] = _nanmean(best)
        for name, members in groups.items():
            trial = self._stacked_oof_losses(x_frame[current + members], y, meta)
            gain, se = _paired_gain(best, trial, match_ids)
            keep = bool(math.isfinite(gain) and gain >= cfg.screen_min_gain and gain >= cfg.screen_z * se)
            summary["groups"][name] = {
                "n_columns": len(members),
                "oof_log_loss": _nanmean(trial),
                "gain": None if math.isnan(gain) else gain,
                "gain_se": None if math.isnan(se) else se,
                "kept": keep,
            }
            if keep:
                current += members
                best = trial
        dropped = [c for c in cols if c not in set(current)]
        return dropped, summary

    def _fit_core(
        self, x_frame: pd.DataFrame, y: np.ndarray, meta: pd.DataFrame, *, drop: Sequence[str] = ()
    ) -> _Core:
        cfg = self.config
        excluded = set(cfg.drop_features) | set(drop)
        features = [c for c in x_frame.columns if c not in excluded]
        x = x_frame[features].to_numpy(dtype=float)
        warnings: list[str] = []

        # expanding-window CV -> out-of-fold LightGBM predictions
        oof, n_folds = self._oof_predictions(x, y, meta, features)

        # stacker on the OOF rows
        have = np.isfinite(oof)
        rating_z = self._rating_logit(x_frame)
        coef = tuple(float(v) for v in cfg.prior_stack_weights)
        intercept = 0.0
        if int(have.sum()) >= cfg.min_oof_rows and len(np.unique(y[have])) == 2:
            design = np.column_stack([rating_z[have], logit(oof[have], _P_CLIP)])
            c, b = fit_logistic(design, y[have], ridge=cfg.stacker_ridge, lower_bounds=[0.0, 0.0])
            coef, intercept = (float(c[0]), float(c[1])), float(b)
        else:
            warnings.append(f"only {int(have.sum())} out-of-fold rows: stacker uses its prior weights")

        # final model + bootstrap ensemble
        _, rounds = self._train_early_stopped(x, y, meta, features, cfg.seed)
        booster = self._train_fixed(x, y, features, rounds, cfg.seed)
        rng = np.random.default_rng(cfg.seed)
        match_ids = meta["match_id"].to_numpy()
        unique_ids = np.unique(match_ids)
        rows_of: dict[Any, np.ndarray] = {m: np.flatnonzero(match_ids == m) for m in unique_ids}
        boot: list[lgb.Booster] = []
        for b_idx in range(cfg.n_bootstrap):
            chosen = rng.choice(unique_ids, size=unique_ids.size, replace=True)
            idx = np.concatenate([rows_of[m] for m in chosen])
            if len(np.unique(y[idx])) < 2:
                continue
            boot.append(self._train_fixed(x[idx], y[idx], features, rounds, cfg.seed + 1000 + b_idx))
        return _Core(
            features=features,
            booster=booster,
            bootstrap=boot,
            stack_coef=(coef[0], coef[1]),
            stack_intercept=intercept,
            best_iteration=rounds,
            n_cv_folds=n_folds,
            n_oof=int(have.sum()),
            warnings=warnings,
        )

    # ------------------------------------------------------------------ prediction core
    def _core_outputs(self, core: _Core, frame: pd.DataFrame) -> dict[str, np.ndarray]:
        """Rating / LightGBM / stacked probabilities and the logit-space uncertainty parts."""
        x = frame.reindex(columns=core.features).to_numpy(dtype=float)
        rating_p = np.clip(
            np.nan_to_num(frame["rating_p"].to_numpy(dtype=float), nan=0.5), _P_CLIP, 1 - _P_CLIP
        )
        rating_z = self._rating_logit(frame)
        p_lgbm = np.clip(core.booster.predict(x), _P_CLIP, 1 - _P_CLIP)
        w_r, w_l = core.stack_coef
        z = w_r * rating_z + w_l * logit(p_lgbm, _P_CLIP) + core.stack_intercept
        if core.bootstrap:
            zb = np.vstack(
                [
                    w_r * rating_z
                    + w_l * logit(np.clip(b.predict(x), _P_CLIP, 1 - _P_CLIP))
                    + core.stack_intercept
                    for b in core.bootstrap
                ]
            )
            sd_model = zb.std(axis=0, ddof=1) if zb.shape[0] > 1 else np.zeros(len(z))
        else:
            sd_model = np.zeros(len(z))
        sd_rating = self._rating_sd_logit(frame, rating_p)
        return {
            "rating": rating_p,
            "lgbm": p_lgbm,
            "stacked": sigmoid(z),
            "sd_model_logit": sd_model,
            "sd_rating_logit": w_r * sd_rating,
        }

    @staticmethod
    def _rating_sd_logit(frame: pd.DataFrame, rating_p: np.ndarray) -> np.ndarray:
        if _RATING_SD_COMPONENT in frame.columns:
            sd = frame[_RATING_SD_COMPONENT].to_numpy(dtype=float)
        else:
            sd = np.full(len(frame), np.nan)
        if "rating_sigma" in frame.columns:
            delta = frame["rating_sigma"].to_numpy(dtype=float) / (rating_p * (1 - rating_p))
            sd = np.where(np.isfinite(sd), sd, delta)
        return np.clip(np.nan_to_num(sd, nan=0.0), 0.0, _MAX_RATING_SD_LOGIT)

    def predict_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Batch prediction for feature rows (columns as produced by ``build_training_rows``).

        Returns a DataFrame with ``rating``, ``lgbm``, ``stacked``, ``calibrated``, ``p``,
        ``sigma_rating``, ``sigma_model`` and ``sigma`` (``lgbm`` / ``stacked`` are NaN in fallback).
        """
        if "rating_p" not in frame.columns:
            raise ValueError("feature rows need the rating features (rating_p); pass a rating provider")
        floor = self.sigma_floor
        if self._core is None:
            p = np.clip(frame["rating_p"].to_numpy(dtype=float), _P_CLIP, 1 - _P_CLIP)
            sig = frame["rating_sigma"].to_numpy(dtype=float) if "rating_sigma" in frame else np.zeros(len(p))
            sig = np.maximum(np.nan_to_num(sig, nan=0.0), floor)
            nan = np.full(len(p), np.nan)
            return pd.DataFrame(
                {
                    "rating": p,
                    "lgbm": nan,
                    "stacked": nan,
                    "calibrated": p,
                    "p": p,
                    "sigma_rating": sig,
                    "sigma_model": np.zeros(len(p)),
                    "sigma": sig,
                },
                index=frame.index,
            )
        out = self._core_outputs(self._core, frame)
        p_cal = self._calibrator.transform(out["stacked"])
        slope = p_cal * (1.0 - p_cal)
        s_rating = slope * out["sd_rating_logit"]
        s_model = slope * out["sd_model_logit"]
        sigma = np.maximum(np.sqrt(s_rating**2 + s_model**2), floor)
        return pd.DataFrame(
            {
                "rating": out["rating"],
                "lgbm": out["lgbm"],
                "stacked": out["stacked"],
                "calibrated": p_cal,
                "p": p_cal,
                "sigma_rating": s_rating,
                "sigma_model": s_model,
                "sigma": sigma,
            },
            index=frame.index,
        )

    # ------------------------------------------------------------------ fit
    def fit(
        self,
        map_results: Iterable[MapResultRecord],
        *,
        store: FeatureStore,
        rating_provider: RatingProvider,
        as_of: datetime | None = None,
    ) -> FitReport:
        """Train on ``map_results`` (only records with ``played_at < as_of`` when ``as_of`` is given).

        ``rating_provider`` must answer for past timestamps (``WalkForwardRatings`` or a
        ``MapRatingModel`` fitted with ``record_history=True``), see :class:`RatingProvider`.

        The fit is atomic: if building the feature rows raises (e.g. a rating provider refusing a
        timestamp), the exception propagates and the ensemble keeps its previous state. A failure
        inside the model training itself (LightGBM, the stacker, the calibrator) does not raise: the
        ensemble falls back to the rating provider and the report names the reason.
        """
        previous = (self._core, self._calibrator, self.report, self.model_version, self.data_end)
        try:
            return self._fit(map_results, store=store, rating_provider=rating_provider, as_of=as_of)
        except BaseException:
            self._core, self._calibrator, self.report, self.model_version, self.data_end = previous
            raise

    def _fit(
        self,
        map_results: Iterable[MapResultRecord],
        *,
        store: FeatureStore,
        rating_provider: RatingProvider,
        as_of: datetime | None,
    ) -> FitReport:
        cfg = self.config
        records = list(map_results)
        if as_of is not None:
            cutoff = ensure_utc(as_of)
            records = [r for r in records if ensure_utc(r.played_at) < cutoff]
        self.data_end = max((ensure_utc(r.played_at) for r in records), default=None)
        x_all, y_all, meta_all = build_training_rows(
            records, store=store, rating_provider=rating_provider, mirror=cfg.mirror
        )
        n_rows = int((~meta_all["mirrored"]).sum()) if len(meta_all) else 0
        reason: str | None = None
        if n_rows < cfg.min_rows:
            reason = f"only {n_rows} labelled maps (min_rows={cfg.min_rows})"
        elif "rating_p" not in x_all.columns:
            reason = "the feature rows carry no rating features"
        if reason is not None:
            return self._finish_fallback(reason, n_rows, meta_all)
        try:
            return self._fit_models(x_all, y_all, meta_all, n_rows)
        except Exception as exc:  # degrade to the rating provider rather than leave no model at all
            logger.warning("MapWinEnsemble training failed, falling back to the ratings: %s", exc)
            return self._finish_fallback(f"training failed ({type(exc).__name__}: {exc})", n_rows, meta_all)

    def _fit_models(
        self, x_all: pd.DataFrame, y_all: np.ndarray, meta_all: pd.DataFrame, n_rows: int
    ) -> FitReport:
        cfg = self.config
        self._high_confidence = {}
        # time-ordered holdout
        starts = meta_all.drop_duplicates("match_id").sort_values(["as_of", "match_id"], kind="mergesort")
        last = ensure_utc(starts["as_of"].iloc[-1].to_pydatetime())
        window_start = last - timedelta(days=self.holdout_days)
        hold_ids = set(starts.loc[starts["as_of"] >= window_start, "match_id"].tolist())
        max_hold = int(len(starts) * cfg.max_holdout_fraction)
        warnings: list[str] = []
        if len(hold_ids) > max_hold:
            hold_ids = set(starts["match_id"].iloc[len(starts) - max_hold :].tolist())
            warnings.append(
                f"the last {self.holdout_days} days hold more than {cfg.max_holdout_fraction:.0%} of the "
                f"matches; the holdout is the latest {max_hold} matches instead"
            )
        is_hold = meta_all["match_id"].isin(hold_ids).to_numpy()
        train_rows = int((~is_hold & ~meta_all["mirrored"].to_numpy()).sum())
        if train_rows < cfg.min_rows // 2 or not hold_ids:
            # not enough to hold anything out: train on everything, no calibration
            warnings.append("too little data for a separate calibration holdout; identity calibration")
            drop = self._screen(x_all, y_all, meta_all)
            core = self._fit_core(x_all, y_all, meta_all, drop=drop)
            self._core = core
            self._calibrator = IdentityCalibrator(eps=cfg.calibration_eps).fit([0.5], [1])
            return self._finish(core, n_rows, meta_all, is_hold=None, holdout={}, warnings=warnings)

        x_tr, y_tr, m_tr = x_all.loc[~is_hold], y_all[~is_hold], meta_all.loc[~is_hold]
        x_ho, y_ho, m_ho = x_all.loc[is_hold], y_all[is_hold], meta_all.loc[is_hold]
        x_tr, m_tr = x_tr.reset_index(drop=True), m_tr.reset_index(drop=True)
        drop = self._screen(x_tr, y_tr, m_tr)
        core = self._fit_core(x_tr, y_tr, m_tr, drop=drop)
        out = self._core_outputs(core, x_ho)
        n_hold_maps = int((~m_ho["mirrored"]).sum())
        self._calibrator = self._fit_calibrator(out["stacked"], y_ho, n_hold_maps)
        holdout = self._holdout_metrics(out, y_ho, m_ho)
        if cfg.refit_on_all:
            warnings.extend(f"pre-holdout model: {w}" for w in core.warnings)
            core = self._fit_core(x_all, y_all, meta_all, drop=drop)  # own warnings added by _finish
        self._core = core
        return self._finish(core, n_rows, meta_all, is_hold=is_hold, holdout=holdout, warnings=warnings)

    def _screen(self, x: pd.DataFrame, y: np.ndarray, meta: pd.DataFrame) -> list[str]:
        """Columns to leave out after the feature-group screen (empty when screening is off)."""
        self._group_screen = {}
        if not self.config.screen_feature_groups:
            return []
        drop, summary = self._screen_feature_groups(x, y, meta)
        self._group_screen = summary
        return drop

    def _fit_calibrator(self, p: np.ndarray, y: np.ndarray, n_maps: int) -> AnyCalibrator:
        cfg = self.config
        if n_maps < cfg.min_holdout_rows:
            return IdentityCalibrator(eps=cfg.calibration_eps).fit(p, y)
        if n_maps < cfg.min_isotonic_rows:
            return PlattCalibrator(eps=cfg.calibration_eps).fit(p, y)
        iso = IsotonicCalibrator(eps=cfg.calibration_eps).fit(p, y)
        return temper_isotonic_ends(iso, p, y, prior=cfg.calibration_end_prior)

    def _holdout_metrics(
        self, out: Mapping[str, np.ndarray], y: np.ndarray, meta: pd.DataFrame
    ) -> dict[str, HoldoutMetrics]:
        """Holdout metrics on the true orientation; ``calibrated`` is cross-fitted over matches."""
        cfg = self.config
        orig = ~meta["mirrored"].to_numpy()
        res: dict[str, HoldoutMetrics] = {}
        for name in ("rating", "lgbm", "stacked"):
            res[name] = _metrics(y[orig], out[name][orig])
        # cross-fitted calibration: calibrate each match group with a calibrator fitted on the others
        match_ids = meta["match_id"].to_numpy()
        unique = np.unique(match_ids)
        k = max(2, min(cfg.calibration_cv_folds, unique.size))
        fold_of = {m: i % k for i, m in enumerate(np.random.default_rng(cfg.seed).permutation(unique))}
        folds = np.asarray([fold_of[m] for m in match_ids])
        cal = np.full(len(y), np.nan)
        n_maps = int(orig.sum())
        for f in range(k):
            test = folds == f
            train = ~test
            if train.sum() == 0 or test.sum() == 0:
                continue
            calib = self._fit_calibrator(out["stacked"][train], y[train], int((train & orig).sum()))
            cal[test] = calib.transform(out["stacked"][test])
        if n_maps and np.all(np.isfinite(cal[orig])):
            res["calibrated"] = _metrics(y[orig], cal[orig])
            self._high_confidence = high_confidence_report(cal[orig], y[orig], fold=True)
        return res

    def _version(self, n_rows: int, features: Sequence[str]) -> str:
        payload = json.dumps(
            {
                "config": self.config.model_dump(mode="json"),
                "features": list(features),
                "n_rows": n_rows,
                "data_end": self.data_end.isoformat() if self.data_end else None,
            },
            sort_keys=True,
        )
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]
        day = self.data_end.strftime("%Y%m%d") if self.data_end else "nodata"
        return f"ens-{day}-{digest}"

    def _window(self, meta: pd.DataFrame, mask: np.ndarray | None) -> tuple[datetime | None, datetime | None]:
        if mask is None or not mask.any():
            return None, None
        times = meta.loc[mask, "as_of"]
        return ensure_utc(times.min().to_pydatetime()), ensure_utc(times.max().to_pydatetime())

    def _finish_fallback(self, reason: str, n_rows: int, meta: pd.DataFrame) -> FitReport:
        logger.info("MapWinEnsemble falls back to the rating provider: %s", reason)
        self._core = None
        self._calibrator = IdentityCalibrator(eps=self.config.calibration_eps).fit([0.5], [1])
        self.model_version = self._version(n_rows, [])
        start, end = self._window(meta, np.ones(len(meta), dtype=bool)) if len(meta) else (None, None)
        self.report = FitReport(
            model_version=self.model_version,
            fitted_at=utcnow(),
            fallback=True,
            fallback_reason=reason,
            n_rows=n_rows,
            train_start=start,
            train_end=end,
            data_end=self.data_end,
            calibrator=self._calibrator.kind,
        )
        return self.report

    def _finish(
        self,
        core: _Core,
        n_rows: int,
        meta: pd.DataFrame,
        *,
        is_hold: np.ndarray | None,
        holdout: dict[str, HoldoutMetrics],
        warnings: list[str],
    ) -> FitReport:
        self.model_version = self._version(n_rows, core.features)
        orig = ~meta["mirrored"].to_numpy()
        train_mask = orig if is_hold is None else orig & ~is_hold
        hold_mask = None if is_hold is None else orig & is_hold
        tr_start, tr_end = self._window(meta, train_mask)
        ho_start, ho_end = self._window(meta, hold_mask)
        gain: float | None = None
        if "rating" in holdout and "calibrated" in holdout:
            a, b = holdout["rating"].log_loss, holdout["calibrated"].log_loss
            gain = a - b if a is not None and b is not None else None
        importance = self.feature_importance()
        self.report = FitReport(
            model_version=self.model_version,
            fitted_at=utcnow(),
            fallback=False,
            n_rows=n_rows,
            n_train_rows=int(train_mask.sum()),
            n_holdout_rows=int(hold_mask.sum()) if hold_mask is not None else 0,
            n_features=len(core.features),
            train_start=tr_start,
            train_end=tr_end,
            holdout_start=ho_start,
            holdout_end=ho_end,
            data_end=self.data_end,
            best_iteration=core.best_iteration,
            n_cv_folds=core.n_cv_folds,
            stacker={
                "rating": core.stack_coef[0],
                "lgbm": core.stack_coef[1],
                "intercept": core.stack_intercept,
            },
            calibrator=self._calibrator.kind,
            holdout=holdout,
            log_loss_gain_vs_rating=gain,
            feature_importance=dict(list(importance.items())[:25]),
            refit_on_all=self.config.refit_on_all and is_hold is not None,
            warnings=[*warnings, *core.warnings],
            high_confidence=dict(self._high_confidence) if is_hold is not None else {},
            feature_groups=dict(self._group_screen),
        )
        return self.report

    # ------------------------------------------------------------------ inspection
    def feature_importance(self, *, importance_type: Literal["gain", "split"] = "gain") -> dict[str, float]:
        """Share of total importance per feature (final + bootstrap models), largest first."""
        if self._core is None:
            return {}
        boosters = [self._core.booster, *self._core.bootstrap]
        total = np.zeros(len(self._core.features))
        for b in boosters:
            total += np.asarray(b.feature_importance(importance_type=importance_type), dtype=float)
        s = float(total.sum())
        if s <= 0:
            return dict.fromkeys(self._core.features, 0.0)
        shares = {f: float(v / s) for f, v in zip(self._core.features, total, strict=True)}
        return dict(sorted(shares.items(), key=lambda kv: -kv[1]))

    # ------------------------------------------------------------------ predict
    def _check_as_of(self, as_of: datetime) -> datetime:
        t = ensure_utc(as_of)
        if self.data_end is not None and t <= self.data_end:
            raise LookAheadError(
                f"prediction as of {t.isoformat()} but the model was trained on data up to "
                f"{self.data_end.isoformat()} (look-ahead); refit on data before {t.isoformat()}"
            )
        return t

    def predict(
        self,
        features_or_ctx: Mapping[str, float] | MatchContext,
        map_name: str,
        *,
        as_of: datetime,
        store: FeatureStore | None = None,
        rating_provider: RatingProvider | None = None,
        picked_by: TeamSlot | Literal["auto"] | None = "auto",
    ) -> MapWinEstimate:
        """P(team1 wins ``map_name``) as of ``as_of``.

        * ``features_or_ctx`` is either a feature row (a mapping as produced by
          ``features_for_match_map`` / ``match_map_features``, which must include the rating
          features) or a :class:`MatchContext`; with a context the row is built here from ``store``
          and ``rating_provider`` (``picked_by`` as in ``features_for_match_map``).
        * ``as_of`` must be after the latest map the model was trained on (``LookAheadError``).
        * ``ctx.match_id`` matters: when the store holds that match (a backtest over finished
          matches), every feature is cut at its start (``FeatureStore.cutoff``); give an upcoming
          match its own id.
        * ``components``: ``rating``, ``lgbm``, ``stacked``, ``calibrated``, ``sigma_rating``,
          ``sigma_model``, ``fallback`` (1.0 when the rating provider's estimate is returned as is).
        """
        t = self._check_as_of(as_of)
        name = normalize_map_name(map_name)
        picker: TeamSlot | None = picked_by if isinstance(picked_by, TeamSlot) else None
        if isinstance(features_or_ctx, MatchContext):
            ctx = features_or_ctx
            if picked_by == "auto":
                picker = _picker_from_ctx(ctx, name)
            if self._core is None or store is None:
                if rating_provider is None:
                    raise ValueError("predicting from a MatchContext needs a rating_provider")
                if self._core is not None:
                    raise ValueError("predicting from a MatchContext needs the FeatureStore (store=...)")
                est = rating_estimate(rating_provider, ctx.team1_id, ctx.team2_id, name, t, is_lan=ctx.is_lan)
                return self._fallback_estimate(name, est.p_team1_win, est.sigma, picker)
            row = features_for_match_map(
                ctx, name, store=store, as_of=t, rating_provider=rating_provider, picked_by=picked_by
            )
            if self.config.symmetric:
                mirrored_picker: TeamSlot | Literal["auto"] | None = (
                    picked_by.other if isinstance(picked_by, TeamSlot) else picked_by
                )
                mirrored_row = features_for_match_map(
                    mirror_context(ctx),
                    name,
                    store=store,
                    as_of=t,
                    rating_provider=rating_provider,
                    picked_by=mirrored_picker,
                )
                return self._symmetric_estimate(name, row, mirrored_row, picker)
        else:
            row = dict(features_or_ctx)
        if "rating_p" not in row:
            raise ValueError("the feature row has no rating features (rating_p); pass a rating provider")
        frame = pd.DataFrame([row], dtype=float)
        if self._core is None:
            sig = float(row.get("rating_sigma", 0.0))
            return self._fallback_estimate(name, float(row["rating_p"]), sig, picker)
        out = self.predict_frame(frame).iloc[0]
        return MapWinEstimate(
            map_name=name,
            p_team1_win=float(out["p"]),
            sigma=float(out["sigma"]),
            picked_by=picker,
            components={
                "rating": float(out["rating"]),
                "lgbm": float(out["lgbm"]),
                "stacked": float(out["stacked"]),
                "calibrated": float(out["calibrated"]),
                "sigma_rating": float(out["sigma_rating"]),
                "sigma_model": float(out["sigma_model"]),
                "fallback": 0.0,
            },
        )

    def _symmetric_estimate(
        self,
        name: str,
        row: Mapping[str, float],
        mirrored_row: Mapping[str, float],
        picker: TeamSlot | None,
    ) -> MapWinEstimate:
        """Average of the forward prediction and the mirrored one (seen from team1), in logit space."""
        for r in (row, mirrored_row):
            if "rating_p" not in r:
                raise ValueError("the feature row has no rating features (rating_p); pass a rating provider")
        out = self.predict_frame(pd.DataFrame([dict(row), dict(mirrored_row)], dtype=float))
        fwd, mir = out.iloc[0], out.iloc[1]

        def team1_view(col: str) -> tuple[float, float]:
            return float(fwd[col]), 1.0 - float(mir[col])

        p_fwd, p_mir = team1_view("p")
        z = 0.5 * (
            math.log(_clip(p_fwd) / (1.0 - _clip(p_fwd))) + math.log(_clip(p_mir) / (1.0 - _clip(p_mir)))
        )
        p = 1.0 / (1.0 + math.exp(-z))
        gap = p_fwd - p_mir
        s_rating = math.sqrt(0.5 * (float(fwd["sigma_rating"]) ** 2 + float(mir["sigma_rating"]) ** 2))
        s_model = math.sqrt(
            0.5 * (float(fwd["sigma_model"]) ** 2 + float(mir["sigma_model"]) ** 2) + (0.5 * gap) ** 2
        )
        sigma = max(math.sqrt(s_rating**2 + s_model**2), self.sigma_floor)
        comps = {col: 0.5 * sum(team1_view(col)) for col in ("rating", "lgbm", "stacked")}
        comps.update(
            {
                "calibrated": p,
                "sigma_rating": s_rating,
                "sigma_model": s_model,
                "fallback": 0.0,
                "p_forward": p_fwd,
                "p_mirrored": p_mir,
                "orientation_gap": gap,
            }
        )
        return MapWinEstimate(map_name=name, p_team1_win=p, sigma=sigma, picked_by=picker, components=comps)

    def _fallback_estimate(
        self, name: str, p: float, sigma: float, picker: TeamSlot | None
    ) -> MapWinEstimate:
        p = min(max(float(p), _P_CLIP), 1 - _P_CLIP)
        s = max(float(sigma) if math.isfinite(sigma) else 0.0, self.sigma_floor)
        return MapWinEstimate(
            map_name=name,
            p_team1_win=p,
            sigma=s,
            picked_by=picker,
            components={"rating": p, "calibrated": p, "sigma_rating": s, "sigma_model": 0.0, "fallback": 1.0},
        )

    def predict_for_sequences(
        self,
        ctx: MatchContext,
        sequences: Sequence[MapSequenceProbability],
        *,
        as_of: datetime,
        store: FeatureStore | None = None,
        rating_provider: RatingProvider | None = None,
    ) -> dict[str, MapWinEstimate]:
        """One estimate per map appearing in ``sequences`` (the input of ``build_match_prediction``).

        When a map's picker differs between sequences, the estimate is the probability-weighted
        mixture over the picker scenarios (``p`` = weighted mean, ``sigma`` by the law of total
        variance) and ``picked_by`` is ``None``; ``components`` are weighted means plus
        ``p_picked_by_team1`` / ``p_picked_by_team2``.
        """
        weights: dict[str, dict[TeamSlot | None, float]] = {}
        for seq in sequences:
            # a pickers list shorter than the maps means unknown pickers, never a dropped map (the same
            # rule as MapRatingModel.predict_for_sequences)
            pickers = list(seq.pickers[: len(seq.maps)]) + [None] * max(0, len(seq.maps) - len(seq.pickers))
            for m, picker in zip(seq.maps, pickers, strict=True):
                name = normalize_map_name(m)
                slot = weights.setdefault(name, {})
                slot[picker] = slot.get(picker, 0.0) + float(seq.probability)
        out: dict[str, MapWinEstimate] = {}
        for name, by_picker in weights.items():
            total = sum(by_picker.values())
            if total <= 0:
                continue
            parts = [
                (
                    w / total,
                    self.predict(
                        ctx,
                        name,
                        as_of=as_of,
                        store=store,
                        rating_provider=rating_provider,
                        picked_by=picker,
                    ),
                )
                for picker, w in by_picker.items()
            ]
            if len(parts) == 1:
                out[name] = parts[0][1]
                continue
            p = sum(w * e.p_team1_win for w, e in parts)
            var = sum(w * (e.sigma**2 + (e.p_team1_win - p) ** 2) for w, e in parts)
            comps: dict[str, float] = {}
            for w, e in parts:
                for key, val in e.components.items():
                    comps[key] = comps.get(key, 0.0) + w * float(val)
            comps["p_picked_by_team1"] = by_picker.get(TeamSlot.TEAM1, 0.0) / total
            comps["p_picked_by_team2"] = by_picker.get(TeamSlot.TEAM2, 0.0) / total
            out[name] = MapWinEstimate(
                map_name=name,
                p_team1_win=float(p),
                sigma=max(float(math.sqrt(var)), self.sigma_floor),
                picked_by=None,
                components=comps,
            )
        return out

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        core = self._core
        return {
            "format_version": FORMAT_VERSION,
            "kind": "MapWinEnsemble",
            "model_version": self.model_version,
            "config": self.config.model_dump(mode="json"),
            "model_settings": {
                "calibration_holdout_days": self._model_settings.calibration_holdout_days,
                "sigma_floor": self._model_settings.sigma_floor,
            },
            "data_end": self.data_end.isoformat() if self.data_end else None,
            "features": core.features if core else [],
            "calibrator": self._calibrator.to_dict(),
            "core": None
            if core is None
            else {
                "booster": core.booster.model_to_string(),
                "bootstrap": [b.model_to_string() for b in core.bootstrap],
                "stack_coef": list(core.stack_coef),
                "stack_intercept": core.stack_intercept,
                "best_iteration": core.best_iteration,
                "n_cv_folds": core.n_cv_folds,
                "n_oof": core.n_oof,
            },
            "report": self.report.model_dump(mode="json") if self.report else None,
        }

    def save(self, path: str | Path) -> Path:
        """Write the fitted ensemble as one JSON file (model version, features, window, boosters)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        return target

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MapWinEnsemble:
        if data.get("kind") != "MapWinEnsemble" or int(data.get("format_version", 0)) > FORMAT_VERSION:
            raise ValueError("not a MapWinEnsemble payload (or written by a newer version)")
        ms = data.get("model_settings") or {}
        model_settings = ModelSettings(
            calibration_holdout_days=int(ms.get("calibration_holdout_days", 90)),
            sigma_floor=float(ms.get("sigma_floor", 0.01)),
        )
        ens = cls(EnsembleConfig.model_validate(data["config"]), settings=model_settings)
        ens.model_version = str(data["model_version"])
        ens.data_end = ensure_utc(datetime.fromisoformat(data["data_end"])) if data.get("data_end") else None
        ens._calibrator = calibrator_from_dict(data["calibrator"])
        core = data.get("core")
        if core is not None:
            lgbm = _lgb()
            ens._core = _Core(
                features=list(data["features"]),
                booster=lgbm.Booster(model_str=core["booster"]),
                bootstrap=[lgbm.Booster(model_str=s) for s in core["bootstrap"]],
                stack_coef=(float(core["stack_coef"][0]), float(core["stack_coef"][1])),
                stack_intercept=float(core["stack_intercept"]),
                best_iteration=int(core["best_iteration"]),
                n_cv_folds=int(core["n_cv_folds"]),
                n_oof=int(core["n_oof"]),
            )
        if data.get("report"):
            ens.report = FitReport.model_validate(data["report"])
        return ens

    @classmethod
    def load(cls, path: str | Path) -> MapWinEnsemble:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def temper_isotonic_ends(
    cal: IsotonicCalibrator, p: ArrayLike, y: ArrayLike, *, prior: float = 1.0
) -> IsotonicCalibrator:
    """Shrink the end plateaus of a fitted isotonic map toward 1/2 by ``prior`` pseudo-counts each way.

    The top plateau (the highest calibrated value, shared by every probability above its first
    breakpoint) holds ``n`` fitted samples with ``k`` wins; its value ``k / n`` becomes at most
    ``(k + prior) / (n + 2 prior)`` - and the bottom plateau symmetrically at least that. Only the
    ends move, only toward 1/2, and the map stays non-decreasing (lower plateaus are capped by the new
    top value, higher ones floored by the new bottom value). In place; returns ``cal``.
    """
    if prior <= 0 or not cal.fitted or cal.x_.size < 2:
        return cal
    pa = np.asarray(p, dtype=float).ravel()
    ya = np.asarray(y, dtype=float).ravel()
    ok = np.isfinite(pa) & np.isfinite(ya)
    pa, ya = pa[ok], ya[ok]
    xs, ys = cal.x_, np.array(cal.y_, dtype=float)
    last = ys.size - 1
    j = last
    while j > 0 and ys[j - 1] == ys[last]:
        j -= 1
    top = pa >= xs[j]
    if top.any():
        value = (float(ya[top].sum()) + prior) / (float(top.sum()) + 2.0 * prior)
        if value < ys[last]:
            ys[j:] = value
            for i in range(j - 1, -1, -1):
                ys[i] = min(ys[i], ys[i + 1])
    j = 0
    while j < last and ys[j + 1] == ys[0]:
        j += 1
    low = pa <= xs[j]
    if low.any():
        value = (float(ya[low].sum()) + prior) / (float(low.sum()) + 2.0 * prior)
        if value > ys[0]:
            ys[: j + 1] = value
            for i in range(j + 1, ys.size):
                ys[i] = max(ys[i], ys[i - 1])
    cal.y_ = np.clip(ys, cal.eps, 1.0 - cal.eps)
    return cal


def _nanmean(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(finite.mean()) if finite.size else None


def _paired_gain(base: np.ndarray, trial: np.ndarray, match_ids: np.ndarray) -> tuple[float, float]:
    """Mean log-loss gain (base - trial) over rows scored by both, and its standard error with the
    rows of one match pooled (maps of a series are correlated). NaN when nothing is comparable."""
    both = np.isfinite(base) & np.isfinite(trial)
    if not both.any():
        return math.nan, math.nan
    diff = base[both] - trial[both]
    frame = pd.DataFrame({"m": match_ids[both], "d": diff}).groupby("m")["d"].sum()
    n_rows = float(both.sum())
    gain = float(diff.mean())
    if len(frame) < 2:
        return gain, math.inf
    # per-match totals -> SE of the mean per row
    se = float(frame.std(ddof=1) * math.sqrt(len(frame)) / n_rows)
    return gain, se


def _clip(p: float) -> float:
    return min(max(p, _P_CLIP), 1.0 - _P_CLIP)


def mirror_context(ctx: MatchContext) -> MatchContext:
    """The same match seen with team1 and team2 swapped (teams, lineups, veto slots, start sides, flags).

    Used for orientation-free predictions; ``match_id`` is kept (the feature store excludes the match
    itself by id). Context flags are mirrored too (team slot swapped, shift negated) although the
    feature rows do not read them.
    """
    return ctx.model_copy(
        update={
            "team1_id": ctx.team2_id,
            "team2_id": ctx.team1_id,
            "team1_name": ctx.team2_name,
            "team2_name": ctx.team1_name,
            "team1_lineup": list(ctx.team2_lineup),
            "team2_lineup": list(ctx.team1_lineup),
            "known_veto": [
                step.model_copy(update={"team": step.team.other if step.team is not None else None})
                for step in ctx.known_veto
            ],
            "first_veto_team": ctx.first_veto_team.other if ctx.first_veto_team is not None else None,
            "start_sides": {
                m: (side.other if side is not None else None) for m, side in ctx.start_sides.items()
            },
            "context_flags": [
                flag.model_copy(
                    update={
                        "team": flag.team.other if flag.team is not None else None,
                        "logit_shift": -flag.logit_shift,
                    }
                )
                for flag in ctx.context_flags
            ],
        }
    )


def _picker_from_ctx(ctx: MatchContext, map_name: str) -> TeamSlot | None:
    from cs2bot.domain.enums import VetoAction

    for step in ctx.known_veto:
        if normalize_map_name(step.map_name) == map_name:
            return step.team if step.action is VetoAction.PICK else None
    return None


def _metrics(y: np.ndarray, p: np.ndarray) -> HoldoutMetrics:
    if len(y) == 0:
        return HoldoutMetrics(n=0, log_loss=None, brier=None, ece=None)
    q = np.clip(p, 0.0, 1.0)
    return HoldoutMetrics(n=len(y), log_loss=log_loss(y, q), brier=brier(y, q), ece=ece(y, q, 10))
