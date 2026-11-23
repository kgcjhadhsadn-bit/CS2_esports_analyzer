"""Sync HLTV matches, map stats, results and upcoming matches into the database.

All functions take an open session, flush but never commit (except ``sync_results`` when
``HLTVSyncOptions.commit_every`` is set), and are idempotent: re-running them updates rows in place.

Orientation: HLTV's page order may differ from ``matches.team1_id`` / ``team2_id`` (e.g. a match
first created from PandaScore). Team-specific columns are written in *our* orientation; references
by team id (winner, picks, vetoes, lineups, player stats) are orientation-free.

Round history is stored per map in ``maps_played.extra['hltv']['rounds']`` (see
:func:`hltv_round_records`) and, when complete, also in the ``rounds`` table with
``source=DataSource.HLTV`` (``HLTVSyncOptions.write_round_rows``, default on). Rounds from demo
parsing (``source=DEMO``) are never overwritten; the demo pipeline replaces HLTV rows.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import (
    Demo,
    MapPlayed,
    Match,
    MatchLineup,
    Player,
    PlayerMapStats,
    Round,
    Team,
    Tournament,
    Veto,
)
from cs2bot.db.repo import find_match, find_team_by_alias, is_swapped, resolve_team, resolve_tournament
from cs2bot.domain.enums import DemoStatus, MatchStatus, RoundEndReason, Side, TeamSlot, Tier
from cs2bot.domain.records import RoundRecord
from cs2bot.ingest.hltv.errors import HLTVParseError
from cs2bot.ingest.hltv.models import (
    HLTVMatchStatus,
    MapState,
    MapStatsPage,
    MatchPage,
    PlayerStatLine,
    ResultRow,
    SyncReport,
    TeamMapStats,
    TeamRef,
    UpcomingRow,
)
from cs2bot.ingest.hltv.parsers import (
    parse_mapstats_page,
    parse_match_page,
    parse_matches_page,
    parse_results_page,
)
from cs2bot.ingest.hltv.parsers.common import names_match
from cs2bot.ingest.hltv.selectors import FORFEIT_MAP_NAME
from cs2bot.ingest.hltv.sync.common import (
    EXTRA_KEY,
    SOURCE,
    HLTVSyncOptions,
    PageSource,
    fetch_page,
    in_scope,
    merge_extra,
    rank_as_of,
    record_team_rank,
    resolve_hltv_player,
    source_base_url,
)
from cs2bot.ingest.hltv.urls import (
    HLTV_BASE_URL,
    RESULTS_PAGE_SIZE,
    absolute_url,
    mapstats_path,
    match_path,
    matches_path,
    results_path,
)
from cs2bot.utils.time import ensure_utc, utcnow

logger = logging.getLogger(__name__)

_PLACEHOLDER_MAPS = frozenset({"tba", "tbd", FORFEIT_MAP_NAME, "unknown", ""})


@dataclass
class _Orientation:
    """How HLTV's page slots map onto our match."""

    match: Match
    slot_team: dict[TeamSlot, int]  # HLTV slot -> DB team id
    hltv1_is_our1: bool

    def ours[T](self, hltv_team1: T, hltv_team2: T) -> tuple[T, T]:
        return (hltv_team1, hltv_team2) if self.hltv1_is_our1 else (hltv_team2, hltv_team1)

    def our_team1_side(self, hltv_team1_side: Side | None) -> Side | None:
        if hltv_team1_side is None:
            return None
        return hltv_team1_side if self.hltv1_is_our1 else hltv_team1_side.other

    def team_id(self, slot: TeamSlot | None) -> int | None:
        return self.slot_team[slot] if slot is not None else None


# ============================================================================================ match


def sync_match(
    session: Session,
    fetcher: PageSource,
    hltv_match_id: int,
    *,
    settings: Settings,
    with_mapstats: bool = True,
    max_age_minutes: int | None = None,
    options: HLTVSyncOptions | None = None,
    url_hint: str | None = None,
    hints: Mapping[str, Any] | None = None,
    expect_finished: bool = False,
) -> SyncReport:
    """Fetch + persist one HLTV match (and its map stats pages when ``with_mapstats``).

    Links to an existing match by ``matches.hltv_id`` or, failing that, by
    :func:`cs2bot.db.repo.find_match` (same teams within ``match_link_window_hours``), respecting that
    match's orientation. Never raises for page/DB problems of this match: they end up in the report.
    ``expect_finished`` (set by :func:`sync_results`) re-fetches once if the cached page still shows
    the match as upcoming / live.
    """
    opts = options or HLTVSyncOptions()
    report = SyncReport(job="hltv.sync_match")
    base_url = settings.hltv.base_url
    path = url_hint or match_path(hltv_match_id)
    what = f"match {hltv_match_id}"
    html = fetch_page(fetcher, report, path, what, max_age_minutes=max_age_minutes)
    if html is None:
        return report
    try:
        page = parse_match_page(html, absolute_url(path, base_url), base_url=base_url)
        if expect_finished and page.status not in (HLTVMatchStatus.FINISHED, HLTVMatchStatus.DELETED):
            fresh = fetch_page(fetcher, report, path, what, max_age_minutes=0)  # cached before kick-off
            if fresh is None:
                return report
            page = parse_match_page(fresh, absolute_url(path, base_url), base_url=base_url)
    except HLTVParseError as exc:
        report.failed += 1
        report.add_error(f"{what}: {exc}")
        return report
    if page.hltv_match_id != hltv_match_id:
        report.add_warning(f"{what}: page reports match id {page.hltv_match_id}")
        page = page.model_copy(update={"hltv_match_id": hltv_match_id})
    report.warnings.extend(f"{what}: {w}" for w in page.warnings)
    try:
        with session.begin_nested():
            orient = _persist_match_page(session, page, settings, opts, report, hints or {})
    except Exception as exc:  # one broken match must never abort a whole sync
        logger.exception("persisting HLTV match %s failed", hltv_match_id)
        report.failed += 1
        report.add_error(f"{what}: persisting failed: {type(exc).__name__}: {exc}")
        return report
    if orient is None:
        return report
    match = orient.match
    mapstats_ok = True
    if with_mapstats:
        for map_result in page.played_maps:
            if map_result.state is not MapState.PLAYED or map_result.mapstats_id is None:
                continue
            sub = sync_mapstats(
                session,
                fetcher,
                map_result.mapstats_id,
                settings=settings,
                match=match,
                map_number=map_result.map_number,
                options=opts,
            )
            report.fetched_pages += sub.fetched_pages
            report.errors.extend(sub.errors)
            report.warnings.extend(sub.warnings)
            report.failed += sub.failed
            report.blocked = report.blocked or sub.blocked
            mapstats_ok = mapstats_ok and sub.ok and sub.failed == 0
            if report.blocked:
                break
    finished = page.status in (HLTVMatchStatus.FINISHED, HLTVMatchStatus.DELETED)
    needs_stats = any(m.state is MapState.PLAYED and m.mapstats_id is not None for m in page.maps)
    complete = finished and (not needs_stats or (with_mapstats and mapstats_ok))
    match.extra = merge_extra(match.extra, {"complete": complete})
    session.flush()
    return report


def _persist_match_page(
    session: Session,
    page: MatchPage,
    settings: Settings,
    opts: HLTVSyncOptions,
    report: SyncReport,
    hints: Mapping[str, Any],
) -> _Orientation | None:
    what = f"match {page.hltv_match_id}"
    if page.team1 is None or page.team2 is None:
        report.skipped += 1
        report.add_warning(f"{what}: teams not decided yet (TBD); skipped")
        return None
    team1 = resolve_team(session, name=page.team1.name, source=SOURCE, external_id=page.team1.hltv_id)
    team2 = resolve_team(session, name=page.team2.name, source=SOURCE, external_id=page.team2.hltv_id)
    if team1 is None or team2 is None or team1.id == team2.id:
        report.failed += 1
        report.add_error(
            f"{what}: could not resolve two distinct teams ({page.team1.name} / {page.team2.name})"
        )
        return None
    tournament = None
    if page.event is not None and page.event.name:
        tournament = resolve_tournament(
            session,
            name=page.event.name[:256],
            source=SOURCE,
            external_id=page.event.hltv_id,
            is_lan=page.is_lan,
        )
    match, created = _find_or_create_match(session, page, team1, team2, opts)
    orient = _orientation(match, team1, team2)
    if orient is None:
        if match.pandascore_id is None:
            match.team1_id, match.team2_id = team1.id, team2.id
            orient = _orientation(match, team1, team2)
        if orient is None:
            report.failed += 1
            report.add_error(f"{what}: linked match {match.id} has different teams; not touching it")
            return None
    now = utcnow()
    _apply_match_fields(session, match, page, orient, tournament, settings, team1, team2, hints, now)
    session.flush()
    _sync_vetoes(session, match, page, orient)
    _sync_maps(session, match, page, orient, report)
    _sync_lineups(session, match, page, orient)
    _sync_demos(session, match, page, report)
    if page.rank_date is not None:
        for team, rank in ((team1, page.team1_rank), (team2, page.team2_rank)):
            if rank is not None:
                record_team_rank(session, team, rank, page.rank_date)
    session.flush()
    if created:
        report.created += 1
    else:
        report.updated += 1
    if match.id not in report.match_ids:
        report.match_ids.append(match.id)
    return orient


def _find_or_create_match(
    session: Session, page: MatchPage, team1: Team, team2: Team, opts: HLTVSyncOptions
) -> tuple[Match, bool]:
    match = session.scalar(select(Match).where(Match.hltv_id == page.hltv_match_id))
    if match is None and page.scheduled_at is not None:
        candidate = find_match(
            session, team1.id, team2.id, page.scheduled_at, window_hours=opts.match_link_window_hours
        )
        if candidate is not None and candidate.hltv_id in (None, page.hltv_match_id):
            match = candidate
    if match is not None:
        return match, False
    match = Match(
        team1_id=team1.id,
        team2_id=team2.id,
        scheduled_at=page.scheduled_at,
        status=page.status.to_match_status(),
        hltv_id=page.hltv_match_id,
        tier=Tier.UNRANKED,
    )
    session.add(match)
    session.flush()
    return match, True


def _orientation(match: Match, team1: Team, team2: Team) -> _Orientation | None:
    """HLTV team1 vs our ``matches.team1_id`` (``db.repo.is_swapped``); None if the teams differ."""
    if {match.team1_id, match.team2_id} != {team1.id, team2.id}:
        return None
    slot_team = {TeamSlot.TEAM1: team1.id, TeamSlot.TEAM2: team2.id}
    return _Orientation(match, slot_team, hltv1_is_our1=not is_swapped(match, team1.id))


_STATUS_RANK = {
    MatchStatus.UNKNOWN: 0,
    MatchStatus.SCHEDULED: 1,
    MatchStatus.POSTPONED: 1,
    MatchStatus.LIVE: 2,
    MatchStatus.CANCELED: 3,
    MatchStatus.FINISHED: 4,
}


def _merge_status(current: MatchStatus | None, new: MatchStatus) -> MatchStatus:
    """Never move a finished match back; a cancelled match may still turn out finished."""
    if current is None:
        return new
    if current is MatchStatus.FINISHED and new is not MatchStatus.FINISHED:
        return current
    if current is MatchStatus.CANCELED and _STATUS_RANK[new] < _STATUS_RANK[MatchStatus.CANCELED]:
        return current
    return new


def _apply_match_fields(
    session: Session,
    match: Match,
    page: MatchPage,
    orient: _Orientation,
    tournament: Tournament | None,
    settings: Settings,
    team1: Team,
    team2: Team,
    hints: Mapping[str, Any],
    now: datetime,
) -> None:
    hltv_only = match.pandascore_id is None
    match.hltv_id = page.hltv_match_id
    match.hltv_url = page.url[:512]
    match.status = _merge_status(match.status, page.status.to_match_status())
    if page.scheduled_at is not None and (match.scheduled_at is None or hltv_only):
        match.scheduled_at = page.scheduled_at
    if page.format is not None and (match.format is None or hltv_only):
        match.format = page.format
    if page.is_lan is not None:
        match.is_lan = page.is_lan
    if page.stage and not match.stage:
        match.stage = page.stage[:128]
    if tournament is not None and match.tournament_id is None:
        match.tournament_id = tournament.id
    if (
        page.status is HLTVMatchStatus.FINISHED
        and page.team1_score is not None
        and page.team2_score is not None
    ):
        match.team1_score, match.team2_score = orient.ours(page.team1_score, page.team2_score)
        if page.winner is not None:
            match.winner_team_id = orient.team_id(page.winner)
    # The page shows the ranks that applied at match time; otherwise use the ranking history.
    when = page.scheduled_at or match.scheduled_at
    rank1 = page.team1_rank if page.team1_rank is not None else rank_as_of(session, team1, when, now=now)
    rank2 = page.team2_rank if page.team2_rank is not None else rank_as_of(session, team2, when, now=now)
    tier = tournament.tier if tournament is not None and tournament.tier is not Tier.UNRANKED else match.tier
    match.in_scope = bool(match.in_scope) or in_scope(tier, rank1, rank2, settings.scope)
    match.last_synced_at = now
    our_rank1, our_rank2 = orient.ours(page.team1_rank, page.team2_rank)
    forfeited_maps = [m.map_number for m in page.maps if m.state is MapState.FORFEIT]
    match.extra = merge_extra(
        match.extra,
        {
            "status": page.status.value,
            "status_text": page.status_text,
            "best_of": page.best_of,
            "event_id": page.event.hltv_id if page.event else None,
            "event_name": page.event.name if page.event else None,
            "scheduled_at": page.scheduled_at.isoformat() if page.scheduled_at else None,
            "team1_rank": our_rank1,
            "team2_rank": our_rank2,
            "rank_date": page.rank_date.isoformat() if page.rank_date else None,
            "forfeit": page.is_forfeit,
            "forfeit_note": page.forfeit_note,
            "forfeit_winner_team_id": orient.team_id(page.winner) if page.is_forfeit else None,
            "forfeited_maps": forfeited_maps or None,
            "notes": page.notes or None,
            "has_veto": bool(page.vetoes),
            "has_lineups": bool(page.team1_lineup and page.team2_lineup),
            "synced_at": now.isoformat(),
            **dict(hints),
        },
    )


def _sync_vetoes(session: Session, match: Match, page: MatchPage, orient: _Orientation) -> None:
    """Replace the match's vetoes when HLTV shows a veto (nothing is deleted before it is published)."""
    if not page.vetoes:
        return
    for old in session.scalars(select(Veto).where(Veto.match_id == match.id)):
        session.delete(old)
    session.flush()  # deletes before inserts: (match_id, step) is unique
    for entry in page.vetoes:
        session.add(
            Veto(
                match_id=match.id,
                step=entry.step,
                team_id=orient.team_id(entry.team),
                action=entry.action,
                map_name=entry.map_name[:32],
            )
        )
    session.flush()
    session.expire(match, ["vetoes"])


def _link_mapstats_id(session: Session, row: MapPlayed, mapstats_id: int, report: SyncReport) -> None:
    if row.hltv_mapstats_id == mapstats_id:
        return
    other = session.scalar(select(MapPlayed).where(MapPlayed.hltv_mapstats_id == mapstats_id))
    if other is not None and other is not row:
        report.add_warning(f"mapstats {mapstats_id} already linked to map row {other.id}; not relinking")
        return
    row.hltv_mapstats_id = mapstats_id


def _sync_maps(
    session: Session, match: Match, page: MatchPage, orient: _Orientation, report: SyncReport
) -> None:
    existing = {
        m.map_number: m for m in session.scalars(select(MapPlayed).where(MapPlayed.match_id == match.id))
    }
    for result in page.maps:
        if result.state not in (MapState.PLAYED, MapState.LIVE) or result.map_name in _PLACEHOLDER_MAPS:
            continue
        row = existing.get(result.map_number)
        if row is None:
            row = MapPlayed(match_id=match.id, map_number=result.map_number, map_name=result.map_name[:32])
            session.add(row)
        elif row.map_name != result.map_name:
            report.add_warning(
                f"match {page.hltv_match_id} map {result.map_number}: {row.map_name!r} -> {result.map_name!r}"
            )
            row.map_name = result.map_name[:32]
        row.status = MatchStatus.FINISHED if result.state is MapState.PLAYED else MatchStatus.LIVE
        row.team1_rounds, row.team2_rounds = orient.ours(result.team1_rounds, result.team2_rounds)
        row.winner_team_id = orient.team_id(result.winner) if result.state is MapState.PLAYED else None
        row.overtime_rounds = result.overtime_rounds
        row.picked_by_team_id = orient.team_id(result.picked_by)
        row.is_decider = result.is_decider
        if row.played_at is None and page.scheduled_at is not None:
            row.played_at = page.scheduled_at
        if result.team1_start_side is not None and row.team1_start_side is None:
            row.team1_start_side = orient.our_team1_side(result.team1_start_side)  # map stats refine it
        splits = result.regulation_side_rounds()
        if splits is not None and result.overtime_rounds == 0 and row.team1_ct_rounds is None:
            # regulation covers the whole map; map stats (incl. overtime) override this later
            our1, our2 = orient.ours(TeamSlot.TEAM1, TeamSlot.TEAM2)
            row.team1_ct_rounds = splits[(our1, Side.CT)]
            row.team1_t_rounds = splits[(our1, Side.T)]
            row.team2_ct_rounds = splits[(our2, Side.CT)]
            row.team2_t_rounds = splits[(our2, Side.T)]
        if result.mapstats_id is not None:
            _link_mapstats_id(session, row, result.mapstats_id, report)
        halves = []
        for half in result.halves:
            a, b = orient.ours(half.team1_rounds, half.team2_rounds)
            side = orient.our_team1_side(half.team1_side)
            halves.append(
                {
                    "team1": a,
                    "team2": b,
                    "team1_side": side.value if side else None,
                    "overtime": half.is_overtime,
                }
            )
        row.extra = merge_extra(
            row.extra, {"raw_name": result.raw_name, "halves": halves, "mapstats_url": result.mapstats_url}
        )
    session.flush()


def _sync_lineups(session: Session, match: Match, page: MatchPage, orient: _Orientation) -> None:
    seen_at = page.scheduled_at
    wanted: dict[int, tuple[int, bool]] = {}
    for slot in TeamSlot:
        team_id = orient.slot_team[slot]
        for p in page.lineup(slot):
            player = resolve_hltv_player(
                session,
                nickname=p.nickname,
                hltv_id=p.hltv_id,
                team_id=team_id,
                seen_at=seen_at,
                country=p.country,
                real_name=p.real_name,
                move_team=not p.is_standin,
            )
            if player is not None and player.id not in wanted:
                wanted[player.id] = (team_id, p.is_standin)
    if not wanted:
        return
    session.flush()
    existing = {
        lu.player_id: lu
        for lu in session.scalars(select(MatchLineup).where(MatchLineup.match_id == match.id))
    }
    for player_id, (team_id, standin) in wanted.items():
        row = existing.get(player_id)
        if row is None:
            session.add(
                MatchLineup(
                    match_id=match.id, team_id=team_id, player_id=player_id, is_standin=standin, source=SOURCE
                )
            )
        else:
            row.team_id = team_id
            row.is_standin = standin if row.source == SOURCE else (row.is_standin or standin)
    # Only teams whose lineup the page actually showed are reconciled: a lineup block that failed to
    # parse (or is not published yet for one side) must not wipe that team's stored lineup.
    shown_teams = {orient.slot_team[slot] for slot in TeamSlot if page.lineup(slot)}
    for player_id, row in existing.items():
        if player_id not in wanted and row.source == SOURCE and row.team_id in shown_teams:
            session.delete(row)  # replaced by a stand-in / roster change on HLTV
    session.flush()


def _sync_demos(session: Session, match: Match, page: MatchPage, report: SyncReport) -> None:
    for link in page.demos:
        row = session.scalar(select(Demo).where(Demo.hltv_demo_id == link.hltv_demo_id))
        if row is None:
            session.add(
                Demo(
                    match_id=match.id,
                    hltv_demo_id=link.hltv_demo_id,
                    url=link.url[:1024],
                    status=DemoStatus.PENDING,
                )
            )
        elif row.match_id != match.id:
            report.add_warning(f"demo {link.hltv_demo_id} belongs to match row {row.match_id}; not relinking")
    session.flush()


# ============================================================================================ mapstats


def sync_mapstats(
    session: Session,
    fetcher: PageSource,
    mapstats_id: int,
    *,
    settings: Settings | None = None,
    match: Match | None = None,
    map_number: int | None = None,
    options: HLTVSyncOptions | None = None,
) -> SyncReport:
    """Fetch + persist one map stats page: player stat lines, side split, round history.

    The ``maps_played`` row is found by ``hltv_mapstats_id``, else by ``match`` + ``map_number``, else
    by the page's match id + map name. Sync the match first if neither exists.
    """
    opts = options or HLTVSyncOptions()
    report = SyncReport(job="hltv.sync_mapstats")
    what = f"mapstats {mapstats_id}"
    path = mapstats_path(mapstats_id)
    html = fetch_page(fetcher, report, path, what)
    if html is None:
        return report
    base_url = settings.hltv.base_url if settings is not None else source_base_url(fetcher, HLTV_BASE_URL)
    try:
        page = parse_mapstats_page(html, absolute_url(path, base_url))
    except HLTVParseError as exc:
        report.failed += 1
        report.add_error(f"{what}: {exc}")
        return report
    report.warnings.extend(f"{what}: {w}" for w in page.warnings)
    try:
        with session.begin_nested():
            row = _locate_map_row(session, page, match, map_number)
            if row is None:
                report.skipped += 1
                report.add_warning(f"{what}: no maps_played row for this map yet (sync the match first)")
                return report
            owner = (
                match if match is not None and match.id == row.match_id else session.get(Match, row.match_id)
            )
            if owner is None:
                report.skipped += 1
                return report
            created = _persist_mapstats(session, owner, row, page, opts, report)
            report.created += created
            report.updated += 1 if not created else 0
    except Exception as exc:
        logger.exception("persisting HLTV mapstats %s failed", mapstats_id)
        report.failed += 1
        report.add_error(f"{what}: persisting failed: {type(exc).__name__}: {exc}")
    return report


def _locate_map_row(
    session: Session, page: MapStatsPage, match: Match | None, map_number: int | None
) -> MapPlayed | None:
    row = session.scalar(select(MapPlayed).where(MapPlayed.hltv_mapstats_id == page.mapstats_id))
    if row is not None:
        return row
    if match is not None and map_number is not None:
        row = session.scalar(
            select(MapPlayed).where(MapPlayed.match_id == match.id, MapPlayed.map_number == map_number)
        )
        if row is not None:
            return row
    owner = match
    if owner is None and page.hltv_match_id is not None:
        owner = session.scalar(select(Match).where(Match.hltv_id == page.hltv_match_id))
    if owner is None:
        return None
    candidates = list(
        session.scalars(
            select(MapPlayed).where(MapPlayed.match_id == owner.id, MapPlayed.map_name == page.map_name)
        )
    )
    return candidates[0] if len(candidates) == 1 else None


def _matches_team(ref: TeamRef, team: Team | None) -> bool:
    if team is None:
        return False
    if ref.hltv_id is not None and team.hltv_id is not None:
        return ref.hltv_id == team.hltv_id
    return names_match(ref.name, team.name)


def _left_is_our_team1(page: MapStatsPage, team_a: Team | None, team_b: Team | None) -> bool | None:
    """True if the page's left team is our team1, False if it is our team2, None if unclear."""
    left, right = page.team1.team, page.team2.team
    l1, l2 = _matches_team(left, team_a), _matches_team(left, team_b)
    r1, r2 = _matches_team(right, team_a), _matches_team(right, team_b)
    straight = (l1 or r2) and not (l2 or r1)  # left = team1, right = team2
    crossed = (l2 or r1) and not (l1 or r2)  # left = team2, right = team1
    if straight:
        return True
    if crossed:
        return False
    return None


def _stat_key(line: PlayerStatLine) -> str:
    return f"id:{line.hltv_id}" if line.hltv_id is not None else f"nick:{line.nickname.casefold()}"


def _side_payload(line: PlayerStatLine | None) -> dict[str, Any] | None:
    if line is None:
        return None
    return {
        k: v
        for k, v in line.model_dump(
            include={
                "kills",
                "deaths",
                "assists",
                "headshots",
                "adr",
                "kast",
                "rating",
                "first_kills",
                "first_deaths",
            }
        ).items()
        if v is not None
    }


def _persist_mapstats(
    session: Session,
    match: Match,
    row: MapPlayed,
    page: MapStatsPage,
    opts: HLTVSyncOptions,
    report: SyncReport,
) -> bool:
    """Returns True if player stat rows were created (first sync of this map)."""
    team_a = session.get(Team, match.team1_id) if match.team1_id is not None else None
    team_b = session.get(Team, match.team2_id) if match.team2_id is not None else None
    left_is_our1 = _left_is_our_team1(page, team_a, team_b)
    if left_is_our1 is None or match.team1_id is None or match.team2_id is None:
        raise HLTVParseError(
            f"mapstats {page.mapstats_id}: teams {page.team1.team.name}/{page.team2.team.name} "
            f"do not match match {match.id}"
        )
    ours1, ours2 = (page.team1, page.team2) if left_is_our1 else (page.team2, page.team1)
    if page.map_name not in _PLACEHOLDER_MAPS and page.map_name != row.map_name:
        report.add_warning(f"mapstats {page.mapstats_id}: map {row.map_name!r} renamed to {page.map_name!r}")
        row.map_name = page.map_name[:32]
    _link_mapstats_id(session, row, page.mapstats_id, report)
    row.status = MatchStatus.FINISHED
    if ours1.rounds is not None and ours2.rounds is not None:
        row.team1_rounds, row.team2_rounds = ours1.rounds, ours2.rounds
        row.overtime_rounds = page.overtime_rounds
        if ours1.rounds != ours2.rounds:
            row.winner_team_id = match.team1_id if ours1.rounds > ours2.rounds else match.team2_id
    if ours1.start_side is not None:
        row.team1_start_side = ours1.start_side
    if page.side_split_complete:
        row.team1_ct_rounds, row.team1_t_rounds = ours1.ct_rounds, ours1.t_rounds
        row.team2_ct_rounds, row.team2_t_rounds = ours2.ct_rounds, ours2.t_rounds
    if page.played_at is not None:
        row.played_at = page.played_at
    rounds_payload = []
    for r in page.rounds:
        winner = TeamSlot.TEAM1 if (r.winner is TeamSlot.TEAM1) == left_is_our1 else TeamSlot.TEAM2
        team1_side = r.team1_side if left_is_our1 or r.team1_side is None else r.team1_side.other
        s1, s2 = (r.team1_score, r.team2_score) if left_is_our1 else (r.team2_score, r.team1_score)
        rounds_payload.append(
            {
                "round": r.round_number,
                "winner": winner.value,
                "winner_side": r.winner_side.value if r.winner_side else None,
                "team1_side": team1_side.value if team1_side else None,
                "end_reason": r.end_reason.value,
                "score": [s1, s2] if s1 is not None and s2 is not None else None,
            }
        )
    row.extra = merge_extra(
        row.extra,
        {
            "mapstats_id": page.mapstats_id,
            "rating_version": page.rating_version,
            "rounds": rounds_payload or None,
            "rounds_complete": page.rounds_complete,
            "team1_rating": ours1.team_rating,
            "team2_rating": ours2.team_rating,
            "team1_first_kills": ours1.first_kills,
            "team2_first_kills": ours2.first_kills,
            "team1_clutches": ours1.clutches_won,
            "team2_clutches": ours2.clutches_won,
        },
    )
    session.flush()
    created_any = False
    seen_at = page.played_at or match.scheduled_at
    done: set[int] = set()
    standins = set(
        session.scalars(
            select(Player.hltv_id)
            .join(MatchLineup, MatchLineup.player_id == Player.id)
            .where(
                MatchLineup.match_id == match.id,
                MatchLineup.is_standin.is_(True),
                Player.hltv_id.is_not(None),
            )
        )
    )
    for stats, team_id in ((ours1, match.team1_id), (ours2, match.team2_id)):
        created_any |= _persist_player_lines(
            session, row, stats, team_id, page, seen_at, report, done, standins
        )
    if opts.write_round_rows and page.rounds_complete:
        _write_round_rows(session, row, match, rounds_payload)
    session.flush()
    return created_any


def _persist_player_lines(
    session: Session,
    row: MapPlayed,
    stats: TeamMapStats,
    team_id: int,
    page: MapStatsPage,
    seen_at: datetime | None,
    report: SyncReport,
    done: set[int],
    standins: set[int | None],
) -> bool:
    ct_lines = {_stat_key(line): line for line in stats.ct_players}
    t_lines = {_stat_key(line): line for line in stats.t_players}
    created = False
    for line in stats.players:
        if line.kills is None or line.deaths is None:
            report.add_warning(
                f"mapstats {page.mapstats_id}: incomplete stat line for {line.nickname}; skipped"
            )
            continue
        player = resolve_hltv_player(
            session,
            nickname=line.nickname,
            hltv_id=line.hltv_id,
            team_id=team_id,
            seen_at=seen_at,
            country=line.country,
            move_team=line.hltv_id not in standins,
        )
        if player is None or player.id in done:
            continue  # two lines resolving to one player would violate (map, player, source)
        done.add(player.id)
        stat = session.scalar(
            select(PlayerMapStats).where(
                PlayerMapStats.map_played_id == row.id,
                PlayerMapStats.player_id == player.id,
                PlayerMapStats.source == SOURCE,
            )
        )
        if stat is None:
            stat = PlayerMapStats(
                map_played_id=row.id,
                player_id=player.id,
                team_id=team_id,
                source=SOURCE,
                kills=line.kills,
                deaths=line.deaths,
            )
            session.add(stat)
            created = True
        ct = ct_lines.get(_stat_key(line))
        t = t_lines.get(_stat_key(line))
        stat.team_id = team_id
        stat.kills = line.kills
        stat.deaths = line.deaths
        stat.assists = line.assists or 0
        stat.headshots = line.headshots
        stat.rounds_played = page.total_rounds
        stat.adr = line.adr
        stat.kast = line.kast
        stat.rating = line.rating
        stat.first_kills = line.first_kills
        stat.first_deaths = line.first_deaths
        stat.clutches_won = line.clutches_won
        stat.multi_kill_rounds = line.multi_kill_rounds
        stat.flash_assists = line.flash_assists
        stat.traded_deaths = line.traded_deaths
        stat.ct_kills = ct.kills if ct else None
        stat.ct_deaths = ct.deaths if ct else None
        stat.t_kills = t.kills if t else None
        stat.t_deaths = t.deaths if t else None
        stat.extra = {
            **(stat.extra or {}),
            "hltv_player_id": line.hltv_id,
            "rating_version": page.rating_version,
            "round_swing": line.round_swing,
            "kd_diff": line.kd_diff,
            "fk_diff": line.fk_diff,
            "ct": _side_payload(ct),
            "t": _side_payload(t),
        }
    session.flush()
    return created


def _write_round_rows(
    session: Session, row: MapPlayed, match: Match, rounds_payload: list[dict[str, Any]]
) -> int:
    """Upsert the HLTV round history of one map into ``rounds`` (``source=DataSource.HLTV``).

    Only complete histories with known sides are written. A map that already has rows from another
    source (demo parsing) is left alone: demo rows are richer and the demo pipeline replaces HLTV rows,
    never the other way round. Existing HLTV rows are updated in place (idempotent), surplus HLTV rows
    removed. Returns the number of rows written.
    """
    if not rounds_payload or any(r["team1_side"] is None or r["winner_side"] is None for r in rounds_payload):
        return 0
    numbers = [int(r["round"]) for r in rounds_payload]
    if len(set(numbers)) != len(numbers):
        return 0
    existing = list(session.scalars(select(Round).where(Round.map_played_id == row.id)))
    if any(r.source is not SOURCE for r in existing):
        return 0
    by_number = {r.round_number: r for r in existing}
    wanted = set(numbers)
    for old in existing:
        if old.round_number not in wanted:
            session.delete(old)
    session.flush()
    for r in rounds_payload:
        number = int(r["round"])
        team1_side = Side(r["team1_side"])
        winner_side = Side(r["winner_side"])
        winner_team_id = match.team1_id if r["winner"] == TeamSlot.TEAM1.value else match.team2_id
        end_reason = RoundEndReason(r["end_reason"])
        target = by_number.get(number)
        if target is None:
            session.add(
                Round(
                    map_played_id=row.id,
                    round_number=number,
                    team1_side=team1_side,
                    winner_side=winner_side,
                    winner_team_id=winner_team_id,
                    source=SOURCE,
                    end_reason=end_reason,
                )
            )
        else:
            target.team1_side = team1_side
            target.winner_side = winner_side
            target.winner_team_id = winner_team_id
            target.end_reason = end_reason
    return len(rounds_payload)


def hltv_round_records(session: Session, map_played: MapPlayed) -> list[RoundRecord]:
    """Round history stored by the HLTV sync, as :class:`RoundRecord` (no economy fields).

    Fallback for maps without a parsed demo. Empty when the map has no complete HLTV round history.
    """
    hltv = (map_played.extra or {}).get(EXTRA_KEY) or {}
    rounds = hltv.get("rounds") or []
    if not hltv.get("rounds_complete") or not rounds:
        return []
    match = session.get(Match, map_played.match_id)
    if match is None or match.team1_id is None or match.team2_id is None:
        return []
    played_at = map_played.played_at or match.scheduled_at
    if played_at is None:
        return []
    out: list[RoundRecord] = []
    score1 = score2 = 0
    for r in rounds:
        if r.get("team1_side") is None:
            return []
        winner = TeamSlot(r["winner"])
        out.append(
            RoundRecord(
                match_id=match.id,
                map_number=map_played.map_number,
                map_name=map_played.map_name,
                played_at=played_at,
                round_number=int(r["round"]),
                team1_id=match.team1_id,
                team2_id=match.team2_id,
                team1_side=Side(r["team1_side"]),
                winner=winner,
                end_reason=RoundEndReason(r["end_reason"]),
                team1_score_before=score1,
                team2_score_before=score2,
                is_lan=bool(match.is_lan),
            )
        )
        if winner is TeamSlot.TEAM1:
            score1 += 1
        else:
            score2 += 1
    return out


# ============================================================================================ listings


def _lookup_team(session: Session, ref: TeamRef | None) -> Team | None:
    if ref is None:
        return None
    if ref.hltv_id is not None:
        team = session.scalar(select(Team).where(Team.hltv_id == ref.hltv_id))
        if team is not None:
            return team
    return find_team_by_alias(session, ref.name)


def _is_complete(match: Match) -> bool:
    return bool(((match.extra or {}).get(EXTRA_KEY) or {}).get("complete"))


def _relevant(
    session: Session,
    team1: TeamRef | None,
    team2: TeamRef | None,
    stars: int,
    around: datetime | None,
    existing: Match | None,
    settings: Settings,
    opts: HLTVSyncOptions,
    now: datetime | None = None,
) -> bool:
    """In scope by tier / HLTV rank (``ingest.scope``), HLTV stars, or an already in-scope match.

    Ranks are the ones that applied at ``around`` (ranking history for old matches, see
    :func:`~cs2bot.ingest.hltv.sync.common.rank_as_of`), not today's.
    """
    if existing is not None and existing.in_scope:
        return True
    if stars >= 1:
        return True
    t1, t2 = _lookup_team(session, team1), _lookup_team(session, team2)
    rank1, rank2 = rank_as_of(session, t1, around, now=now), rank_as_of(session, t2, around, now=now)
    if in_scope(None, rank1, rank2, settings.scope):
        return True
    if t1 is not None and t2 is not None and around is not None:
        candidate = find_match(session, t1.id, t2.id, around, window_hours=opts.match_link_window_hours)
        if candidate is not None and candidate.in_scope:
            return True
    return False


def sync_results(
    session: Session,
    fetcher: PageSource,
    *,
    settings: Settings,
    since: datetime,
    max_pages: int = 50,
    in_scope_only: bool = True,
    stop_when_nothing_new: bool = True,
    query: Mapping[str, str | int] | None = None,
    options: HLTVSyncOptions | None = None,
    force: bool = False,
) -> SyncReport:
    """Walk ``/results`` (100 per page, newest first) back to ``since`` and sync relevant matches.

    Stops at the first page that only holds already-complete matches (pass
    ``stop_when_nothing_new=False`` for a backfill that must resume past them), at rows older than
    ``since``, after ``max_pages`` or when HLTV blocks us (``report.blocked``; partial progress stays in
    the session for the caller to commit).
    """
    opts = options or HLTVSyncOptions()
    report = SyncReport(job="hltv.sync_results")
    since = ensure_utc(since)
    base_url = settings.hltv.base_url
    pages = 0
    synced = 0
    for page_index in range(max_pages):
        path = results_path(page_index * RESULTS_PAGE_SIZE, query)
        html = fetch_page(
            fetcher, report, path, f"results page {page_index + 1}", max_age_minutes=opts.listing_ttl_minutes
        )
        if html is None:
            break
        rows = parse_results_page(html, base_url=base_url)
        pages += 1
        if not rows:
            break
        reached_old = False
        new_on_page = 0
        complete_on_page = 0
        for row in rows:
            if row.played_at is not None and row.played_at < since:
                reached_old = True
                continue
            existing = session.scalar(select(Match).where(Match.hltv_id == row.hltv_match_id))
            if existing is not None and not force and _is_complete(existing):
                complete_on_page += 1
                report.skipped += 1
                continue
            if in_scope_only and not _relevant(
                session, row.team1, row.team2, row.stars, row.played_at, existing, settings, opts
            ):
                report.skipped += 1
                continue
            new_on_page += 1
            report.merge(_sync_listed_match(session, fetcher, row, settings, opts))
            synced += 1
            if opts.commit_every and synced % opts.commit_every == 0:
                session.commit()
            if report.blocked:
                break
        if report.blocked or reached_old:
            break
        if stop_when_nothing_new and new_on_page == 0 and complete_on_page > 0:
            break
    report.details.update({"pages": pages, "since": since.isoformat(), "matches_synced": synced})
    return report


def _sync_listed_match(
    session: Session, fetcher: PageSource, row: ResultRow, settings: Settings, opts: HLTVSyncOptions
) -> SyncReport:
    return sync_match(
        session,
        fetcher,
        row.hltv_match_id,
        settings=settings,
        with_mapstats=True,
        options=opts,
        url_hint=row.url,
        hints={"stars": row.stars},
        expect_finished=True,
    )


def sync_upcoming(
    session: Session,
    fetcher: PageSource,
    *,
    settings: Settings,
    options: HLTVSyncOptions | None = None,
    now: datetime | None = None,
    include_live: bool = False,
) -> SyncReport:
    """Sync relevant upcoming matches from ``/matches`` (teams, lineups, veto once published).

    Match pages close to their start (``near_start_window_minutes``) are re-fetched with a short TTL
    because vetoes and lineups usually appear about an hour before the match.
    """
    opts = options or HLTVSyncOptions()
    report = SyncReport(job="hltv.sync_upcoming")
    current = now or utcnow()
    html = fetch_page(
        fetcher, report, matches_path(), "matches list", max_age_minutes=opts.listing_ttl_minutes
    )
    if html is None:
        return report
    rows: list[UpcomingRow] = parse_matches_page(html, base_url=settings.hltv.base_url)
    horizon = current + timedelta(hours=settings.scope.max_hours_ahead)
    for row in rows:
        if (row.is_live and not include_live) or not row.has_teams:
            report.skipped += 1
            continue
        if row.scheduled_at is not None and row.scheduled_at > horizon:
            report.skipped += 1
            continue
        existing = session.scalar(select(Match).where(Match.hltv_id == row.hltv_match_id))
        if not _relevant(
            session, row.team1, row.team2, row.stars, row.scheduled_at, existing, settings, opts, current
        ):
            report.skipped += 1
            continue
        near_start = row.scheduled_at is not None and row.scheduled_at - current <= timedelta(
            minutes=opts.near_start_window_minutes
        )
        ttl = opts.near_start_ttl_minutes if near_start else opts.upcoming_match_ttl_minutes
        report.merge(
            sync_match(
                session,
                fetcher,
                row.hltv_match_id,
                settings=settings,
                with_mapstats=False,
                max_age_minutes=ttl,
                options=opts,
                url_hint=row.url,
                hints={"stars": row.stars},
            )
        )
        if report.blocked:
            break
    report.details.update({"listed": len(rows)})
    return report
