"""What actually happened in a finished match.

This is the input for settling bets (``cs2bot.engine.settlement``), for backtests and for scoring
predictions against reality. ``cs2bot.datasets`` builds it from the database.

Orientation is always our match record: ``team1`` / ``team2`` = ``matches.team1_id`` / ``team2_id``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cs2bot.domain.enums import MatchFormat, TeamSlot


class SettlementOutcome(StrEnum):
    """Result of one bet selection."""

    WON = "won"
    LOST = "lost"
    VOID = "void"  # stake refunded: map not played, match cancelled / forfeited, player did not play ...
    PUSH = "push"  # stake refunded: the result landed exactly on an integer line
    UNKNOWN = "unknown"  # not enough data to decide locally (e.g. no round history) - ask the bookmaker

    @property
    def is_refund(self) -> bool:
        return self in (SettlementOutcome.VOID, SettlementOutcome.PUSH)


class MapActual(BaseModel):
    """One map that was actually played."""

    model_config = ConfigDict(frozen=True)

    map_number: int  # 1-based position in the series
    map_name: str
    team1_rounds: int
    team2_rounds: int
    # Winner of every round in order (index 0 = round 1). None when no round history is available;
    # markets that need it (pistol rounds, halves, race to N) then settle as UNKNOWN.
    round_winners: tuple[TeamSlot, ...] | None = None
    # Internal player id -> kills on this map. Empty when no player stats are available.
    player_kills: dict[int, int] = Field(default_factory=dict)

    @property
    def winner(self) -> TeamSlot:
        return TeamSlot.TEAM1 if self.team1_rounds > self.team2_rounds else TeamSlot.TEAM2

    @property
    def total_rounds(self) -> int:
        return self.team1_rounds + self.team2_rounds

    @property
    def went_to_overtime(self) -> bool:
        return self.total_rounds > 24


class MatchActuals(BaseModel):
    """Final state of a match."""

    model_config = ConfigDict(frozen=True)

    match_id: int
    format: MatchFormat
    team1_id: int
    team2_id: int
    maps: tuple[MapActual, ...] = ()  # maps actually played, ordered by map_number
    forfeited: bool = False  # walkover / default win: every bet on the match is void
    cancelled: bool = False
    complete: bool = True  # False while the series is unfinished or map data is still missing

    @property
    def team1_maps(self) -> int:
        return sum(1 for m in self.maps if m.winner is TeamSlot.TEAM1)

    @property
    def team2_maps(self) -> int:
        return sum(1 for m in self.maps if m.winner is TeamSlot.TEAM2)

    @property
    def maps_played(self) -> int:
        return len(self.maps)

    def map(self, map_number: int) -> MapActual | None:
        """The k-th map of the series, or None if it was not played."""
        for m in self.maps:
            if m.map_number == map_number:
                return m
        return None
