"""Database seeding helpers for the demo tests: one match between "Alpha" and "Bravo" with lineups."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from cs2bot.db.models import MapPlayed, Match, MatchLineup, Player, Team
from cs2bot.domain.enums import DataSource, MatchFormat, MatchStatus, Side
from tests.ingest.demos.demos_builders import STEAMIDS, TEAM_A, TEAM_B

PLAYED_AT = datetime(2026, 3, 10, 18, 0, tzinfo=UTC)


@dataclass
class SeededMatch:
    match_id: int
    team_alpha_id: int
    team_bravo_id: int
    map_ids: dict[int, int] = field(default_factory=dict)  # map_number -> maps_played.id
    player_ids: dict[str, int] = field(default_factory=dict)  # demo name ("a1") -> players.id

    @property
    def steamid_map(self) -> dict[int, int]:
        return {STEAMIDS[name]: player_id for name, player_id in self.player_ids.items()}


def seed_match(
    session: Session,
    *,
    alpha_is_team1: bool = True,
    maps: tuple[str, ...] = ("mirage",),
    with_lineups: bool = True,
    alpha_name: str = "Alpha",
    bravo_name: str = "Bravo",
    nicknames: dict[str, str] | None = None,
    played_at: datetime = PLAYED_AT,
    team1_start_side: Side | None = None,
    scores: dict[int, tuple[int, int]] | None = None,
) -> SeededMatch:
    """Create teams, ten players, a finished match and its maps; returns the ids.

    ``alpha_is_team1=False`` stores the match as Bravo vs Alpha, i.e. the demo's roster "a" (team A,
    which starts as CT in ``standard_match``) is ``matches.team2``.
    """
    alpha = Team(name=alpha_name)
    bravo = Team(name=bravo_name)
    session.add_all([alpha, bravo])
    session.flush()
    team1, team2 = (alpha, bravo) if alpha_is_team1 else (bravo, alpha)
    match = Match(
        team1_id=team1.id,
        team2_id=team2.id,
        scheduled_at=played_at,
        status=MatchStatus.FINISHED,
        format=MatchFormat.BO3 if len(maps) > 1 else MatchFormat.BO1,
        in_scope=True,
    )
    session.add(match)
    session.flush()
    seeded = SeededMatch(match_id=match.id, team_alpha_id=alpha.id, team_bravo_id=bravo.id)
    for name in TEAM_A + TEAM_B:
        team = alpha if name in TEAM_A else bravo
        player = Player(nickname=(nicknames or {}).get(name, name), current_team_id=team.id)
        session.add(player)
        session.flush()
        seeded.player_ids[name] = player.id
        if with_lineups:
            session.add(
                MatchLineup(match_id=match.id, team_id=team.id, player_id=player.id, source=DataSource.HLTV)
            )
    for number, map_name in enumerate(maps, start=1):
        score = (scores or {}).get(number)
        map_played = MapPlayed(
            match_id=match.id,
            map_number=number,
            map_name=map_name,
            status=MatchStatus.FINISHED,
            played_at=played_at,
            team1_start_side=team1_start_side,
            team1_rounds=score[0] if score else None,
            team2_rounds=score[1] if score else None,
        )
        session.add(map_played)
        session.flush()
        seeded.map_ids[number] = map_played.id
    session.flush()
    return seeded
