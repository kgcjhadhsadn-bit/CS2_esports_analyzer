"""Team page: recent results, form, map statistics, players."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from cs2bot.api.deps import DB, Ctx, require_auth
from cs2bot.api.formatting import round_or_none, utc
from cs2bot.api.params import IdPath
from cs2bot.api.queries import match_brief, match_load_options, team_ref
from cs2bot.api.schemas import TeamDetail, TeamMapResult, TeamMapStat, TeamOut, TeamPlayerOut, TeamResult
from cs2bot.db.models import MapPlayed, Match, Player, PlayerMapStats, Team, Veto
from cs2bot.domain.enums import DataSource, MatchStatus, VetoAction
from cs2bot.utils.time import ensure_utc

router = APIRouter(prefix="/api/teams", tags=["teams"], dependencies=[Depends(require_auth)])

# When several sources report the same player/map line, prefer the most detailed one.
_SOURCE_PRIORITY: dict[DataSource, int] = {
    DataSource.HLTV: 0,
    DataSource.DEMO: 1,
    DataSource.PANDASCORE: 2,
    DataSource.MANUAL: 3,
    DataSource.SYNTHETIC: 4,
}


def _result(team_score: int | None, opp_score: int | None, won: bool | None) -> Literal["W", "L", "D"] | None:
    if won is True:
        return "W"
    if won is False:
        return "L"
    if team_score is not None and opp_score is not None and team_score == opp_score:
        return "D"
    return None


@dataclass
class _MapAcc:
    played: int = 0
    won: int = 0
    rounds_for: int = 0
    rounds_against: int = 0
    diffs: list[int] = field(default_factory=list)
    picked: int = 0
    picked_by_opponent: int = 0
    deciders: int = 0
    bans: int = 0
    opponent_bans: int = 0
    last_played_at: datetime | None = None


@router.get("/{team_id}", response_model=TeamDetail)
def get_team(
    team_id: IdPath,
    ctx: Ctx,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100, description="Recent results to return")] = 20,
    window_days: Annotated[int, Query(ge=1, le=3650, description="Window for map/player statistics")] = 180,
) -> TeamDetail:
    team = db.scalar(select(Team).where(Team.id == team_id).options(selectinload(Team.aliases)))
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")
    now = ctx.now()
    since = now - timedelta(days=window_days)
    involves = or_(Match.team1_id == team_id, Match.team2_id == team_id)

    recent = list(
        db.scalars(
            select(Match)
            .where(involves, Match.status == MatchStatus.FINISHED)
            .order_by(Match.scheduled_at.is_(None), Match.scheduled_at.desc(), Match.id.desc())
            .limit(limit)
            .options(*match_load_options(), selectinload(Match.maps))
        )
    )
    results: list[TeamResult] = []
    for m in recent:
        is_team1 = m.team1_id == team_id
        own, opp = (m.team1_score, m.team2_score) if is_team1 else (m.team2_score, m.team1_score)
        won = None if m.winner_team_id is None else m.winner_team_id == team_id
        results.append(
            TeamResult(
                match_id=m.id,
                scheduled_at=utc(m.scheduled_at),
                opponent=team_ref(m.team2 if is_team1 else m.team1),
                tournament=m.tournament.name if m.tournament is not None else None,
                tier=m.tier,
                format=m.format,
                team_score=own,
                opponent_score=opp,
                result=_result(own, opp, won),
                maps=[
                    TeamMapResult(
                        map_number=mp.map_number,
                        map_name=mp.map_name,
                        team_rounds=mp.team1_rounds if is_team1 else mp.team2_rounds,
                        opponent_rounds=mp.team2_rounds if is_team1 else mp.team1_rounds,
                        won=None if mp.winner_team_id is None else mp.winner_team_id == team_id,
                    )
                    for mp in sorted(m.maps, key=lambda x: x.map_number)
                ],
            )
        )
    form: list[Literal["W", "L", "D"]] = [r.result for r in results[:10] if r.result is not None]

    upcoming = [
        match_brief(m)
        for m in db.scalars(
            select(Match)
            .where(
                involves,
                Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.LIVE]),
                Match.scheduled_at >= now - timedelta(hours=6),
            )
            .order_by(Match.scheduled_at, Match.id)
            .limit(5)
            .options(*match_load_options())
        )
    ]

    played_at = func.coalesce(MapPlayed.played_at, Match.scheduled_at)
    maps: dict[str, _MapAcc] = defaultdict(_MapAcc)
    for mp, m in db.execute(
        select(MapPlayed, Match)
        .join(Match, MapPlayed.match_id == Match.id)
        .where(involves, MapPlayed.winner_team_id.is_not(None), played_at >= since)
    ):
        acc = maps[mp.map_name]
        is_team1 = m.team1_id == team_id
        own = (mp.team1_rounds if is_team1 else mp.team2_rounds) or 0
        opp = (mp.team2_rounds if is_team1 else mp.team1_rounds) or 0
        acc.played += 1
        acc.won += int(mp.winner_team_id == team_id)
        acc.rounds_for += own
        acc.rounds_against += opp
        acc.diffs.append(own - opp)
        if mp.picked_by_team_id is not None:
            if mp.picked_by_team_id == team_id:
                acc.picked += 1
            else:
                acc.picked_by_opponent += 1
        acc.deciders += int(bool(mp.is_decider))
        when = utc(mp.played_at) or utc(m.scheduled_at)
        if when is not None and (acc.last_played_at is None or when > acc.last_played_at):
            acc.last_played_at = when
    for veto_team, map_name in db.execute(
        select(Veto.team_id, Veto.map_name)
        .join(Match, Veto.match_id == Match.id)
        .where(involves, Veto.action == VetoAction.BAN, Match.scheduled_at >= since)
    ):
        acc = maps[map_name]
        if veto_team == team_id:
            acc.bans += 1
        elif veto_team is not None:
            acc.opponent_bans += 1
    map_stats = [
        TeamMapStat(
            map_name=name,
            played=acc.played,
            won=acc.won,
            win_rate=round(acc.won / acc.played, 4) if acc.played else None,
            rounds_for=acc.rounds_for,
            rounds_against=acc.rounds_against,
            avg_round_diff=round(sum(acc.diffs) / len(acc.diffs), 2) if acc.diffs else None,
            picked=acc.picked,
            picked_by_opponent=acc.picked_by_opponent,
            deciders=acc.deciders,
            bans=acc.bans,
            opponent_bans=acc.opponent_bans,
            last_played_at=acc.last_played_at,
        )
        for name, acc in sorted(maps.items(), key=lambda kv: (-kv[1].played, kv[0]))
    ]

    roster = list(
        db.scalars(select(Player).where(Player.current_team_id == team_id).order_by(Player.nickname))
    )
    best: dict[tuple[int, int], tuple[PlayerMapStats, datetime | None]] = {}
    if roster:
        for stat, mp_played_at, m_scheduled in db.execute(
            select(PlayerMapStats, MapPlayed.played_at, Match.scheduled_at)
            .join(MapPlayed, PlayerMapStats.map_played_id == MapPlayed.id)
            .join(Match, MapPlayed.match_id == Match.id)
            .where(
                PlayerMapStats.player_id.in_([p.id for p in roster]),
                PlayerMapStats.team_id == team_id,
                played_at >= since,
            )
        ):
            key = (stat.map_played_id, stat.player_id)
            current = best.get(key)
            if current is None or _SOURCE_PRIORITY.get(stat.source, 9) < _SOURCE_PRIORITY.get(
                current[0].source, 9
            ):
                best[key] = (stat, utc(mp_played_at) or utc(m_scheduled))
    per_player: dict[int, list[tuple[PlayerMapStats, datetime | None]]] = defaultdict(list)
    for (_, player_id), value in best.items():
        per_player[player_id].append(value)
    players: list[TeamPlayerOut] = []
    for p in roster:
        lines = per_player.get(p.id, [])
        kills = sum(s.kills for s, _ in lines)
        deaths = sum(s.deaths for s, _ in lines)
        rounds = sum(s.rounds_played or 0 for s, _ in lines)
        ratings = [s.rating for s, _ in lines if s.rating is not None]
        adrs = [s.adr for s, _ in lines if s.adr is not None]
        last = max((ts for _, ts in lines if ts is not None), default=None)
        players.append(
            TeamPlayerOut(
                id=p.id,
                nickname=p.nickname,
                real_name=p.real_name,
                country=p.country,
                primary_role=p.primary_role,
                role_confidence=p.role_confidence,
                hltv_id=p.hltv_id,
                maps=len(lines),
                kills=kills,
                deaths=deaths,
                kd=round(kills / deaths, 3) if deaths else None,
                kpr=round(kills / rounds, 3) if rounds else None,
                avg_rating=round_or_none(sum(ratings) / len(ratings), 3) if ratings else None,
                avg_adr=round_or_none(sum(adrs) / len(adrs), 1) if adrs else None,
                last_played_at=ensure_utc(last) if last is not None else None,
            )
        )
    return TeamDetail(
        team=TeamOut(
            id=team.id,
            name=team.name,
            slug=team.slug,
            country=team.country,
            logo_url=team.logo_url,
            hltv_rank=team.hltv_rank,
            hltv_rank_updated_at=utc(team.hltv_rank_updated_at),
            pandascore_id=team.pandascore_id,
            hltv_id=team.hltv_id,
            aliases=sorted({a.alias for a in team.aliases}),
        ),
        window_days=window_days,
        form=form,
        recent_results=results,
        upcoming=upcoming,
        maps=map_stats,
        players=players,
    )
