"""Shared helpers for the odds tests (gRPC-web payload builder, DB factories, sample events)."""

from __future__ import annotations

import struct
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from cs2bot.db.models import Match, MatchLineup, Player, Team, TeamAlias, Tournament
from cs2bot.domain.enums import DataSource, MatchStatus
from cs2bot.domain.odds import CollectedEvent, CollectedMarket, CollectedSelection
from cs2bot.odds.grpc_web import encode_varint, field_bytes, field_varint, grpc_web_frame
from cs2bot.utils.names import normalize_team_name

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- protobuf builders


def fixed64(number: int, value: float) -> bytes:
    return encode_varint(number << 3 | 1) + struct.pack("<d", value)


def proto_selection(sel_id: int, name: str, odds: float, betslip_market_id: int | None = None) -> bytes:
    msg = field_varint(1, sel_id) + field_bytes(10, name) + fixed64(12, odds)
    if betslip_market_id is not None:
        msg += field_varint(15, betslip_market_id)
    return msg


def proto_market(
    market_id: int, name: str, selections: list[bytes], *, suspended: bool = False, layout: str = "direct"
) -> bytes:
    msg = field_varint(1, market_id) + field_bytes(2, name)
    if suspended:
        msg += field_varint(9, 3)
    msg += field_varint(12, 4)  # the Market's own field 12 is an enum, never odds
    if layout == "direct":
        msg += b"".join(field_bytes(16, s) for s in selections)
    else:  # nested "selection matrix": groups -> rows -> selections
        rows = b"".join(field_bytes(1, field_bytes(1, s)) for s in selections)
        msg += field_bytes(10, rows)
    return msg


def proto_match(
    match_id: int,
    name: str,
    date: str,
    contestants: tuple[str, str],
    competition: str,
    markets: list[bytes],
    *,
    live: bool = False,
    grouped_markets: list[bytes] | None = None,
    categories: tuple[str, ...] = ("ca_esp_top", "ca_esp_maps"),
) -> bytes:
    msg = field_varint(1, match_id) + field_bytes(2, name) + field_bytes(3, date)
    if live:
        msg += field_varint(6, 1)
    msg += field_bytes(8, field_varint(1, 9001) + field_bytes(2, competition) + field_bytes(5, "World"))
    for market in markets:
        msg += field_bytes(9, market)
    if grouped_markets:
        msg += field_bytes(11, field_bytes(2, "Mapy") + b"".join(field_bytes(3, m) for m in grouped_markets))
    for idx, contestant in enumerate(contestants, start=1):
        msg += field_bytes(12, field_bytes(1, f"contestant:{idx}") + field_bytes(3, contestant))
    for cat in categories:
        msg += field_bytes(20, field_bytes(1, cat) + field_bytes(2, cat.split("_")[-1].title()))
    return msg


def grpc_response(match: bytes, *, extra_frames: list[bytes] | None = None, status: int = 0) -> bytes:
    """GetMatch-like response body: data frame(s) + trailer frame."""
    body = grpc_web_frame(field_bytes(1, field_bytes(1, match)))
    for frame in extra_frames or []:
        body += grpc_web_frame(frame)
    trailer = f"grpc-status:{status}\r\ngrpc-message:\r\n".encode()
    return body + b"\x80" + struct.pack(">I", len(trailer)) + trailer


def sample_grpc_match(match_id: int = 1001234567, *, odds_navi: float = 1.62, live: bool = False) -> bytes:
    winner = proto_market(
        8001,
        "Zwycięzca meczu",
        [
            proto_selection(9001, "Natus Vincere", odds_navi, 8001),
            proto_selection(9002, "FaZe Clan", 2.25, 8001),
        ],
    )
    rounds = proto_market(
        8002,
        "Mapa 1 - liczba rund",
        [
            proto_selection(9011, "Powyżej 21,5", 1.85, 80021),
            proto_selection(9012, "Poniżej 21,5", 1.95, 80021),
            proto_selection(9013, "Powyżej 23,5", 2.6, 80022),
            proto_selection(9014, "Poniżej 23,5", 1.48, 80022),
        ],
        layout="matrix",
    )
    overtime = proto_market(
        8003,
        "Dogrywka - mapa 1",
        [proto_selection(9021, "Tak", 4.75, 8003), proto_selection(9022, "Nie", 1.15, 8003)],
        suspended=True,
    )
    return proto_match(
        match_id,
        "Natus Vincere - FaZe Clan",
        "2026-11-23T17:00:00Z",
        ("Natus Vincere", "FaZe Clan"),
        "Counter-Strike 2 - IEM Chengdu",
        [winner],
        live=live,
        grouped_markets=[rounds, overtime],
    )


# --------------------------------------------------------------------------- DB factories


def make_team(session: Session, name: str, *, aliases: tuple[str, ...] = (), slug: str | None = None) -> Team:
    team = Team(name=name, slug=slug)
    session.add(team)
    session.flush()
    for alias in aliases:
        session.add(TeamAlias(team_id=team.id, alias=normalize_team_name(alias), source=DataSource.HLTV))
    session.flush()
    return team


def make_match(
    session: Session,
    team1: Team,
    team2: Team,
    at: datetime,
    *,
    status: MatchStatus = MatchStatus.SCHEDULED,
    in_scope: bool = True,
    tournament: str | None = None,
) -> Match:
    tour_id = None
    if tournament is not None:
        tour = Tournament(name=tournament)
        session.add(tour)
        session.flush()
        tour_id = tour.id
    match = Match(
        team1_id=team1.id,
        team2_id=team2.id,
        scheduled_at=at,
        status=status,
        in_scope=in_scope,
        tournament_id=tour_id,
    )
    session.add(match)
    session.flush()
    return match


def add_lineup(session: Session, match: Match, team: Team, nicknames: list[str]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for nick in nicknames:
        player = Player(nickname=nick, current_team_id=team.id)
        session.add(player)
        session.flush()
        session.add(
            MatchLineup(match_id=match.id, team_id=team.id, player_id=player.id, source=DataSource.HLTV)
        )
        ids[nick] = player.id
    session.flush()
    return ids


# --------------------------------------------------------------------------- collected events


def sel(name: str, odds: str, ext: str = "", available: bool = True) -> CollectedSelection:
    return CollectedSelection(
        external_id=ext or name, raw_name=name, odds=Decimal(odds), is_available=available
    )


def market(name: str, *selections: CollectedSelection, ext: str = "") -> CollectedMarket:
    return CollectedMarket(external_id=ext or name, raw_name=name, selections=list(selections))


def event(
    external_id: str = "1001",
    team1: str = "Natus Vincere",
    team2: str = "FaZe Clan",
    *,
    starts_at: datetime | None = None,
    markets: list[CollectedMarket] | None = None,
    competition: str | None = "Counter-Strike 2 - IEM Chengdu",
    is_live: bool = False,
) -> CollectedEvent:
    return CollectedEvent(
        external_id=external_id,
        name=f"{team1} - {team2}",
        team1_raw=team1,
        team2_raw=team2,
        competition_raw=competition,
        starts_at=starts_at,
        url=f"https://www.betclic.pl/e-sport-s102/iem-c1/x-m{external_id}",
        is_live=is_live,
        markets=markets or [],
    )
