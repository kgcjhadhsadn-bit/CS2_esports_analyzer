"""Typed results of the HLTV parsers (pure data, no DB) and the sync report.

Orientation inside these models is *HLTV's* page orientation: ``team1`` is the team HLTV lists
first (left). The sync layer maps it onto ``matches.team1_id`` / ``team2_id`` (which may be swapped).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from cs2bot.domain.enums import MatchFormat, MatchStatus, RoundEndReason, Side, TeamSlot, VetoAction
from cs2bot.domain.sync import SyncReport as _SharedSyncReport

BEST_OF_TO_FORMAT: dict[int, MatchFormat] = {
    1: MatchFormat.BO1,
    2: MatchFormat.BO2,
    3: MatchFormat.BO3,
    5: MatchFormat.BO5,
}


class HLTVMatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    DELETED = "deleted"  # HLTV "Match deleted": cancelled or a full forfeit / walkover

    def to_match_status(self) -> MatchStatus:
        return {
            HLTVMatchStatus.SCHEDULED: MatchStatus.SCHEDULED,
            HLTVMatchStatus.LIVE: MatchStatus.LIVE,
            HLTVMatchStatus.FINISHED: MatchStatus.FINISHED,
            HLTVMatchStatus.POSTPONED: MatchStatus.POSTPONED,
            HLTVMatchStatus.DELETED: MatchStatus.CANCELED,
        }[self]


class MapState(StrEnum):
    PLAYED = "played"
    LIVE = "live"  # has a (partial) score, match still running, no stats link yet
    UNPLAYED = "unplayed"  # not played (series decided earlier) or not played yet / TBA
    FORFEIT = "forfeit"  # HLTV "Default" map (walkover)


class TeamRef(BaseModel):
    name: str
    hltv_id: int | None = None


class EventRef(BaseModel):
    name: str
    hltv_id: int | None = None


# ------------------------------------------------------------------------------------- listings


class ResultRow(BaseModel):
    """One row of ``/results`` (team ids are not shown on that page)."""

    hltv_match_id: int
    url: str
    team1: TeamRef
    team2: TeamRef
    team1_score: int | None = None
    team2_score: int | None = None
    winner: TeamSlot | None = None
    event: EventRef | None = None
    stars: int = 0
    played_at: datetime | None = None
    # True when the row had no start time and ``played_at`` is the day headline at 00:00 UTC (the day
    # is in HLTV's display time zone, so the real start can be up to a day later)
    played_at_is_date_only: bool = False
    format: MatchFormat | None = None
    map_name: str | None = None  # BO1 only
    is_forfeit: bool = False
    format_hint: str | None = None  # raw ".map-text" (bo3 / nuke / def ...)


class Pagination(BaseModel):
    start: int
    end: int
    total: int
    next_url: str | None = None

    @property
    def has_next(self) -> bool:
        return self.end < self.total


class UpcomingRow(BaseModel):
    """One upcoming or live match of ``/matches``."""

    hltv_match_id: int
    url: str
    team1: TeamRef | None = None
    team2: TeamRef | None = None
    scheduled_at: datetime | None = None
    format: MatchFormat | None = None
    format_hint: str | None = None
    event: EventRef | None = None
    stars: int = 0
    is_live: bool = False
    is_lan: bool | None = None
    title: str | None = None  # placeholder text when the teams are not known yet
    map_names: list[str] = Field(default_factory=list)

    @property
    def has_teams(self) -> bool:
        return self.team1 is not None and self.team2 is not None


# ------------------------------------------------------------------------------------- match page


class HalfScore(BaseModel):
    team1_rounds: int
    team2_rounds: int
    team1_side: Side | None = None  # None for overtime groups (HLTV does not mark OT sides)
    is_overtime: bool = False


class MapResult(BaseModel):
    map_number: int  # 1-based, series order
    map_name: str  # normalised ("mirage"); "tba" / "default" for placeholders
    raw_name: str
    state: MapState
    team1_rounds: int | None = None
    team2_rounds: int | None = None
    winner: TeamSlot | None = None
    halves: list[HalfScore] = Field(default_factory=list)
    team1_start_side: Side | None = None
    mapstats_id: int | None = None
    mapstats_url: str | None = None
    picked_by: TeamSlot | None = None
    is_decider: bool = False

    @property
    def total_rounds(self) -> int | None:
        if self.team1_rounds is None or self.team2_rounds is None:
            return None
        return self.team1_rounds + self.team2_rounds

    @property
    def overtime_rounds(self) -> int:
        total = self.total_rounds
        return max(0, total - 24) if total is not None else 0

    def regulation_side_rounds(self) -> dict[tuple[TeamSlot, Side], int] | None:
        """Rounds won per (team, side) in regulation, or None if sides are unknown."""
        regulation = [h for h in self.halves if not h.is_overtime]
        if not regulation or any(h.team1_side is None for h in regulation):
            return None
        out = {(slot, side): 0 for slot in TeamSlot for side in Side}
        for half in regulation:
            side1 = half.team1_side
            assert side1 is not None
            out[(TeamSlot.TEAM1, side1)] += half.team1_rounds
            out[(TeamSlot.TEAM2, side1.other)] += half.team2_rounds
        return out


class VetoEntry(BaseModel):
    step: int
    action: VetoAction
    team: TeamSlot | None = None  # None for the leftover map or an unresolved team name
    team_name: str | None = None
    map_name: str
    raw: str


class LineupPlayerRow(BaseModel):
    nickname: str
    hltv_id: int | None = None
    is_standin: bool = False
    full_name: str | None = None  # "Mathieu 'ZywOo' Herbaut"
    real_name: str | None = None  # "Mathieu Herbaut"
    country: str | None = None


class DemoLink(BaseModel):
    hltv_demo_id: int
    url: str
    map_name: str | None = None  # set when the page ties the download to one map


class MatchPage(BaseModel):
    hltv_match_id: int
    url: str
    team1: TeamRef | None = None
    team2: TeamRef | None = None
    team1_rank: int | None = None
    team2_rank: int | None = None
    rank_date: date | None = None
    scheduled_at: datetime | None = None
    status: HLTVMatchStatus = HLTVMatchStatus.SCHEDULED
    status_text: str | None = None
    best_of: int | None = None
    format: MatchFormat | None = None
    is_lan: bool | None = None
    event: EventRef | None = None
    stage: str | None = None
    notes: list[str] = Field(default_factory=list)
    team1_score: int | None = None
    team2_score: int | None = None
    winner: TeamSlot | None = None
    maps: list[MapResult] = Field(default_factory=list)
    vetoes: list[VetoEntry] = Field(default_factory=list)
    team1_lineup: list[LineupPlayerRow] = Field(default_factory=list)
    team2_lineup: list[LineupPlayerRow] = Field(default_factory=list)
    demos: list[DemoLink] = Field(default_factory=list)
    is_forfeit: bool = False
    forfeit_note: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def demo_link(self) -> DemoLink | None:
        return self.demos[0] if self.demos else None

    @property
    def played_maps(self) -> list[MapResult]:
        return [m for m in self.maps if m.state in (MapState.PLAYED, MapState.LIVE)]

    def team(self, slot: TeamSlot) -> TeamRef | None:
        return self.team1 if slot is TeamSlot.TEAM1 else self.team2

    def lineup(self, slot: TeamSlot) -> list[LineupPlayerRow]:
        return self.team1_lineup if slot is TeamSlot.TEAM1 else self.team2_lineup


# ------------------------------------------------------------------------------------- map stats


class PlayerStatLine(BaseModel):
    nickname: str
    hltv_id: int | None = None
    country: str | None = None
    kills: int | None = None
    headshots: int | None = None
    assists: int | None = None
    flash_assists: int | None = None
    deaths: int | None = None
    traded_deaths: int | None = None
    kast: float | None = None  # 0..100
    adr: float | None = None
    rating: float | None = None
    first_kills: int | None = None
    first_deaths: int | None = None
    kd_diff: int | None = None
    fk_diff: int | None = None
    multi_kill_rounds: int | None = None
    clutches_won: int | None = None
    round_swing: float | None = None  # percent (Rating 3.0 pages only)


class TeamMapStats(BaseModel):
    team: TeamRef
    rounds: int | None = None
    won: bool | None = None
    start_side: Side | None = None
    ct_rounds: int | None = None
    t_rounds: int | None = None
    overtime_rounds: int | None = None  # rounds this team won in overtime
    team_rating: float | None = None
    first_kills: int | None = None
    clutches_won: int | None = None
    players: list[PlayerStatLine] = Field(default_factory=list)
    ct_players: list[PlayerStatLine] = Field(default_factory=list)
    t_players: list[PlayerStatLine] = Field(default_factory=list)


class RoundOutcome(BaseModel):
    round_number: int  # 1-based
    winner: TeamSlot  # page orientation (team1 = left team of the map stats page)
    winner_side: Side | None = None
    team1_side: Side | None = None
    end_reason: RoundEndReason = RoundEndReason.OTHER
    outcome: str  # raw icon name: ct_win / t_win / bomb_defused / bomb_exploded / stopwatch
    team1_score: int | None = None  # score after the round, from the icon title
    team2_score: int | None = None
    is_overtime: bool = False


class MapStatsPage(BaseModel):
    mapstats_id: int
    url: str
    hltv_match_id: int | None = None
    map_name: str
    raw_map_name: str | None = None
    played_at: datetime | None = None
    event: EventRef | None = None
    team1: TeamMapStats
    team2: TeamMapStats
    rating_version: str | None = None
    rounds: list[RoundOutcome] = Field(default_factory=list)
    rounds_complete: bool = False  # round history agrees with the final score
    side_split_complete: bool = False  # CT/T round counts cover every round (incl. overtime)
    warnings: list[str] = Field(default_factory=list)

    @property
    def total_rounds(self) -> int | None:
        if self.team1.rounds is None or self.team2.rounds is None:
            return None
        return self.team1.rounds + self.team2.rounds

    @property
    def overtime_rounds(self) -> int:
        total = self.total_rounds
        return max(0, total - 24) if total is not None else 0

    @property
    def winner(self) -> TeamSlot | None:
        if self.team1.rounds is None or self.team2.rounds is None or self.team1.rounds == self.team2.rounds:
            return None
        return TeamSlot.TEAM1 if self.team1.rounds > self.team2.rounds else TeamSlot.TEAM2


# ------------------------------------------------------------------------------------- ranking / news


class RankingRow(BaseModel):
    rank: int
    team_name: str
    hltv_team_id: int | None = None
    points: int | None = None
    ranking_date: date | None = None
    change: int | None = None  # +up / -down; None when new or unknown
    is_new: bool = False
    player_hltv_ids: list[int] = Field(default_factory=list)


class NewsRow(BaseModel):
    url: str
    hltv_news_id: int | None = None
    title: str
    published_at: datetime | None = None
    comments: int | None = None
    country: str | None = None


class NewsArticle(BaseModel):
    url: str
    hltv_news_id: int | None = None
    title: str
    published_at: datetime | None = None
    author: str | None = None
    lead: str | None = None
    body: str = ""
    team_hltv_ids: list[int] = Field(default_factory=list)
    player_hltv_ids: list[int] = Field(default_factory=list)


# ------------------------------------------------------------------------------------- sync report


# The report type is shared with the other ingestion sources.
SyncReport = _SharedSyncReport
