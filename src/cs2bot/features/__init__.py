"""cs2bot.features - leakage-safe feature engineering (see docs/dev/reports/datasets_features.md).

* :class:`FeatureStore` - chronological indexes over the training records; every query takes an
  ``as_of`` and only reads data strictly before it.
* :func:`build_training_rows` / :func:`features_for_match_map` - the feature row per
  (match, map, team1-vs-team2) for training and for live prediction (same code path).
* :class:`RatingProvider` - the protocol a rating model implements to contribute rating features.
* :mod:`cs2bot.features.roles` - player roles from demo data.
* :mod:`cs2bot.features.meta` - active map pool, patch timeline, change-point detection.
* :mod:`cs2bot.features.deep` (deep_data) - schedule, travel, stakes, format, economy / clutch,
  player match-ups, coach and market factors (:class:`DeepFeatureIndex`, built by the store);
  :mod:`cs2bot.features.geo` and :mod:`cs2bot.features.stakes` are its lookup helpers.
"""

from cs2bot.features.deep import (
    DEEP_MATCH_FEATURES,
    DEEP_TEAM_FEATURES,
    DeepConfig,
    DeepFeatureIndex,
    deep_feature_names,
)
from cs2bot.features.geo import parse_location, team_home, tz_shift_hours
from cs2bot.features.meta import (
    BOCPDConfig,
    BOCPDResult,
    Patch,
    PatchTimeline,
    RegimeBreak,
    RegimeConfig,
    RegimeDetector,
    bocpd,
    days_since_last_patch,
    detect_active_map_pool,
    form_trend,
    load_patch_timeline,
    patch_id,
    recent_regime_break,
)
from cs2bot.features.roles import RoleConfig, decide_role, infer_roles, role_metrics, update_player_roles
from cs2bot.features.rows import (
    META_COLUMNS,
    RATING_LOGIT_FEATURE,
    TIER_RANK,
    RatingProvider,
    build_training_rows,
    features_for_match_map,
    match_map_features,
    rating_estimate,
    rating_features,
)
from cs2bot.features.stakes import StageInfo, classify_stage, swiss_situation
from cs2bot.features.store import FeatureConfig, FeatureStore
from cs2bot.features.strength import StrengthConfig, StrengthTrace, walk_strength

__all__ = [
    "DEEP_MATCH_FEATURES",
    "DEEP_TEAM_FEATURES",
    "META_COLUMNS",
    "RATING_LOGIT_FEATURE",
    "TIER_RANK",
    "BOCPDConfig",
    "BOCPDResult",
    "DeepConfig",
    "DeepFeatureIndex",
    "FeatureConfig",
    "FeatureStore",
    "Patch",
    "PatchTimeline",
    "RatingProvider",
    "RegimeBreak",
    "RegimeConfig",
    "RegimeDetector",
    "RoleConfig",
    "StageInfo",
    "StrengthConfig",
    "StrengthTrace",
    "bocpd",
    "build_training_rows",
    "classify_stage",
    "days_since_last_patch",
    "decide_role",
    "deep_feature_names",
    "detect_active_map_pool",
    "features_for_match_map",
    "form_trend",
    "infer_roles",
    "load_patch_timeline",
    "match_map_features",
    "parse_location",
    "patch_id",
    "rating_estimate",
    "rating_features",
    "recent_regime_break",
    "role_metrics",
    "swiss_situation",
    "team_home",
    "tz_shift_hours",
    "update_player_roles",
    "walk_strength",
]
