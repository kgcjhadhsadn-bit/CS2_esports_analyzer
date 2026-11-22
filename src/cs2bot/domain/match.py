"""Input context for predicting one upcoming match."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import MatchFormat, TeamSlot, Tier
from cs2bot.domain.records import VetoStep


class LineupPlayer(BaseModel):
    player_id: int
    nickname: str | None = None
    is_standin: bool = False


class MatchContext(BaseModel):
    match_id: int
    scheduled_at: datetime
    format: MatchFormat
    team1_id: int
    team2_id: int
    team1_name: str | None = None
    team2_name: str | None = None
    tier: Tier = Tier.UNRANKED
    is_lan: bool = False
    tournament_name: str | None = None
    stage: str | None = None
    team1_lineup: list[LineupPlayer] = Field(default_factory=list)
    team2_lineup: list[LineupPlayer] = Field(default_factory=list)
    map_pool: list[str] = Field(default_factory=list)
    known_veto: list[VetoStep] = Field(default_factory=list)  # empty until published
    first_veto_team: TeamSlot | None = None
    context_flags: list[ContextFlag] = Field(default_factory=list)

    @property
    def veto_complete(self) -> bool:
        return bool(self.known_veto) and len(self.known_veto) >= len(self.map_pool) > 0
