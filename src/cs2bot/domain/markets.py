"""Internal betting-market taxonomy.

Every bookmaker outcome is mapped to a ``(MarketSpec, selection_key)`` pair, always expressed in
the orientation of *our* match (``team1`` / ``team2`` = ``matches.team1_id`` / ``team2_id``),
never the bookmaker's display order. Unmapped markets are stored but never bet on.

Line conventions
----------------
* Handicap ``line`` is always from **team1's** perspective. ``MAP_HANDICAP(line=-1.5)`` with
  selection ``team1`` means "team1 wins by 2+ maps"; selection ``team2`` in the same spec means
  "team2 +1.5". Integer lines can push (stake refunded).
* Totals: selection ``over`` / ``under`` relative to ``line``.

Selection keys
--------------
``team1``, ``team2``, ``draw``, ``over``, ``under``, ``yes``, ``no``, ``odd``, ``even``,
score strings ``"2-1"`` / ``"13-9"`` (team1 first) and ``"player:<internal player id>"``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class MarketType(StrEnum):
    MATCH_WINNER = "match_winner"  # team1 / team2 (draw for BO2)
    MAP_WINNER = "map_winner"  # map_number; team1 / team2
    CORRECT_MAP_SCORE = "correct_map_score"  # "2-0", "2-1", "1-2", "0-2" ...
    MAP_HANDICAP = "map_handicap"  # line (team1 perspective); team1 / team2
    TOTAL_MAPS = "total_maps"  # line; over / under
    BOTH_TEAMS_WIN_A_MAP = "both_teams_win_a_map"  # yes / no
    ROUND_HANDICAP = "round_handicap"  # map_number, line (team1 perspective); team1 / team2
    TOTAL_ROUNDS = "total_rounds"  # map_number, line; over / under
    TEAM_TOTAL_ROUNDS = "team_total_rounds"  # map_number, team, line; over / under
    TOTAL_ROUNDS_ODD_EVEN = "total_rounds_odd_even"  # map_number; odd / even
    MAP_CORRECT_SCORE = "map_correct_score"  # map_number; "13-9" ...
    MAP_OVERTIME = "map_overtime"  # map_number; yes / no
    PISTOL_ROUND_WINNER = "pistol_round_winner"  # map_number, round_number (1 or 13); team1 / team2
    RACE_TO_ROUNDS = "race_to_rounds"  # map_number, line = N rounds; team1 / team2
    HALF_WINNER = "half_winner"  # map_number, half (1|2); team1 / team2 / draw
    PLAYER_KILLS = "player_kills"  # player_id, map_number or maps_scope, line; over / under
    PLAYER_KILLS_H2H = "player_kills_h2h"  # player_id vs opponent_player_id; player:<id>
    TOP_FRAGGER = "top_fragger"  # map_number/maps_scope, optional team; player:<id>
    OTHER = "other"  # unmapped - never bet


SELECTION_TEAM1 = "team1"
SELECTION_TEAM2 = "team2"
SELECTION_DRAW = "draw"
SELECTION_OVER = "over"
SELECTION_UNDER = "under"
SELECTION_YES = "yes"
SELECTION_NO = "no"
SELECTION_ODD = "odd"
SELECTION_EVEN = "even"
PLAYER_SELECTION_PREFIX = "player:"

# Markets whose outcome depends on a specific map being played (void if it is not).
MAP_SCOPED_MARKETS: frozenset[MarketType] = frozenset(
    {
        MarketType.MAP_WINNER,
        MarketType.ROUND_HANDICAP,
        MarketType.TOTAL_ROUNDS,
        MarketType.TEAM_TOTAL_ROUNDS,
        MarketType.TOTAL_ROUNDS_ODD_EVEN,
        MarketType.MAP_CORRECT_SCORE,
        MarketType.MAP_OVERTIME,
        MarketType.PISTOL_ROUND_WINNER,
        MarketType.RACE_TO_ROUNDS,
        MarketType.HALF_WINNER,
    }
)


def player_selection(player_id: int) -> str:
    return f"{PLAYER_SELECTION_PREFIX}{player_id}"


def parse_player_selection(selection: str) -> int | None:
    if selection.startswith(PLAYER_SELECTION_PREFIX):
        try:
            return int(selection[len(PLAYER_SELECTION_PREFIX) :])
        except ValueError:
            return None
    return None


def score_selection(team1: int, team2: int) -> str:
    return f"{team1}-{team2}"


def parse_score_selection(selection: str) -> tuple[int, int] | None:
    parts = selection.split("-")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


class MarketSpec(BaseModel):
    """A bookmaker market normalised into our taxonomy (immutable, hashable)."""

    model_config = ConfigDict(frozen=True)

    market_type: MarketType
    map_number: int | None = None  # 1-based position in the series; None = whole match
    maps_scope: tuple[int, ...] | None = None  # e.g. (1, 2) for "kills on maps 1-2"
    line: float | None = None
    team: str | None = None  # "team1" / "team2" for team-specific markets
    player_id: int | None = None
    player_name: str | None = None
    opponent_player_id: int | None = None
    half: int | None = None
    round_number: int | None = None

    @field_validator("team")
    @classmethod
    def _check_team(cls, v: str | None) -> str | None:
        if v is not None and v not in (SELECTION_TEAM1, SELECTION_TEAM2):
            raise ValueError("team must be 'team1' or 'team2'")
        return v

    @property
    def is_map_scoped(self) -> bool:
        return self.market_type in MAP_SCOPED_MARKETS and self.map_number is not None

    def key(self) -> str:
        """Canonical, stable string used for DB indexing and de-duplication."""
        parts: list[str] = [self.market_type.value]
        if self.map_number is not None:
            parts.append(f"map={self.map_number}")
        if self.maps_scope is not None:
            parts.append("maps=" + ",".join(str(m) for m in self.maps_scope))
        if self.team is not None:
            parts.append(f"team={self.team}")
        if self.player_id is not None:
            parts.append(f"player={self.player_id}")
        if self.opponent_player_id is not None:
            parts.append(f"opp={self.opponent_player_id}")
        if self.half is not None:
            parts.append(f"half={self.half}")
        if self.round_number is not None:
            parts.append(f"round={self.round_number}")
        if self.line is not None:
            parts.append(f"line={self.line:g}")
        return "|".join(parts)


class MarketOutcome(BaseModel):
    """One concrete bettable outcome of a market."""

    model_config = ConfigDict(frozen=True)

    spec: MarketSpec
    selection: str

    def key(self) -> str:
        return f"{self.spec.key()}#{self.selection}"
