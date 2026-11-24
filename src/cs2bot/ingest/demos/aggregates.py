"""Rolling aggregates over parsed demos: where players stand and who beats whom.

Both functions summarise the maps *played in* ``[as_of - window_days, as_of)`` - the ``as_of`` day
itself is excluded, so rows with ``window_end == D`` only contain information that was available
before day ``D`` started (UTC) and can be used for any match on ``D`` without look-ahead. A map's
date is ``maps_played.played_at``, falling back to ``matches.scheduled_at``.

Rows of the same ``window_end`` are replaced, so rebuilding is idempotent. Rows of other
``window_end`` values are kept (walk-forward backtests read them); pass ``prune_before`` to delete
old ones.

``positions_agg`` (one row per player / map / side / place):

* ``rounds_sampled`` - rounds on that map and side in which the player was alive at the sample
  second (the denominator, identical for all places of that player / map / side);
* ``presence_share`` - share of those rounds in which the player stood in this place at the sample
  second (the shares of a player / map / side add up to 1 when every sample has a place name; a
  sample without one still counts in ``rounds_sampled`` but in no place, so the sum is then below 1);
* ``kills`` / ``deaths`` / ``opening_kills`` / ``opening_deaths`` - enemy kills made from, and deaths
  suffered in, this place at any time of the round (team kills and suicides are ignored).

``duels_agg`` (one row per ordered pair of players per map, plus ``map_name == "*"`` for all maps):
``kills`` = times ``player`` killed ``opponent``, ``deaths`` = the reverse, likewise for opening
kills. Each pair therefore appears twice, once from each player's point of view.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import ColumnElement, and_, case, delete, func, select
from sqlalchemy.orm import Session

from cs2bot.db.models import (
    DuelAggregate,
    Kill,
    MapPlayed,
    Match,
    PlayerPositionSample,
    PositionAggregate,
)
from cs2bot.domain.enums import Side
from cs2bot.utils.time import utcnow

ALL_MAPS = "*"


def _window(as_of: date | None, window_days: int) -> tuple[date, date, datetime, datetime]:
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    end = as_of or utcnow().date()
    start = end - timedelta(days=window_days)
    return (
        start,
        end,
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end, time.min, tzinfo=UTC),
    )


def _in_window(lo: datetime, hi: datetime) -> ColumnElement[bool]:
    played = func.coalesce(MapPlayed.played_at, Match.scheduled_at)
    return and_(played >= lo, played < hi)


@dataclass
class _PlaceStats:
    samples: int = 0
    kills: int = 0
    deaths: int = 0
    opening_kills: int = 0
    opening_deaths: int = 0


def rebuild_position_aggregates(
    session: Session,
    *,
    window_days: int = 120,
    sample_second: int = 20,
    as_of: date | None = None,
    prune_before: date | None = None,
) -> int:
    """Rebuild ``positions_agg`` for ``window_end == as_of`` (default: today, UTC); returns the row count.

    ``sample_second`` selects which position sample defines "where the player plays" (it must be one
    of ``settings.demos.position_sample_seconds``; 20 s after freeze end is when default setups are
    taken but before most fights).
    """
    start, end, lo, hi = _window(as_of, window_days)
    stats: dict[tuple[int, str, Side, str], _PlaceStats] = defaultdict(_PlaceStats)
    totals: dict[tuple[int, str, Side], int] = defaultdict(int)

    samples = session.execute(
        select(
            PlayerPositionSample.player_id,
            MapPlayed.map_name,
            PlayerPositionSample.side,
            PlayerPositionSample.place,
            func.count(),
        )
        .join(MapPlayed, MapPlayed.id == PlayerPositionSample.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .where(
            _in_window(lo, hi),
            PlayerPositionSample.seconds_into_round == sample_second,
            PlayerPositionSample.is_alive.is_(True),
        )
        .group_by(
            PlayerPositionSample.player_id,
            MapPlayed.map_name,
            PlayerPositionSample.side,
            PlayerPositionSample.place,
        )
    )
    for player_id, map_name, side, place, count in samples:
        totals[(player_id, map_name, side)] += count
        if place:
            stats[(player_id, map_name, side, place)].samples += count

    # A kill by / of a player without a players row still counts for the other, linked, player.
    enemy_kill = and_(
        Kill.attacker_side.is_not(None),
        Kill.victim_side.is_not(None),
        Kill.attacker_side != Kill.victim_side,
    )
    kills = session.execute(
        select(
            Kill.attacker_player_id,
            MapPlayed.map_name,
            Kill.attacker_side,
            Kill.attacker_place,
            Kill.is_opening,
        )
        .join(MapPlayed, MapPlayed.id == Kill.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .where(
            _in_window(lo, hi),
            enemy_kill,
            Kill.attacker_player_id.is_not(None),
            Kill.attacker_place.is_not(None),
        )
    )
    for killer_id, kill_map, killer_side, killer_place, is_opening in kills:
        if killer_id is None or killer_side is None or not killer_place:
            continue
        entry = stats[(killer_id, kill_map, killer_side, killer_place)]
        entry.kills += 1
        entry.opening_kills += int(bool(is_opening))
    deaths = session.execute(
        select(
            Kill.victim_player_id, MapPlayed.map_name, Kill.victim_side, Kill.victim_place, Kill.is_opening
        )
        .join(MapPlayed, MapPlayed.id == Kill.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .where(
            _in_window(lo, hi), enemy_kill, Kill.victim_player_id.is_not(None), Kill.victim_place.is_not(None)
        )
    )
    for victim_id, death_map, victim_side, victim_place, is_opening in deaths:
        if victim_id is None or victim_side is None or not victim_place:
            continue
        entry = stats[(victim_id, death_map, victim_side, victim_place)]
        entry.deaths += 1
        entry.opening_deaths += int(bool(is_opening))

    session.execute(
        delete(PositionAggregate)
        .where(PositionAggregate.window_end == end)
        .execution_options(synchronize_session="fetch")
    )
    if prune_before is not None:
        session.execute(
            delete(PositionAggregate)
            .where(PositionAggregate.window_end < prune_before)
            .execution_options(synchronize_session="fetch")
        )
    for (player_id, map_name, side, place), entry in sorted(
        stats.items(), key=lambda item: (item[0][0], item[0][1], item[0][2].value, item[0][3])
    ):
        total = totals.get((player_id, map_name, side), 0)
        session.add(
            PositionAggregate(
                player_id=player_id,
                map_name=map_name,
                side=side,
                place=place[:48],
                window_start=start,
                window_end=end,
                rounds_sampled=total,
                presence_share=entry.samples / total if total else 0.0,
                kills=entry.kills,
                deaths=entry.deaths,
                opening_kills=entry.opening_kills,
                opening_deaths=entry.opening_deaths,
            )
        )
    session.flush()
    return len(stats)


@dataclass
class _DuelStats:
    kills: int = 0
    deaths: int = 0
    opening_kills: int = 0
    opening_deaths: int = 0


def rebuild_duel_aggregates(
    session: Session,
    *,
    window_days: int = 120,
    as_of: date | None = None,
    prune_before: date | None = None,
) -> int:
    """Rebuild ``duels_agg`` for ``window_end == as_of`` (default: today, UTC); returns the row count."""
    _start, end, lo, hi = _window(as_of, window_days)
    stats: dict[tuple[int, int, str], _DuelStats] = defaultdict(_DuelStats)
    rows = session.execute(
        select(
            Kill.attacker_player_id,
            Kill.victim_player_id,
            MapPlayed.map_name,
            func.count(),
            func.sum(case((Kill.is_opening.is_(True), 1), else_=0)),
        )
        .join(MapPlayed, MapPlayed.id == Kill.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .where(
            _in_window(lo, hi),
            Kill.attacker_player_id.is_not(None),
            Kill.victim_player_id.is_not(None),
            Kill.attacker_player_id != Kill.victim_player_id,
            Kill.attacker_side.is_not(None),
            Kill.victim_side.is_not(None),
            Kill.attacker_side != Kill.victim_side,
        )
        .group_by(Kill.attacker_player_id, Kill.victim_player_id, MapPlayed.map_name)
    )
    for attacker, victim, map_name, count, openings in rows:
        opening = int(openings or 0)
        for key in (map_name, ALL_MAPS):
            forward = stats[(attacker, victim, key)]
            forward.kills += count
            forward.opening_kills += opening
            backward = stats[(victim, attacker, key)]
            backward.deaths += count
            backward.opening_deaths += opening

    session.execute(
        delete(DuelAggregate)
        .where(DuelAggregate.window_end == end)
        .execution_options(synchronize_session="fetch")
    )
    if prune_before is not None:
        session.execute(
            delete(DuelAggregate)
            .where(DuelAggregate.window_end < prune_before)
            .execution_options(synchronize_session="fetch")
        )
    for (player_id, opponent_id, map_name), entry in sorted(stats.items()):
        session.add(
            DuelAggregate(
                player_id=player_id,
                opponent_player_id=opponent_id,
                map_name=map_name,
                window_end=end,
                kills=entry.kills,
                deaths=entry.deaths,
                opening_kills=entry.opening_kills,
                opening_deaths=entry.opening_deaths,
            )
        )
    session.flush()
    return len(stats)
