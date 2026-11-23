"""cs2bot.datasets - database rows -> plain records, match context and actual results.

Public interface (see docs/dev/reports/datasets_features.md):

* training records: :func:`load_map_results`, :func:`load_veto_records`, :func:`load_round_records`,
  :func:`load_player_map_records`, :func:`to_frame`
* demo-derived extras: :func:`load_team_demo_stats`, :func:`load_role_frames`,
  :func:`available_position_seconds`
* one match: :func:`load_match_context`, :func:`data_completeness`, :func:`load_match_actuals`
"""

from cs2bot.datasets._common import SOURCE_PRIORITY
from cs2bot.datasets.actuals import load_match_actuals
from cs2bot.datasets.context import (
    CompletenessConfig,
    MatchNotFoundError,
    completeness_breakdown,
    data_completeness,
    load_match_context,
)
from cs2bot.datasets.demo import (
    RoleFrames,
    TeamDemoMapRecord,
    available_position_seconds,
    load_role_frames,
    load_team_demo_stats,
)
from cs2bot.datasets.loaders import (
    load_map_results,
    load_player_map_records,
    load_round_records,
    load_veto_records,
    to_frame,
)

__all__ = [
    "SOURCE_PRIORITY",
    "CompletenessConfig",
    "MatchNotFoundError",
    "RoleFrames",
    "TeamDemoMapRecord",
    "available_position_seconds",
    "completeness_breakdown",
    "data_completeness",
    "load_map_results",
    "load_match_actuals",
    "load_match_context",
    "load_player_map_records",
    "load_role_frames",
    "load_round_records",
    "load_team_demo_stats",
    "load_veto_records",
    "to_frame",
]
