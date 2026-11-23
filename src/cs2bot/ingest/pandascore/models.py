"""Typed (pydantic) views of the PandaScore REST payloads this project reads.

Field names were taken from the OpenAPI definitions embedded in the public reference pages
(``developers.pandascore.co/reference/get_csgo_matches_past-1``, ``..._matchidorslug-1``,
``..._matchidorslug_games-1``, ``get_tournaments_tournamentidorslug``, ``get_teams_teamidorslug``,
``get_tournaments_tournamentidorslug_rosters``, ``get_matches_matchidorslug_opponents``), read in
November 2026. No live token was available while writing this, so every model is deliberately tolerant:

* unknown fields are ignored (``extra="ignore"``), nearly everything is optional,
* ``null`` where a list is documented becomes an empty list,
* datetimes are parsed into aware UTC datetimes.

What is where in the API (this drives the sync):

* match lists (``/csgo/matches/...``) carry ``games`` with only id / position / status / winner,
* ``/csgo/matches/{id}`` adds ``rounds_score`` and anonymous per-game player lines,
* ``/csgo/matches/{id}/games`` and ``/csgo/games/{id}`` add the ``map``, the round-by-round history and
  player stat lines with their team (historical-data plans only),
* rosters come from ``/tournaments/{id}/rosters`` (or ``/matches/{id}/opponents``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

from cs2bot.utils.time import parse_datetime


def _to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str | int | float | datetime):
        return parse_datetime(value)
    raise ValueError(f"not a timestamp: {type(value).__name__}")


def _none_to_list(value: Any) -> Any:
    return [] if value is None else value


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


UTCDatetime = Annotated[datetime | None, BeforeValidator(_to_datetime)]
OptStr = Annotated[str | None, BeforeValidator(_blank_to_none)]


class PSModel(BaseModel):
    """Base for all payload models: ignore fields we do not model."""

    model_config = ConfigDict(extra="ignore")


class PSPlayer(PSModel):
    id: int | None = None
    name: OptStr = None  # the nickname
    first_name: OptStr = None
    last_name: OptStr = None
    nationality: OptStr = None  # ISO 3166-1 alpha-2
    role: OptStr = None
    slug: OptStr = None
    active: bool | None = None
    image_url: OptStr = None
    modified_at: UTCDatetime = None

    @property
    def real_name(self) -> str | None:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) if parts else None


class PSTeam(PSModel):
    id: int | None = None
    name: OptStr = None
    acronym: OptStr = None
    slug: OptStr = None
    location: OptStr = None  # ISO country code of the organisation
    image_url: OptStr = None
    modified_at: UTCDatetime = None
    players: Annotated[list[PSPlayer], BeforeValidator(_none_to_list)] = []


class PSLeague(PSModel):
    id: int | None = None
    name: OptStr = None
    slug: OptStr = None
    url: OptStr = None


class PSSerie(PSModel):
    id: int | None = None
    name: OptStr = None
    full_name: OptStr = None
    slug: OptStr = None
    season: OptStr = None
    year: int | None = None
    league_id: int | None = None
    begin_at: UTCDatetime = None
    end_at: UTCDatetime = None


class PSVideogameTitle(PSModel):
    id: int | None = None
    name: OptStr = None
    slug: OptStr = None  # "cs-2" or "cs-go"


class PSRosterEntry(PSModel):
    """One element of ``tournament.expected_roster``."""

    team: PSTeam | None = None
    players: Annotated[list[PSPlayer], BeforeValidator(_none_to_list)] = []


class PSTournament(PSModel):
    """A PandaScore *tournament* is one stage ("Playoffs", "Group A") of a serie of a league."""

    id: int | None = None
    name: OptStr = None
    slug: OptStr = None
    tier: OptStr = None  # s, a, b, c, d, unranked (null when not rated)
    type: OptStr = None  # "offline", "online", "online/offline"
    region: OptStr = None
    country: OptStr = None
    prizepool: OptStr = None  # e.g. "5000 United States Dollar"
    begin_at: UTCDatetime = None
    end_at: UTCDatetime = None
    modified_at: UTCDatetime = None
    league_id: int | None = None
    serie_id: int | None = None
    detailed_stats: bool | None = None
    has_bracket: bool | None = None
    league: PSLeague | None = None
    serie: PSSerie | None = None
    teams: Annotated[list[PSTeam], BeforeValidator(_none_to_list)] = []
    expected_roster: Annotated[list[PSRosterEntry], BeforeValidator(_none_to_list)] = []


class PSOpponent(PSModel):
    type: OptStr = None  # "Team" or "Player"
    opponent: PSTeam | None = None


class PSResult(PSModel):
    team_id: int | None = None
    player_id: int | None = None
    score: int | None = None


class PSWinnerRef(PSModel):
    id: int | None = None
    type: OptStr = None


class PSMap(PSModel):
    id: int | None = None
    name: OptStr = None
    slug: OptStr = None


class PSRoundScore(PSModel):
    team_id: int | None = None
    score: int | None = None


class PSRound(PSModel):
    round: int | None = None  # 1-based
    ct: int | None = None  # team id playing CT
    terrorists: int | None = None  # team id playing T
    winner_side: OptStr = None  # "ct" or "terrorists"
    winner_team: int | None = None
    outcome: OptStr = None  # defused, eliminated, exploded, planted_eliminated, timeout


class PSGamePlayer(PSModel):
    """One player's stat line in one game.

    ``/csgo/matches/{id}`` only gives ``player_id``; the games endpoints add ``player`` and ``team``.
    """

    player_id: int | None = None
    player: PSPlayer | None = None
    team: PSTeam | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    headshots: int | None = None
    adr: float | None = None
    kast: float | None = None  # 0..100
    rating: float | None = None
    flash_assists: float | None = None
    k_d_diff: int | None = None
    first_kills_diff: int | None = None

    @property
    def pandascore_player_id(self) -> int | None:
        if self.player is not None and self.player.id is not None:
            return self.player.id
        return self.player_id


class PSGame(PSModel):
    """One map of a match."""

    id: int | None = None
    match_id: int | None = None
    position: int | None = None  # 1-based map number
    status: OptStr = None  # finished, not_played, not_started, running
    finished: bool | None = None
    complete: bool | None = None  # stats are final
    forfeit: bool | None = None
    detailed_stats: bool | None = None
    begin_at: UTCDatetime = None
    end_at: UTCDatetime = None
    length: int | None = None  # seconds
    winner: PSWinnerRef | None = None
    map: PSMap | None = None
    rounds_score: Annotated[list[PSRoundScore], BeforeValidator(_none_to_list)] = []
    rounds: Annotated[list[PSRound], BeforeValidator(_none_to_list)] = []
    players: Annotated[list[PSGamePlayer], BeforeValidator(_none_to_list)] = []
    teams: Annotated[list[PSTeam], BeforeValidator(_none_to_list)] = []

    @property
    def winner_team_id(self) -> int | None:
        """PandaScore team id of the winner (None while undecided or for player-vs-player games)."""
        if self.winner is None or self.winner.id is None:
            return None
        if self.winner.type is not None and self.winner.type.lower() != "team":
            return None
        return self.winner.id

    def rounds_for(self, team_id: int | None) -> int | None:
        for entry in self.rounds_score:
            if team_id is not None and entry.team_id == team_id:
                return entry.score
        return None


class PSMatch(PSModel):
    id: int | None = None
    name: OptStr = None
    slug: OptStr = None
    status: OptStr = None  # not_started, running, finished, canceled, postponed
    match_type: OptStr = None  # best_of, first_to, all_games_played, custom, ...
    number_of_games: int | None = None
    begin_at: UTCDatetime = None  # == scheduled_at until the match starts, then the real start
    scheduled_at: UTCDatetime = None  # official time announced by the organiser
    original_scheduled_at: UTCDatetime = None
    end_at: UTCDatetime = None
    modified_at: UTCDatetime = None
    rescheduled: bool | None = None
    forfeit: bool | None = None
    draw: bool | None = None
    detailed_stats: bool | None = None
    winner_id: int | None = None
    winner_type: OptStr = None
    league_id: int | None = None
    serie_id: int | None = None
    tournament_id: int | None = None
    opponents: Annotated[list[PSOpponent], BeforeValidator(_none_to_list)] = []
    results: Annotated[list[PSResult], BeforeValidator(_none_to_list)] = []
    games: Annotated[list[PSGame], BeforeValidator(_none_to_list)] = []
    tournament: PSTournament | None = None
    serie: PSSerie | None = None
    league: PSLeague | None = None
    videogame_title: PSVideogameTitle | None = None

    @property
    def kickoff(self) -> datetime | None:
        """Best known official start time: ``scheduled_at``, else ``begin_at``, else the original time."""
        return self.scheduled_at or self.begin_at or self.original_scheduled_at

    @property
    def is_team_match(self) -> bool:
        """False for player-vs-player matches (never the case for CS2, but the schema allows it)."""
        return all((o.type or "Team").lower() == "team" for o in self.opponents)

    def team_opponents(self) -> list[PSTeam]:
        """Known team opponents in PandaScore's order (fewer than two = TBD)."""
        return [
            o.opponent
            for o in self.opponents
            if o.opponent is not None and o.opponent.id is not None and (o.type or "Team").lower() == "team"
        ]

    def score_for(self, team_id: int | None) -> int | None:
        for result in self.results:
            if team_id is not None and result.team_id == team_id:
                return result.score
        return None


class PSTournamentRosters(PSModel):
    """``GET /tournaments/{id}/rosters``."""

    type: OptStr = None
    rosters: Annotated[list[PSTeam], BeforeValidator(_none_to_list)] = []


class PSMatchOpponents(PSModel):
    """``GET /matches/{id}/opponents``."""

    opponent_type: OptStr = None
    opponents: Annotated[list[PSTeam], BeforeValidator(_none_to_list)] = []
