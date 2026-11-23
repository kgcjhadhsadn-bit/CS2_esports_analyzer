"""Matches: list, detail (lineups, vetoes, maps, flags, analysis, markets table), odds history."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from cs2bot.api.analytics import downsample
from cs2bot.api.deps import DB, Ctx, Paging, require_auth
from cs2bot.api.ev import TaxTerms
from cs2bot.api.formatting import describe_outcome, market_sort_key, odds_str, parse_spec_key, utc
from cs2bot.api.params import (
    MAX_DB_INT,
    FromParam,
    IdPath,
    IdQuery,
    ListParam,
    ToParam,
    as_utc,
    parse_enum_list,
)
from cs2bot.api.queries import (
    ACTIVE_LINK_STATUSES,
    build_market_rows,
    event_ref,
    events_by_match,
    match_load_options,
    match_summaries,
    player_ids_in,
    player_names,
    team_names,
    unmapped_selection_count,
)
from cs2bot.api.schemas import (
    AnalysisOut,
    ContextFlagOut,
    LineupPlayerOut,
    LineupsOut,
    MapOut,
    MatchDetail,
    MatchPage,
    OddsHistoryOut,
    OddsPoint,
    OddsSeries,
    VetoOut,
)
from cs2bot.db.models import (
    BookmakerEvent,
    BookmakerMarket,
    BookmakerSelection,
    ContextFlagRow,
    Match,
    MatchAnalysis,
    OddsSnapshot,
    Player,
)
from cs2bot.domain.enums import LinkStatus, MatchStatus, TeamSlot
from cs2bot.utils.time import ensure_utc

router = APIRouter(prefix="/api/matches", tags=["matches"], dependencies=[Depends(require_auth)])

_EVENT_ORDER = {
    LinkStatus.CONFIRMED: 0,
    LinkStatus.AUTO: 1,
    LinkStatus.PENDING_REVIEW: 2,
    LinkStatus.UNLINKED: 3,
    LinkStatus.REJECTED: 4,
}


def _slot(match: Match, team_id: int | None) -> TeamSlot | None:
    if team_id is None:
        return None
    if team_id == match.team1_id:
        return TeamSlot.TEAM1
    if team_id == match.team2_id:
        return TeamSlot.TEAM2
    return None


@router.get("", response_model=MatchPage)
def list_matches(
    ctx: Ctx,
    db: DB,
    page: Paging,
    status: ListParam = None,
    from_: FromParam = None,
    to: ToParam = None,
    in_scope: bool | None = None,
    linked: Annotated[
        bool | None, Query(description="true: has an auto/confirmed Betclic link; false: has none")
    ] = None,
    team_id: IdQuery = None,
    order: Literal["asc", "desc"] = "asc",
) -> MatchPage:
    stmt = select(Match)
    statuses = parse_enum_list(status, MatchStatus, "status")
    if statuses:
        stmt = stmt.where(Match.status.in_(statuses))
    lower, upper = as_utc(from_), as_utc(to)
    if lower is not None:
        stmt = stmt.where(Match.scheduled_at >= lower)
    if upper is not None:
        stmt = stmt.where(Match.scheduled_at <= upper)
    if in_scope is not None:
        stmt = stmt.where(Match.in_scope.is_(in_scope))
    if team_id is not None:
        stmt = stmt.where(or_(Match.team1_id == team_id, Match.team2_id == team_id))
    if linked is not None:
        linked_ids = select(BookmakerEvent.match_id).where(
            BookmakerEvent.match_id.is_not(None), BookmakerEvent.link_status.in_(list(ACTIVE_LINK_STATUSES))
        )
        stmt = stmt.where(Match.id.in_(linked_ids) if linked else Match.id.not_in(linked_ids))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    when = Match.scheduled_at.asc() if order == "asc" else Match.scheduled_at.desc()
    tie = Match.id.asc() if order == "asc" else Match.id.desc()
    rows = list(
        db.scalars(
            stmt.order_by(Match.scheduled_at.is_(None), when, tie)
            .limit(page.limit)
            .offset(page.offset)
            .options(*match_load_options())
        )
    )
    items = match_summaries(db, rows, TaxTerms.from_settings(ctx.settings))
    return MatchPage(total=total, limit=page.limit, offset=page.offset, items=items)


@router.get("/{match_id}", response_model=MatchDetail)
def get_match(match_id: IdPath, ctx: Ctx, db: DB) -> MatchDetail:
    match = db.scalar(
        select(Match)
        .where(Match.id == match_id)
        .options(
            *match_load_options(),
            selectinload(Match.maps),
            selectinload(Match.vetoes),
            selectinload(Match.lineups),
        )
    )
    if match is None:
        raise HTTPException(status_code=404, detail="match not found")
    settings = ctx.settings
    tax = TaxTerms.from_settings(settings)
    summary = match_summaries(db, [match], tax)[0]

    players: dict[int, Player] = {
        p.id: p
        for p in db.scalars(select(Player).where(Player.id.in_([lu.player_id for lu in match.lineups])))
    }
    lineups = LineupsOut()
    for lu in sorted(
        match.lineups,
        key=lambda x: (x.is_standin, players[x.player_id].nickname.lower() if x.player_id in players else ""),
    ):
        player = players.get(lu.player_id)
        out = LineupPlayerOut(
            player_id=lu.player_id,
            nickname=player.nickname if player is not None else None,
            is_standin=bool(lu.is_standin),
            role=player.primary_role if player is not None else None,
        )
        slot = _slot(match, lu.team_id)
        if slot is TeamSlot.TEAM1:
            lineups.team1.append(out)
        elif slot is TeamSlot.TEAM2:
            lineups.team2.append(out)

    vetoes = [
        VetoOut(
            step=v.step, team_id=v.team_id, team=_slot(match, v.team_id), action=v.action, map_name=v.map_name
        )
        for v in sorted(match.vetoes, key=lambda v: v.step)
    ]
    maps = [
        MapOut(
            id=mp.id,
            map_number=mp.map_number,
            map_name=mp.map_name,
            status=mp.status,
            team1_rounds=mp.team1_rounds,
            team2_rounds=mp.team2_rounds,
            winner_team_id=mp.winner_team_id,
            winner=_slot(match, mp.winner_team_id),
            team1_start_side=mp.team1_start_side,
            team1_ct_rounds=mp.team1_ct_rounds,
            team1_t_rounds=mp.team1_t_rounds,
            team2_ct_rounds=mp.team2_ct_rounds,
            team2_t_rounds=mp.team2_t_rounds,
            overtime_rounds=mp.overtime_rounds or 0,
            picked_by_team_id=mp.picked_by_team_id,
            picked_by=_slot(match, mp.picked_by_team_id),
            is_decider=bool(mp.is_decider),
            played_at=utc(mp.played_at),
        )
        for mp in sorted(match.maps, key=lambda m: m.map_number)
    ]
    flags = [
        ContextFlagOut(
            id=f.id,
            team_id=f.team_id,
            team=_slot(match, f.team_id),
            flag_type=f.flag_type,
            severity=f.severity,
            confidence=f.confidence,
            logit_shift=f.logit_shift,
            veto=bool(f.veto),
            rationale=f.rationale,
            evidence=list(f.evidence or []),
            model=f.model,
            active=bool(f.active),
            created_at=ensure_utc(f.created_at),
            expires_at=utc(f.expires_at),
        )
        for f in db.scalars(
            select(ContextFlagRow)
            .where(ContextFlagRow.match_id == match.id)
            .order_by(
                ContextFlagRow.active.desc(), ContextFlagRow.created_at.desc(), ContextFlagRow.id.desc()
            )
        )
    ]
    analysis_row = db.scalar(
        select(MatchAnalysis)
        .where(MatchAnalysis.match_id == match.id)
        .order_by(MatchAnalysis.is_latest.desc(), MatchAnalysis.created_at.desc(), MatchAnalysis.id.desc())
        .limit(1)
    )
    analysis = (
        AnalysisOut(
            id=analysis_row.id,
            created_at=ensure_utc(analysis_row.created_at),
            model_version=analysis_row.model_version,
            payload=dict(analysis_row.payload or {}),
            rationale=analysis_row.rationale,
            rationale_model=analysis_row.rationale_model,
        )
        if analysis_row is not None
        else None
    )
    events = sorted(
        events_by_match(db, [match.id]).get(match.id, []),
        key=lambda e: (_EVENT_ORDER.get(e.link_status, 9), -e.id),
    )
    markets = build_market_rows(
        db,
        match,
        tax=tax,
        now=ctx.now(),
        odds_max_age_s=float(settings.betclic.odds_max_age_s),
        extra_players={pid: p.nickname for pid, p in players.items()},
    )
    return MatchDetail(
        match=summary,
        lineups=lineups,
        vetoes=vetoes,
        maps=maps,
        context_flags=flags,
        analysis=analysis,
        betclic_events=[event_ref(e) for e in events],
        markets=markets,
        unmapped_selections=unmapped_selection_count(db, match.id),
        tax_rate=str(settings.effective_tax_rate),
    )


@router.get("/{match_id}/odds-history", response_model=OddsHistoryOut)
def odds_history(
    match_id: IdPath,
    db: DB,
    selection_id: Annotated[
        int | None,
        Query(
            ge=1, le=MAX_DB_INT, description="One selection; omitted = every mapped selection of the match"
        ),
    ] = None,
    since: Annotated[
        datetime | None, Query(description="Only snapshots captured at/after this instant")
    ] = None,
    max_points: Annotated[int, Query(ge=10, le=20000)] = 2000,
) -> OddsHistoryOut:
    match = db.scalar(select(Match).where(Match.id == match_id).options(*match_load_options()))
    if match is None:
        raise HTTPException(status_code=404, detail="match not found")
    stmt = (
        select(BookmakerSelection, BookmakerMarket)
        .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
        .join(BookmakerEvent, BookmakerMarket.event_id == BookmakerEvent.id)
        .where(BookmakerEvent.match_id == match_id)
    )
    if selection_id is not None:
        stmt = stmt.where(BookmakerSelection.id == selection_id)
    else:
        stmt = stmt.where(
            BookmakerEvent.link_status != LinkStatus.REJECTED,
            BookmakerSelection.is_mapped.is_(True),
            BookmakerSelection.selection_key.is_not(None),
        )
    pairs = list(db.execute(stmt))
    if selection_id is not None and not pairs:
        raise HTTPException(status_code=404, detail="selection not found for this match")

    ids = [sel.id for sel, _ in pairs]
    points: dict[int, list[OddsPoint]] = defaultdict(list)
    if ids:
        snap_stmt = select(OddsSnapshot).where(OddsSnapshot.selection_id.in_(ids))
        lower = as_utc(since)
        if lower is not None:
            snap_stmt = snap_stmt.where(OddsSnapshot.captured_at >= lower)
        for snap in db.scalars(
            snap_stmt.order_by(OddsSnapshot.selection_id, OddsSnapshot.captured_at, OddsSnapshot.id)
        ):
            points[snap.selection_id].append(
                OddsPoint(
                    captured_at=ensure_utc(snap.captured_at),
                    odds=odds_str(snap.odds) or "",
                    is_available=bool(snap.is_available),
                )
            )
    wanted_players: set[int] = set()
    for sel, market in pairs:
        if market.spec_key and sel.selection_key:
            wanted_players.update(
                player_ids_in(market.spec or parse_spec_key(market.spec_key), sel.selection_key)
            )
    names = player_names(db, wanted_players)
    t1, t2 = team_names(match)

    def spec_of(market: BookmakerMarket) -> dict[str, Any]:
        if isinstance(market.spec, dict):
            return market.spec
        return parse_spec_key(market.spec_key) if market.spec_key else {"market_type": "other"}

    series: list[OddsSeries] = []
    for sel, market in sorted(
        pairs,
        key=lambda p: (*market_sort_key(spec_of(p[1]), p[0].selection_key or ""), p[1].raw_name, p[0].id),
    ):
        label = None
        if market.spec_key and sel.selection_key:
            label = describe_outcome(spec_of(market), sel.selection_key, team1=t1, team2=t2, players=names)
        series.append(
            OddsSeries(
                selection_id=sel.id,
                bookmaker_event_id=market.event_id,
                market_raw=market.raw_name,
                selection_raw=sel.raw_name,
                spec_key=market.spec_key,
                selection_key=sel.selection_key,
                label=label,
                current_odds=odds_str(sel.last_odds),
                points=downsample(points.get(sel.id, []), max_points),
            )
        )
    return OddsHistoryOut(match_id=match_id, series=series)
