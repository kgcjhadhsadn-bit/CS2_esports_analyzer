"""Loaders for demo-derived detail tables (kills, per-round player stats, position samples).

These tables are only filled for maps whose demo was parsed (``cs2bot.ingest.demos``). Everything
here degrades to "no rows" when they are empty, so callers can run before the first demo exists.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import String, and_, case, func, select, type_coerce
from sqlalchemy.orm import Session

from cs2bot.datasets._common import apply_match_filters, map_time_expr
from cs2bot.datasets.loaders import _clean_map_name
from cs2bot.db.models import Kill, MapPlayed, Match, PlayerPositionSample, PlayerRoundStats, Round
from cs2bot.domain.enums import DataSource
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

KILL_COLUMNS: Final[tuple[str, ...]] = (
    "map_played_id",
    "map_name",
    "round_number",
    "attacker_player_id",
    "victim_player_id",
    "attacker_side",
    "victim_side",
    "weapon",
    "is_opening",
    "seconds_into_round",
)
POSITION_COLUMNS: Final[tuple[str, ...]] = (
    "map_played_id",
    "map_name",
    "round_number",
    "player_id",
    "side",
    "seconds_into_round",
    "place",
    "x",
    "y",
    "z",
    "is_alive",
)
PLAYER_ROUND_COLUMNS: Final[tuple[str, ...]] = (
    "map_played_id",
    "round_number",
    "player_id",
    "side",
    "flashes_thrown",
    "enemies_flashed",
)


class TeamDemoMapRecord(BaseModel):
    """Demo-derived team totals on one map (trading and utility), in the team's own orientation."""

    model_config = ConfigDict(frozen=True)

    match_id: int
    map_number: int
    map_name: str
    played_at: datetime
    team_id: int
    opponent_team_id: int
    rounds: int
    kills: int
    deaths: int
    trade_kills: int  # kills that avenged a teammate inside the trade window
    traded_deaths: int  # own deaths that a teammate avenged inside the trade window
    utility_damage: float | None = None  # HE + molotov / incendiary damage dealt
    enemies_flashed: int | None = None


def load_team_demo_stats(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
    sources: Collection[DataSource] | None = (DataSource.DEMO,),
) -> list[TeamDemoMapRecord]:
    """Two records per demo-parsed map (one per team), chronological.

    Kills are attributed to a team through ``kills.attacker_side`` and the round's ``team1_side``
    (player ids may be NULL for unresolved steam ids, sides never are for real kills). Team kills and
    kills without sides are ignored. Only maps whose ``rounds`` rows come from ``sources`` are used
    (default: DEMO) because the trade flags of other origins are placeholders; pass ``sources=None``
    to accept every origin.
    """
    ts = map_time_expr()
    round_join = and_(Round.map_played_id == Kill.map_played_id, Round.round_number == Kill.round_number)
    a_side = type_coerce(Kill.attacker_side, String)
    v_side = type_coerce(Kill.victim_side, String)
    t1_side = type_coerce(Round.team1_side, String)
    attacker_t1 = a_side == t1_side

    def total(cond: Any) -> Any:
        return func.sum(case((cond, 1), else_=0))

    kill_stmt = (
        select(
            Kill.map_played_id,
            total(attacker_t1).label("t1_kills"),
            total(~attacker_t1).label("t2_kills"),
            total(and_(attacker_t1, Kill.is_trade.is_(True))).label("t1_trade_kills"),
            total(and_(~attacker_t1, Kill.is_trade.is_(True))).label("t2_trade_kills"),
            total(and_(~attacker_t1, Kill.was_traded.is_(True))).label("t1_traded_deaths"),
            total(and_(attacker_t1, Kill.was_traded.is_(True))).label("t2_traded_deaths"),
            MapPlayed.match_id,
            MapPlayed.map_number,
            MapPlayed.map_name,
            MapPlayed.team1_rounds,
            MapPlayed.team2_rounds,
            ts.label("played_at"),
            Match.team1_id,
            Match.team2_id,
        )
        .join(Round, round_join)
        .join(MapPlayed, MapPlayed.id == Kill.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .where(Kill.attacker_side.is_not(None), Kill.victim_side.is_not(None), a_side != v_side)
        .group_by(
            Kill.map_played_id,
            MapPlayed.match_id,
            MapPlayed.map_number,
            MapPlayed.map_name,
            MapPlayed.team1_rounds,
            MapPlayed.team2_rounds,
            ts,
            Match.team1_id,
            Match.team2_id,
        )
    )
    kill_stmt = apply_match_filters(
        kill_stmt, time_expr=ts, since=since, until=until, in_scope_only=in_scope_only
    )
    if sources is not None:
        kill_stmt = kill_stmt.where(Round.source.in_(list(sources)))
    kill_rows = list(session.execute(kill_stmt))
    if not kill_rows:
        return []

    prs_join = and_(
        Round.map_played_id == PlayerRoundStats.map_played_id,
        Round.round_number == PlayerRoundStats.round_number,
    )
    prs_t1 = type_coerce(PlayerRoundStats.side, String) == t1_side
    util_stmt = (
        select(
            PlayerRoundStats.map_played_id,
            func.sum(case((prs_t1, PlayerRoundStats.utility_damage), else_=0)),
            func.sum(case((~prs_t1, PlayerRoundStats.utility_damage), else_=0)),
            func.sum(case((prs_t1, PlayerRoundStats.enemies_flashed), else_=0)),
            func.sum(case((~prs_t1, PlayerRoundStats.enemies_flashed), else_=0)),
            func.count(func.distinct(PlayerRoundStats.round_number)),
        )
        .join(Round, prs_join)
        .join(MapPlayed, MapPlayed.id == PlayerRoundStats.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .group_by(PlayerRoundStats.map_played_id)
    )
    util_stmt = apply_match_filters(
        util_stmt, time_expr=ts, since=since, until=until, in_scope_only=in_scope_only
    )
    utility = {row[0]: row[1:] for row in session.execute(util_stmt)}

    out: list[TeamDemoMapRecord] = []
    for r in kill_rows:
        map_name = _clean_map_name(r.map_name)
        if map_name is None:
            continue
        util = utility.get(r.map_played_id)
        rounds = (r.team1_rounds or 0) + (r.team2_rounds or 0) or (int(util[4]) if util else 0)
        if rounds <= 0:
            continue
        sides = (
            (r.team1_id, r.team2_id, r.t1_kills, r.t2_kills, r.t1_trade_kills, r.t1_traded_deaths, 0),
            (r.team2_id, r.team1_id, r.t2_kills, r.t1_kills, r.t2_trade_kills, r.t2_traded_deaths, 1),
        )
        for team_id, opp_id, kills, deaths, trade_kills, traded_deaths, idx in sides:
            try:
                out.append(
                    TeamDemoMapRecord(
                        match_id=r.match_id,
                        map_number=r.map_number,
                        map_name=map_name,
                        played_at=ensure_utc(r.played_at),
                        team_id=team_id,
                        opponent_team_id=opp_id,
                        rounds=rounds,
                        kills=int(kills or 0),
                        deaths=int(deaths or 0),
                        trade_kills=int(trade_kills or 0),
                        traded_deaths=int(traded_deaths or 0),
                        utility_damage=float(util[idx] or 0) if util else None,
                        enemies_flashed=int(util[2 + idx] or 0) if util else None,
                    )
                )
            except ValidationError:
                log.debug("load_team_demo_stats: skipped malformed map %s", r.map_played_id)
    out.sort(key=lambda t: (t.played_at, t.match_id, t.map_number, t.team_id))
    return out


# ------------------------------------------------------------------------------------ role frames


def _empty(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


@dataclass
class RoleFrames:
    """Raw material for :func:`cs2bot.features.roles.infer_roles` (pandas, sides as ``"ct"``/``"t"``).

    * ``kills``: :data:`KILL_COLUMNS` - one row per kill.
    * ``positions``: :data:`POSITION_COLUMNS` - one row per position sample.
    * ``player_rounds``: :data:`PLAYER_ROUND_COLUMNS` - one row per (map, round, player) played; used
      for per-round rates. Optional: when empty, rounds are counted from the position samples.

    Hand-built frames (tests, notebooks) may omit the optional columns ``z``, ``flashes_thrown`` and
    ``enemies_flashed``.
    """

    kills: pd.DataFrame = field(default_factory=lambda: _empty(KILL_COLUMNS))
    positions: pd.DataFrame = field(default_factory=lambda: _empty(POSITION_COLUMNS))
    player_rounds: pd.DataFrame = field(default_factory=lambda: _empty(PLAYER_ROUND_COLUMNS))


def available_position_seconds(session: Session, *, sample_rows: int = 20_000) -> list[int]:
    """Sample offsets (seconds after freeze time) present in the newest position samples.

    The demo pipeline stores one sample per player per round for each of
    ``settings.demos.position_sample_seconds``. Reading the newest ``sample_rows`` rows (a few maps)
    is enough to see which offsets exist without scanning the whole table.
    """
    newest = (
        select(PlayerPositionSample.seconds_into_round)
        .order_by(PlayerPositionSample.id.desc())
        .limit(sample_rows)
        .subquery()
    )
    return sorted(int(s) for s in session.scalars(select(newest.c.seconds_into_round).distinct()))


def load_role_frames(
    session: Session,
    *,
    window_days: int = 120,
    as_of: datetime | None = None,
    position_seconds: Collection[int] | None = None,
    in_scope_only: bool = False,
) -> RoleFrames:
    """Kills, position samples and played rounds of maps in ``[as_of - window_days, as_of)``.

    ``position_seconds`` restricts the position samples to those offsets after freeze time (the
    table holds several per round; role inference needs one or two), which keeps the frame small.
    """
    end = ensure_utc(as_of) if as_of is not None else utcnow()
    start = end - timedelta(days=window_days)
    ts = map_time_expr()

    kill_stmt = (
        select(
            Kill.map_played_id,
            MapPlayed.map_name,
            Kill.round_number,
            Kill.attacker_player_id,
            Kill.victim_player_id,
            type_coerce(Kill.attacker_side, String),
            type_coerce(Kill.victim_side, String),
            Kill.weapon,
            Kill.is_opening,
            Kill.seconds_into_round,
        )
        .join(MapPlayed, MapPlayed.id == Kill.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
    )
    kill_stmt = apply_match_filters(
        kill_stmt, time_expr=ts, since=start, until=end, in_scope_only=in_scope_only
    )
    kills = pd.DataFrame(session.execute(kill_stmt).all(), columns=list(KILL_COLUMNS))

    pos_stmt = (
        select(
            PlayerPositionSample.map_played_id,
            MapPlayed.map_name,
            PlayerPositionSample.round_number,
            PlayerPositionSample.player_id,
            type_coerce(PlayerPositionSample.side, String),
            PlayerPositionSample.seconds_into_round,
            PlayerPositionSample.place,
            PlayerPositionSample.x,
            PlayerPositionSample.y,
            PlayerPositionSample.z,
            PlayerPositionSample.is_alive,
        )
        .join(MapPlayed, MapPlayed.id == PlayerPositionSample.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
    )
    pos_stmt = apply_match_filters(
        pos_stmt, time_expr=ts, since=start, until=end, in_scope_only=in_scope_only
    )
    if position_seconds is not None:
        pos_stmt = pos_stmt.where(PlayerPositionSample.seconds_into_round.in_(sorted(position_seconds)))
    positions = pd.DataFrame(session.execute(pos_stmt).all(), columns=list(POSITION_COLUMNS))

    prs_stmt = (
        select(
            PlayerRoundStats.map_played_id,
            PlayerRoundStats.round_number,
            PlayerRoundStats.player_id,
            type_coerce(PlayerRoundStats.side, String),
            PlayerRoundStats.flashes_thrown,
            PlayerRoundStats.enemies_flashed,
        )
        .join(MapPlayed, MapPlayed.id == PlayerRoundStats.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
    )
    prs_stmt = apply_match_filters(
        prs_stmt, time_expr=ts, since=start, until=end, in_scope_only=in_scope_only
    )
    player_rounds = pd.DataFrame(session.execute(prs_stmt).all(), columns=list(PLAYER_ROUND_COLUMNS))
    return RoleFrames(kills=kills, positions=positions, player_rounds=player_rounds)
