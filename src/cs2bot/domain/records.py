"""Plain records used to train / evaluate models.

Model code consumes these (or DataFrames with the column names listed below) and never touches
the database directly. ``cs2bot.datasets`` converts DB rows into these records.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from cs2bot.domain.enums import (
    BuyType,
    MatchFormat,
    PlayerRole,
    RoundEndReason,
    Side,
    TeamSlot,
    Tier,
    VetoAction,
)


class MapResultRecord(BaseModel):
    """One finished map."""

    model_config = ConfigDict(frozen=True)

    match_id: int
    map_number: int
    map_name: str
    played_at: datetime
    team1_id: int
    team2_id: int
    team1_rounds: int
    team2_rounds: int
    team1_start_side: Side | None = None
    team1_ct_rounds: int | None = None
    team1_t_rounds: int | None = None
    team2_ct_rounds: int | None = None
    team2_t_rounds: int | None = None
    overtime_rounds: int = 0
    picked_by: TeamSlot | None = None  # None = decider / unknown
    is_lan: bool = False
    tier: Tier = Tier.UNRANKED
    format: MatchFormat | None = None
    team1_lineup: tuple[int, ...] = ()
    team2_lineup: tuple[int, ...] = ()

    @property
    def winner(self) -> TeamSlot:
        return TeamSlot.TEAM1 if self.team1_rounds > self.team2_rounds else TeamSlot.TEAM2

    @property
    def winner_team_id(self) -> int:
        return self.team1_id if self.winner is TeamSlot.TEAM1 else self.team2_id

    @property
    def total_rounds(self) -> int:
        return self.team1_rounds + self.team2_rounds


class VetoStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    step: int  # 1-based
    action: VetoAction
    team: TeamSlot | None  # None for the leftover / decider
    map_name: str


class VetoRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    match_id: int
    played_at: datetime
    format: MatchFormat
    team1_id: int
    team2_id: int
    steps: tuple[VetoStep, ...]
    map_pool: tuple[str, ...] | None = None


class RoundRecord(BaseModel):
    """One round, oriented to the match's team1/team2."""

    model_config = ConfigDict(frozen=True)

    match_id: int
    map_number: int
    map_name: str
    played_at: datetime
    round_number: int
    team1_id: int
    team2_id: int
    team1_side: Side
    winner: TeamSlot
    end_reason: RoundEndReason = RoundEndReason.OTHER
    team1_equipment_value: int | None = None
    team2_equipment_value: int | None = None
    team1_money_start: int | None = None
    team2_money_start: int | None = None
    team1_buy: BuyType | None = None
    team2_buy: BuyType | None = None
    bomb_planted: bool | None = None
    team1_survivors: int | None = None
    team2_survivors: int | None = None
    team1_score_before: int | None = None
    team2_score_before: int | None = None
    is_lan: bool = False

    @property
    def winner_side(self) -> Side:
        return self.team1_side if self.winner is TeamSlot.TEAM1 else self.team1_side.other


class PlayerMapRecord(BaseModel):
    """One player's stat line on one map."""

    model_config = ConfigDict(frozen=True)

    match_id: int
    map_number: int
    map_name: str
    played_at: datetime
    player_id: int
    team_id: int
    opponent_team_id: int
    kills: int
    deaths: int
    assists: int = 0
    rounds_played: int
    headshots: int | None = None
    adr: float | None = None
    kast: float | None = None
    rating: float | None = None
    first_kills: int | None = None
    first_deaths: int | None = None
    awp_kills: int | None = None
    role: PlayerRole | None = None
    is_lan: bool = False
    tier: Tier = Tier.UNRANKED
    is_standin: bool = False


# Column contracts for DataFrame-based code (pandas or polars). Same names as the record fields.
MAP_RESULT_COLUMNS: tuple[str, ...] = tuple(MapResultRecord.model_fields)
ROUND_COLUMNS: tuple[str, ...] = tuple(RoundRecord.model_fields)
PLAYER_MAP_COLUMNS: tuple[str, ...] = tuple(PlayerMapRecord.model_fields)
