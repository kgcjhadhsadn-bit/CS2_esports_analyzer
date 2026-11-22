"""News / context layer contracts (Claude output is parsed into these)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from cs2bot.domain.enums import ContextFlagType, TeamSlot


class NewsItemIn(BaseModel):
    source: str
    url: str
    title: str
    body: str | None = None
    published_at: datetime | None = None
    team_ids: list[int] = Field(default_factory=list)
    player_ids: list[int] = Field(default_factory=list)


class ContextFlag(BaseModel):
    """A structured, bounded adjustment derived from news/context.

    ``logit_shift`` is applied to team1's per-map win logit (positive favours team1). It is
    clipped by ``LLMSettings.max_flag_logit_shift`` per flag and ``max_total_logit_shift`` in total.
    ``veto=True`` means "do not bet this match at all" (e.g. lineup chaos, possible forfeit).
    """

    flag_type: ContextFlagType
    team: TeamSlot | None = None  # which team the flag concerns (None = match-wide)
    severity: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    logit_shift: float = 0.0
    veto: bool = False
    rationale: str
    evidence: list[str] = Field(default_factory=list)  # URLs / news item ids
    expires_at: datetime | None = None
