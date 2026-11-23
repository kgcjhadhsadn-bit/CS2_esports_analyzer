"""Build the :class:`~cs2bot.domain.match.MatchContext` of one match and score how complete its data is.

``load_match_context`` is what the prediction service calls for every upcoming match;
``data_completeness`` gates betting (``decision.min_data_completeness``, PLAN.md section 6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.orm import Session

from cs2bot.config import DEFAULT_ACTIVE_MAP_POOL, Settings
from cs2bot.datasets._common import map_time_expr, resolve_lan, resolve_tier
from cs2bot.datasets.loaders import ACTIVE_DUTY_POOL_SIZE, _clean_map_name, load_map_results
from cs2bot.db.models import (
    BookmakerEvent,
    ContextFlagRow,
    MapPlayed,
    Match,
    MatchLineup,
    Player,
    PlayerMapStats,
    Team,
    Tournament,
    Veto,
)
from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import LinkStatus, MatchFormat, MatchStatus, Side, TeamSlot, VetoAction
from cs2bot.domain.match import LineupPlayer, MatchContext
from cs2bot.domain.records import VetoStep
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

#: Players in a CS2 lineup.
LINEUP_SIZE = 5
#: Window used to detect the map pool actually being played (see ``features.meta``).
POOL_WINDOW_DAYS = 60


class MatchNotFoundError(LookupError):
    """The match does not exist or has no two known teams yet (nothing to predict)."""


def _slot(team_id: int | None, team1_id: int, team2_id: int) -> TeamSlot | None:
    if team_id == team1_id:
        return TeamSlot.TEAM1
    if team_id == team2_id:
        return TeamSlot.TEAM2
    return None


def _lineup_rows(session: Session, match_id: int, team_id: int) -> list[LineupPlayer]:
    stmt = (
        select(MatchLineup.player_id, MatchLineup.is_standin, Player.nickname, Player.primary_role)
        .join(Player, Player.id == MatchLineup.player_id)
        .where(MatchLineup.match_id == match_id, MatchLineup.team_id == team_id)
        .order_by(MatchLineup.player_id)
    )
    return [
        LineupPlayer(player_id=pid, nickname=nick, is_standin=bool(standin), role=role)
        for pid, standin, nick, role in session.execute(stmt)
    ]


def _previous_lineup(session: Session, match: Match, team_id: int, before: datetime) -> list[LineupPlayer]:
    """The team's lineup in its most recent earlier match that has lineup rows (may be empty).

    Stand-in flags are carried over: if the team needed a stand-in last time, assuming the same five
    (including the stand-in) is the best guess until the real lineup is published.
    """
    stmt = (
        select(MatchLineup.match_id)
        .join(Match, Match.id == MatchLineup.match_id)
        .where(
            MatchLineup.team_id == team_id,
            Match.id != match.id,
            Match.status != MatchStatus.CANCELED,
            Match.scheduled_at.is_not(None),
            Match.scheduled_at < before,
        )
        .order_by(Match.scheduled_at.desc(), Match.id.desc())
        .limit(1)
    )
    previous = session.scalar(stmt)
    return _lineup_rows(session, previous, team_id) if previous is not None else []


def _infer_format(match: Match, vetoes: list[VetoStep], n_maps: int) -> MatchFormat:
    """``matches.format``, else the HLTV ``best_of`` hint, the veto shape, the map count, else BO3."""
    if match.format is not None:
        return match.format
    hltv = (match.extra or {}).get("hltv") or {}
    best_of = hltv.get("best_of")
    by_number = {1: MatchFormat.BO1, 2: MatchFormat.BO2, 3: MatchFormat.BO3, 5: MatchFormat.BO5}
    if isinstance(best_of, int) and best_of in by_number:
        return by_number[best_of]
    if len(vetoes) >= ACTIVE_DUTY_POOL_SIZE:
        picks = sum(1 for s in vetoes if s.action is VetoAction.PICK)
        return {0: MatchFormat.BO1, 2: MatchFormat.BO3, 4: MatchFormat.BO5}.get(picks, MatchFormat.BO3)
    if n_maps == 1:
        return MatchFormat.BO1
    if n_maps >= 4:
        return MatchFormat.BO5
    return MatchFormat.BO3  # by far the most common Tier 1/2 format


def _fallback_map_pool(session: Session, settings: Settings, as_of: datetime) -> list[str]:
    """The map pool of a match whose veto is not (fully) published yet.

    1. The pool actually played: in-scope results of the last 60 days before ``as_of``
       (``features.meta.detect_active_map_pool``), when that yields exactly seven maps.
    2. ``settings.scope.active_map_pool`` when the owner configured it (it differs from the built-in
       default).
    3. The Active Duty pool on that day according to ``config/patches.yaml`` - the built-in default
       of the settings is a constant, so it is wrong for older matches (backtests) and goes stale at
       every pool rotation, while the patch file is the maintained calendar.
    4. The built-in default of the settings.
    """
    # lazy import: cs2bot.features imports cs2bot.datasets
    from cs2bot.features.meta import detect_active_map_pool, load_patch_timeline

    recent = load_map_results(session, since=as_of - timedelta(days=POOL_WINDOW_DAYS), until=as_of)
    detected = detect_active_map_pool(recent, as_of, window_days=POOL_WINDOW_DAYS)
    if len(detected) == ACTIVE_DUTY_POOL_SIZE:
        return detected
    configured = sorted({normalize_map_name(m) for m in settings.scope.active_map_pool})
    if configured != sorted(normalize_map_name(m) for m in DEFAULT_ACTIVE_MAP_POOL):
        return configured
    try:
        calendar = load_patch_timeline().map_pool_at(as_of)
    except (OSError, ValueError) as exc:
        log.warning("patch timeline unavailable, using the configured map pool: %s", exc)
        calendar = None
    if calendar is not None and len(calendar) == ACTIVE_DUTY_POOL_SIZE:
        return sorted(calendar)
    return configured


def _active_flags(session: Session, match: Match, now: datetime) -> list[ContextFlag]:
    stmt = (
        select(ContextFlagRow)
        .where(
            ContextFlagRow.match_id == match.id,
            ContextFlagRow.active.is_(True),
            or_(ContextFlagRow.expires_at.is_(None), ContextFlagRow.expires_at > now),
        )
        .order_by(ContextFlagRow.id)
    )
    flags: list[ContextFlag] = []
    for row in session.scalars(stmt):
        try:
            flags.append(
                ContextFlag(
                    flag_type=row.flag_type,
                    team=_slot(row.team_id, match.team1_id or -1, match.team2_id or -1),
                    severity=min(max(row.severity, 0.0), 1.0),
                    confidence=min(max(row.confidence, 0.0), 1.0),
                    logit_shift=row.logit_shift,
                    veto=row.veto,
                    rationale=row.rationale,
                    evidence=[str(e) for e in (row.evidence or [])],
                    expires_at=row.expires_at,
                )
            )
        except ValueError:
            log.debug("load_match_context: skipped malformed context flag %s", row.id)
    return flags


def load_match_context(
    session: Session,
    match_id: int,
    *,
    settings: Settings,
    now: datetime | None = None,
    include_context_flags: bool = True,
) -> MatchContext:
    """Everything the model plane needs to know about one match before it starts.

    * **Lineups:** ``match_lineups`` of this match (stand-in flag, ``players.primary_role``). A team
      without lineup rows yet gets the lineup of its most recent earlier match (see
      :func:`_previous_lineup`); :func:`data_completeness` only gives full credit to confirmed ones.
    * **map_pool:** the seven maps of the published veto when it is complete; else the pool detected
      from in-scope maps of the last 60 days (``features.meta.detect_active_map_pool``) when that
      yields exactly seven maps; else ``settings.scope.active_map_pool`` (see
      :func:`_fallback_map_pool` for the role of ``config/patches.yaml`` while that setting is left
      at its built-in default). Always sorted.
    * **known_veto / first_veto_team:** ``vetoes`` rows in team1/team2 orientation; the first-veto
      team is the team of step 1 when that step has one.
    * **start_sides:** map name -> team1's starting side for maps whose side is already stored.
    * **context_flags:** active, unexpired ``context_flags`` rows of the match (set
      ``include_context_flags=False`` when the caller attaches fresh LLM flags itself).

    Raises :class:`MatchNotFoundError` when the match is missing or a team is still unknown.
    """
    now = ensure_utc(now) if now is not None else utcnow()
    match = session.get(Match, match_id)
    if match is None or match.team1_id is None or match.team2_id is None:
        raise MatchNotFoundError(f"match {match_id} not found or teams not known yet")
    team1_id, team2_id = match.team1_id, match.team2_id
    team1 = session.get(Team, team1_id)
    team2 = session.get(Team, team2_id)
    tournament = session.get(Tournament, match.tournament_id) if match.tournament_id is not None else None
    scheduled_at = match.scheduled_at or now
    history_end = min(scheduled_at, now)

    lineups: dict[int, list[LineupPlayer]] = {}
    for team_id in (team1_id, team2_id):
        rows = _lineup_rows(session, match.id, team_id)
        lineups[team_id] = rows or _previous_lineup(session, match, team_id, history_end)

    veto_rows = session.execute(
        select(Veto.step, Veto.team_id, Veto.action, Veto.map_name)
        .where(Veto.match_id == match.id)
        .order_by(Veto.step)
    ).all()
    known_veto: list[VetoStep] = []
    for step, veto_team_id, action, raw_map in veto_rows:
        map_name = _clean_map_name(raw_map)
        if map_name is None:
            continue
        known_veto.append(
            VetoStep(
                step=step, action=action, team=_slot(veto_team_id, team1_id, team2_id), map_name=map_name
            )
        )
    first_veto_team = known_veto[0].team if known_veto and known_veto[0].step == 1 else None

    map_rows = session.execute(
        select(MapPlayed.map_name, MapPlayed.team1_start_side)
        .where(MapPlayed.match_id == match.id)
        .order_by(MapPlayed.map_number)
    ).all()
    start_sides: dict[str, Side | None] = {}
    for raw_map, side in map_rows:
        map_name = _clean_map_name(raw_map)
        if map_name is not None and side is not None:
            start_sides[map_name] = side

    veto_maps = [s.map_name for s in known_veto]
    if len(veto_maps) == ACTIVE_DUTY_POOL_SIZE and len(set(veto_maps)) == ACTIVE_DUTY_POOL_SIZE:
        map_pool = sorted(veto_maps)
    else:
        map_pool = _fallback_map_pool(session, settings, history_end)

    return MatchContext(
        match_id=match.id,
        scheduled_at=scheduled_at,
        format=_infer_format(match, known_veto, len(map_rows)),
        team1_id=team1_id,
        team2_id=team2_id,
        team1_name=team1.name if team1 is not None else None,
        team2_name=team2.name if team2 is not None else None,
        tier=resolve_tier(match.tier, tournament.tier if tournament is not None else None),
        is_lan=resolve_lan(match.is_lan, tournament.is_lan if tournament is not None else None),
        tournament_name=tournament.name if tournament is not None else None,
        stage=match.stage,
        team1_lineup=lineups[team1_id],
        team2_lineup=lineups[team2_id],
        map_pool=map_pool,
        known_veto=known_veto,
        first_veto_team=first_veto_team,
        start_sides=start_sides,
        context_flags=_active_flags(session, match, now) if include_context_flags else [],
    )


# ------------------------------------------------------------------------------- data completeness


@dataclass(frozen=True)
class CompletenessConfig:
    """Weights (they sum to 1) and thresholds of :func:`data_completeness`.

    With the default betting gate of 0.95 every component must be (almost) fully satisfied: a missing
    odds link (-0.10), an unconfirmed lineup (-0.075 per team) or a team without recent maps
    (-0.125) each push the score below the gate on their own.
    """

    weight_lineups: float = 0.30  # 0.15 per team: >= 5 players in match_lineups for THIS match
    weight_recent_maps: float = 0.25  # 0.125 per team: finished maps in the recent window
    weight_veto_history: float = 0.15  # 0.075 per team: earlier matches with a stored veto
    weight_player_stats: float = 0.20  # 0.10 per team: lineup players with enough stat lines
    weight_odds_link: float = 0.10  # a Betclic event linked with status AUTO / CONFIRMED
    fallback_lineup_credit: float = 0.5  # 5 players known only from the team's previous match
    min_recent_maps: int = 8
    recent_maps_days: int = 90
    min_vetoes: int = 3
    veto_days: int = 180
    min_player_maps: int = 5
    player_days: int = 180


DEFAULT_COMPLETENESS = CompletenessConfig()


def _team_filter(team_id: int) -> Any:
    return or_(Match.team1_id == team_id, Match.team2_id == team_id)


def completeness_breakdown(
    session: Session,
    ctx: MatchContext,
    *,
    now: datetime | None = None,
    config: CompletenessConfig = DEFAULT_COMPLETENESS,
) -> dict[str, float]:
    """Component scores in [0, 1] behind :func:`data_completeness` (for the dashboard / debugging).

    Keys: ``lineups``, ``recent_maps``, ``veto_history``, ``player_stats``, ``odds_link``. History is
    counted strictly before ``min(ctx.scheduled_at, now)`` and never includes the match itself.
    """
    now = ensure_utc(now) if now is not None else utcnow()
    ref = min(ensure_utc(ctx.scheduled_at), now)
    teams = (
        (ctx.team1_id, ctx.team1_lineup),
        (ctx.team2_id, ctx.team2_lineup),
    )
    ts = map_time_expr()

    confirmed = dict(
        session.execute(
            select(MatchLineup.team_id, func.count())
            .where(MatchLineup.match_id == ctx.match_id)
            .group_by(MatchLineup.team_id)
        ).all()
    )
    lineup_scores: list[float] = []
    for team_id, lineup in teams:
        if confirmed.get(team_id, 0) >= LINEUP_SIZE:
            lineup_scores.append(1.0)
        elif len(lineup) >= LINEUP_SIZE:
            lineup_scores.append(config.fallback_lineup_credit)
        else:
            lineup_scores.append(0.0)

    recent_scores: list[float] = []
    veto_scores: list[float] = []
    for team_id, _ in teams:
        n_maps = session.scalar(
            select(func.count())
            .select_from(MapPlayed)
            .join(Match, Match.id == MapPlayed.match_id)
            .where(
                _team_filter(team_id),
                Match.id != ctx.match_id,
                Match.status != MatchStatus.CANCELED,
                MapPlayed.team1_rounds.is_not(None),
                MapPlayed.team2_rounds.is_not(None),
                ts >= ref - timedelta(days=config.recent_maps_days),
                ts < ref,
            )
        )
        recent_scores.append(min(1.0, (n_maps or 0) / max(1, config.min_recent_maps)))
        n_vetoes = session.scalar(
            select(func.count(distinct(Veto.match_id)))
            .select_from(Veto)
            .join(Match, Match.id == Veto.match_id)
            .where(
                _team_filter(team_id),
                Match.id != ctx.match_id,
                Match.scheduled_at >= ref - timedelta(days=config.veto_days),
                Match.scheduled_at < ref,
            )
        )
        veto_scores.append(min(1.0, (n_vetoes or 0) / max(1, config.min_vetoes)))

    player_ids = sorted({p.player_id for _, lineup in teams for p in lineup})
    maps_per_player: dict[int, int] = {}
    if player_ids:
        per_player_stmt = (
            select(PlayerMapStats.player_id, func.count(distinct(PlayerMapStats.map_played_id)))
            .join(MapPlayed, MapPlayed.id == PlayerMapStats.map_played_id)
            .join(Match, Match.id == MapPlayed.match_id)
            .where(
                PlayerMapStats.player_id.in_(player_ids),
                Match.id != ctx.match_id,
                ts >= ref - timedelta(days=config.player_days),
                ts < ref,
            )
            .group_by(PlayerMapStats.player_id)
        )
        maps_per_player = {int(pid): int(n) for pid, n in session.execute(per_player_stmt).all()}
    player_scores: list[float] = []
    for _, lineup in teams:
        known = sum(1 for p in lineup if maps_per_player.get(p.player_id, 0) >= config.min_player_maps)
        player_scores.append(min(1.0, known / LINEUP_SIZE))

    linked = session.scalar(
        select(func.count())
        .select_from(BookmakerEvent)
        .where(
            BookmakerEvent.match_id == ctx.match_id,
            BookmakerEvent.link_status.in_([LinkStatus.AUTO, LinkStatus.CONFIRMED]),
        )
    )
    return {
        "lineups": sum(lineup_scores) / 2,
        "recent_maps": sum(recent_scores) / 2,
        "veto_history": sum(veto_scores) / 2,
        "player_stats": sum(player_scores) / 2,
        "odds_link": 1.0 if linked else 0.0,
    }


def data_completeness(
    session: Session,
    ctx: MatchContext,
    *,
    now: datetime | None = None,
    config: CompletenessConfig = DEFAULT_COMPLETENESS,
) -> float:
    """How complete the data behind a prediction is, in [0, 1] (1 = everything we want is there).

    Weighted sum of five components (weights in :class:`CompletenessConfig`):

    ======================  ======  ==============================================================
    component               weight  full credit when
    ======================  ======  ==============================================================
    lineups                 0.30    both teams have >= 5 players in ``match_lineups`` for this
                                    match (half credit for a lineup carried over from the team's
                                    previous match)
    recent maps             0.25    each team has >= 8 finished maps in the last 90 days (linear)
    veto history            0.15    each team has >= 3 earlier matches with a veto in 180 days
    player stats            0.20    every lineup player has >= 5 stat lines in the last 180 days
    odds link               0.10    a bookmaker event is linked (AUTO / CONFIRMED) to the match
    ======================  ======  ==============================================================

    The result is not written anywhere; the caller stores it on ``matches.data_completeness`` /
    the prediction.
    """
    parts = completeness_breakdown(session, ctx, now=now, config=config)
    score = (
        config.weight_lineups * parts["lineups"]
        + config.weight_recent_maps * parts["recent_maps"]
        + config.weight_veto_history * parts["veto_history"]
        + config.weight_player_stats * parts["player_stats"]
        + config.weight_odds_link * parts["odds_link"]
    )
    total = (
        config.weight_lineups
        + config.weight_recent_maps
        + config.weight_veto_history
        + config.weight_player_stats
        + config.weight_odds_link
    )
    return float(min(1.0, max(0.0, score / total))) if total > 0 else 0.0
