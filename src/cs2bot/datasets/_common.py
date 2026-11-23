"""Shared query helpers for the dataset loaders (private to :mod:`cs2bot.datasets`)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime
from typing import Any, Final, TypeVar

from sqlalchemy import ColumnElement, Select, func
from sqlalchemy.orm import Session

from cs2bot.db.models import MapPlayed, Match
from cs2bot.domain.enums import DataSource, MatchStatus, Tier
from cs2bot.utils.time import ensure_utc

T = TypeVar("T")

#: Preferred source first. Demo data is measured from the game itself, HLTV is a curated scoreboard,
#: PandaScore is an API feed, synthetic rows only exist in tests and demos.
SOURCE_PRIORITY: Final[tuple[DataSource, ...]] = (
    DataSource.DEMO,
    DataSource.HLTV,
    DataSource.PANDASCORE,
    DataSource.SYNTHETIC,
)
_SOURCE_RANK: Final[dict[DataSource, int]] = {s: i for i, s in enumerate(SOURCE_PRIORITY)}

#: SQLite's default limit of bound parameters is 999 (older builds); stay well below it.
IN_CHUNK: Final[int] = 500


def source_rank(source: DataSource | str | None) -> int:
    """Sort key: lower = more trusted. Unknown sources rank after every known one."""
    if source is None:
        return len(SOURCE_PRIORITY)
    try:
        return _SOURCE_RANK.get(DataSource(source), len(SOURCE_PRIORITY))
    except ValueError:
        return len(SOURCE_PRIORITY)


def map_time_expr() -> ColumnElement[datetime]:
    """When a map was played: ``maps_played.played_at``, else the match's scheduled start."""
    return func.coalesce(MapPlayed.played_at, Match.scheduled_at)


def apply_match_filters(
    stmt: Select[Any],
    *,
    time_expr: ColumnElement[datetime],
    since: datetime | None,
    until: datetime | None,
    in_scope_only: bool,
) -> Select[Any]:
    """Common WHERE clause: both teams known, not cancelled, ``since <= time < until``, scope."""
    stmt = stmt.where(
        Match.team1_id.is_not(None),
        Match.team2_id.is_not(None),
        Match.team1_id != Match.team2_id,
        Match.status != MatchStatus.CANCELED,
        time_expr.is_not(None),
    )
    if since is not None:
        stmt = stmt.where(time_expr >= ensure_utc(since))
    if until is not None:
        stmt = stmt.where(time_expr < ensure_utc(until))
    if in_scope_only:
        stmt = stmt.where(Match.in_scope.is_(True))
    return stmt


def resolve_tier(match_tier: Tier | None, tournament_tier: Tier | None) -> Tier:
    """The match's own tier, falling back to the tournament's when the match is unranked."""
    if match_tier is not None and match_tier is not Tier.UNRANKED:
        return match_tier
    if tournament_tier is not None:
        return tournament_tier
    return Tier.UNRANKED


def resolve_lan(match_is_lan: bool | None, tournament_is_lan: bool | None) -> bool:
    """LAN flag of the match, else of the tournament, else ``False`` (online is the common case)."""
    if match_is_lan is not None:
        return bool(match_is_lan)
    return bool(tournament_is_lan)


def chunks(items: Sequence[T], size: int = IN_CHUNK) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_in_chunks(
    session: Session,
    make_stmt: Callable[[Sequence[int]], Select[Any]],
    ids: Iterable[int],
    *,
    size: int = IN_CHUNK,
) -> Iterator[Any]:
    """Run ``make_stmt(chunk_of_ids)`` for every chunk and yield all rows (portable ``IN`` lists)."""
    unique = sorted(set(ids))
    for chunk in chunks(unique, size):
        yield from session.execute(make_stmt(chunk))
