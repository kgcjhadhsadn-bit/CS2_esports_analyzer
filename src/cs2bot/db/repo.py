"""Shared get-or-create helpers used by every ingestion source.

Entity resolution order for teams/players:
1. the source's external id (``pandascore_id`` / ``hltv_id``),
2. an exact normalised alias already registered (any source),
3. a fuzzy alias match above ``fuzzy_threshold`` (teams only, and only when unambiguous),
4. create a new row.

The resolved row gets the external id and alias attached, so later lookups hit step 1/2.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cs2bot.db.models import Match, Player, Team, TeamAlias, Tournament
from cs2bot.domain.enums import DataSource, Tier
from cs2bot.utils.names import normalize_player_name, normalize_team_name, team_name_similarity

_EXTERNAL_ID_FIELD = {DataSource.PANDASCORE: "pandascore_id", DataSource.HLTV: "hltv_id"}


def _ensure_alias(session: Session, team: Team, name: str, source: DataSource) -> None:
    alias = normalize_team_name(name)
    if not alias:
        return
    exists = session.scalar(select(TeamAlias).where(TeamAlias.alias == alias, TeamAlias.source == source))
    if exists is None:
        session.add(TeamAlias(team_id=team.id, alias=alias, source=source))
        session.flush()


def find_team_by_alias(session: Session, name: str) -> Team | None:
    alias = normalize_team_name(name)
    row = session.scalar(select(TeamAlias).where(TeamAlias.alias == alias).limit(1))
    return session.get(Team, row.team_id) if row is not None else None


def fuzzy_find_team(session: Session, name: str, *, threshold: float = 92.0) -> Team | None:
    """Best fuzzy alias match if it is above threshold and clearly better than the runner-up."""
    scored: dict[int, float] = {}
    for alias in session.scalars(select(TeamAlias)):
        score = team_name_similarity(name, alias.alias)
        if score > scored.get(alias.team_id, 0.0):
            scored[alias.team_id] = score
    if not scored:
        return None
    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    best_id, best = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best >= threshold and best - runner_up >= 3.0:
        return session.get(Team, best_id)
    return None


def resolve_team(
    session: Session,
    *,
    name: str,
    source: DataSource,
    external_id: int | None = None,
    fuzzy_threshold: float = 92.0,
    create: bool = True,
    **fields: Any,
) -> Team | None:
    """Find or create the canonical team row for a team seen in ``source``."""
    id_field = _EXTERNAL_ID_FIELD.get(source)
    team: Team | None = None
    if id_field and external_id is not None:
        team = session.scalar(select(Team).where(getattr(Team, id_field) == external_id))
    if team is None:
        team = find_team_by_alias(session, name)
        if team is not None and id_field and external_id is not None:
            current = getattr(team, id_field)
            if current is not None and current != external_id:
                team = None  # same name, different entity in this source (e.g. renamed/reused name)
    if team is None:
        candidate = fuzzy_find_team(session, name, threshold=fuzzy_threshold)
        if candidate is not None and not (
            id_field and external_id is not None and getattr(candidate, id_field) not in (None, external_id)
        ):
            team = candidate
    if team is None:
        if not create:
            return None
        team = Team(name=name, **fields)
        session.add(team)
        session.flush()
    if id_field and external_id is not None and getattr(team, id_field) is None:
        setattr(team, id_field, external_id)
    for key, value in fields.items():
        if value is not None and getattr(team, key, None) in (None, "", {}):
            setattr(team, key, value)
    session.flush()
    _ensure_alias(session, team, name, source)
    return team


def resolve_player(
    session: Session,
    *,
    nickname: str,
    source: DataSource,
    external_id: int | None = None,
    team_id: int | None = None,
    create: bool = True,
    update_current_team: bool = True,
    **fields: Any,
) -> Player | None:
    """Find or create a player. Nickname-only matching is restricted to the same current team.

    Pass ``update_current_team=False`` when processing OLD matches (backfills): the player is still
    matched/created with ``team_id`` but an existing ``current_team_id`` is left alone.
    """
    id_field = _EXTERNAL_ID_FIELD.get(source)
    player: Player | None = None
    if id_field and external_id is not None:
        player = session.scalar(select(Player).where(getattr(Player, id_field) == external_id))
    if player is None and team_id is not None:
        norm = normalize_player_name(nickname)
        for cand in session.scalars(select(Player).where(Player.current_team_id == team_id)):
            if normalize_player_name(cand.nickname) == norm and not (
                id_field and external_id is not None and getattr(cand, id_field) not in (None, external_id)
            ):
                player = cand
                break
    if player is None:
        if not create:
            return None
        player = Player(nickname=nickname, current_team_id=team_id, **fields)
        session.add(player)
        session.flush()
    if id_field and external_id is not None and getattr(player, id_field) is None:
        setattr(player, id_field, external_id)
    if team_id is not None and (update_current_team or player.current_team_id is None):
        player.current_team_id = team_id
    for key, value in fields.items():
        if value is not None and getattr(player, key, None) in (None, "", {}):
            setattr(player, key, value)
    session.flush()
    return player


def resolve_tournament(
    session: Session,
    *,
    name: str,
    source: DataSource,
    external_id: int | None = None,
    tier: Tier | None = None,
    **fields: Any,
) -> Tournament:
    id_field = {DataSource.PANDASCORE: "pandascore_id", DataSource.HLTV: "hltv_event_id"}.get(source)
    tour: Tournament | None = None
    if id_field and external_id is not None:
        tour = session.scalar(select(Tournament).where(getattr(Tournament, id_field) == external_id))
    if tour is None:
        tour = Tournament(name=name, tier=tier or Tier.UNRANKED, **fields)
        session.add(tour)
        session.flush()
    if id_field and external_id is not None and getattr(tour, id_field) is None:
        setattr(tour, id_field, external_id)
    if tier is not None and tour.tier == Tier.UNRANKED:
        tour.tier = tier
    for key, value in fields.items():
        if value is not None and getattr(tour, key, None) in (None, "", {}):
            setattr(tour, key, value)
    session.flush()
    return tour


def find_match(
    session: Session, team_a_id: int, team_b_id: int, around: datetime, *, window_hours: float = 12.0
) -> Match | None:
    """Existing match between two teams (either orientation) scheduled within +-window of ``around``.

    Used by every source (PandaScore, HLTV, Betclic linking) so the same real match is stored once.
    When several candidates exist, the one closest in time wins.
    """
    lo, hi = around - timedelta(hours=window_hours), around + timedelta(hours=window_hours)
    query = select(Match).where(
        or_(
            and_(Match.team1_id == team_a_id, Match.team2_id == team_b_id),
            and_(Match.team1_id == team_b_id, Match.team2_id == team_a_id),
        ),
        Match.scheduled_at >= lo,
        Match.scheduled_at <= hi,
    )
    candidates = [m for m in session.scalars(query) if m.scheduled_at is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda m: abs((m.scheduled_at - around).total_seconds()))  # type: ignore[operator]


def is_swapped(match: Match, source_first_team_id: int) -> bool:
    """True if the source lists our ``team2`` first (scores/sides/vetoes must then be swapped)."""
    return match.team2_id == source_first_team_id and match.team1_id != source_first_team_id
