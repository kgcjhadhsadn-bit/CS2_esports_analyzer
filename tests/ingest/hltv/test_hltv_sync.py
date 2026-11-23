"""HLTV -> DB sync: matches, map stats, results, upcoming, rankings, news."""

from __future__ import annotations

import re
import sys
import types
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import (
    Demo,
    MapPlayed,
    Match,
    MatchLineup,
    NewsItem,
    Player,
    PlayerMapStats,
    Round,
    Team,
    TeamRanking,
    Tournament,
    Veto,
)
from cs2bot.db.repo import resolve_player, resolve_team
from cs2bot.domain.enums import (
    DataSource,
    DemoStatus,
    MatchFormat,
    MatchStatus,
    RoundEndReason,
    Side,
    TeamSlot,
    Tier,
    VetoAction,
)
from cs2bot.ingest.hltv import (
    HLTVBlockedError,
    HLTVSyncOptions,
    hltv_round_records,
    sync_mapstats,
    sync_match,
    sync_news,
    sync_rankings,
    sync_results,
    sync_upcoming,
)
from cs2bot.ingest.hltv.sync import common as sync_common
from tests.ingest.hltv.conftest import FakeFetcher, load_fixture


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def team(session: Session, hltv_id: int) -> Team:
    row = session.scalar(select(Team).where(Team.hltv_id == hltv_id))
    assert row is not None
    return row


def player(session: Session, hltv_id: int) -> Player:
    row = session.scalar(select(Player).where(Player.hltv_id == hltv_id))
    assert row is not None
    return row


def match_by_hltv(session: Session, hltv_id: int) -> Match:
    row = session.scalar(select(Match).where(Match.hltv_id == hltv_id))
    assert row is not None
    return row


# ------------------------------------------------------------------------------------------- match


def test_sync_finished_bo3_creates_everything(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    report = sync_match(session, fetcher, 2378001, settings=app_settings)
    assert report.ok, report.errors
    assert report.created == 1 and report.fetched_pages == 4  # match page + 3 map stats pages

    m = match_by_hltv(session, 2378001)
    vit, navi = team(session, 9565), team(session, 4608)
    assert (m.team1_id, m.team2_id) == (vit.id, navi.id)
    assert m.status is MatchStatus.FINISHED and m.format is MatchFormat.BO3 and m.is_lan is True
    assert (m.team1_score, m.team2_score, m.winner_team_id) == (2, 1, vit.id)
    assert m.scheduled_at == datetime(2026, 11, 21, 16, 30, tzinfo=UTC)
    assert m.stage == "Playoffs - Semi-final" and m.in_scope  # both teams are top-50 on the page
    assert m.hltv_url == "https://www.hltv.org/matches/2378001/vitality-vs-natus-vincere-iem-cologne-2026"
    assert m.extra["hltv"]["complete"] is True and m.extra["hltv"]["team1_rank"] == 1

    tour = session.get(Tournament, m.tournament_id)
    assert (
        tour is not None
        and tour.hltv_event_id == 8001
        and tour.is_lan is True
        and tour.name == "IEM Cologne 2026"
    )

    maps = {mp.map_number: mp for mp in session.scalars(select(MapPlayed).where(MapPlayed.match_id == m.id))}
    assert [maps[i].map_name for i in (1, 2, 3)] == ["mirage", "inferno", "anubis"]
    mirage, inferno, anubis = maps[1], maps[2], maps[3]
    assert (mirage.team1_rounds, mirage.team2_rounds, mirage.winner_team_id) == (11, 13, navi.id)
    assert mirage.picked_by_team_id == navi.id and not mirage.is_decider
    assert mirage.team1_start_side is Side.CT
    assert (mirage.team1_ct_rounds, mirage.team1_t_rounds, mirage.team2_ct_rounds, mirage.team2_t_rounds) == (
        5,
        6,
        6,
        7,
    )
    assert mirage.hltv_mapstats_id == 210001 and mirage.status is MatchStatus.FINISHED
    assert mirage.played_at == datetime(2026, 11, 21, 16, 30, tzinfo=UTC)
    # Inferno's map stats page lists NAVI first; values still land in our orientation
    assert (inferno.team1_rounds, inferno.team2_rounds, inferno.winner_team_id) == (13, 7, vit.id)
    assert inferno.team1_start_side is Side.T and (inferno.team1_t_rounds, inferno.team1_ct_rounds) == (8, 5)
    assert inferno.picked_by_team_id == vit.id
    # decider with two overtimes: side split includes all 36 rounds
    assert anubis.is_decider and anubis.picked_by_team_id is None and anubis.overtime_rounds == 12
    assert anubis.team1_ct_rounds + anubis.team1_t_rounds == 19
    assert anubis.team2_ct_rounds + anubis.team2_t_rounds == 17

    vetoes = list(session.scalars(select(Veto).where(Veto.match_id == m.id).order_by(Veto.step)))
    assert [(v.step, v.action, v.team_id, v.map_name) for v in vetoes] == [
        (1, VetoAction.BAN, navi.id, "nuke"),
        (2, VetoAction.BAN, vit.id, "ancient"),
        (3, VetoAction.PICK, navi.id, "mirage"),
        (4, VetoAction.PICK, vit.id, "inferno"),
        (5, VetoAction.BAN, navi.id, "dust2"),
        (6, VetoAction.BAN, vit.id, "train"),
        (7, VetoAction.LEFTOVER, None, "anubis"),
    ]

    lineups = list(session.scalars(select(MatchLineup).where(MatchLineup.match_id == m.id)))
    assert len(lineups) == 10 and {lu.source for lu in lineups} == {DataSource.HLTV}
    zywoo = player(session, 11893)
    assert (
        zywoo.nickname == "ZywOo" and zywoo.current_team_id == vit.id and zywoo.real_name == "Mathieu Herbaut"
    )
    assert {lu.team_id for lu in lineups if lu.player_id == zywoo.id} == {vit.id}

    stats = list(session.scalars(select(PlayerMapStats).where(PlayerMapStats.source == DataSource.HLTV)))
    assert len(stats) == 30
    apex_mirage = next(
        s for s in stats if s.player_id == player(session, 7322).id and s.map_played_id == mirage.id
    )
    assert (apex_mirage.kills, apex_mirage.deaths, apex_mirage.headshots, apex_mirage.assists) == (
        19,
        17,
        9,
        6,
    )
    assert apex_mirage.rounds_played == 24 and apex_mirage.team_id == vit.id
    assert apex_mirage.kast == pytest.approx(67.6) and apex_mirage.rating == pytest.approx(1.61)
    assert apex_mirage.ct_kills is not None and apex_mirage.ct_kills + (apex_mirage.t_kills or 0) == 19
    assert (
        apex_mirage.extra["rating_version"] == "3.0"
        and apex_mirage.extra["ct"]["kills"] == apex_mirage.ct_kills
    )

    demos = list(session.scalars(select(Demo).where(Demo.match_id == m.id)))
    assert sorted(d.hltv_demo_id for d in demos) == [96001, 96002, 96003]
    assert all(d.status is DemoStatus.PENDING and d.url and "/download/demo/" in d.url for d in demos)

    # ranks shown on the match page (dated ranking link) update the teams
    assert vit.hltv_rank == 1 and navi.hltv_rank == 3
    assert vit.hltv_rank_updated_at == datetime(2026, 11, 16, tzinfo=UTC)
    assert count(session, TeamRanking) == 2
    # complete round histories also go to ``rounds`` (source HLTV)
    assert count(session, Round) == 24 + 20 + 36
    assert set(session.scalars(select(Round.source))) == {DataSource.HLTV}


def test_sync_match_is_idempotent(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    first = sync_match(session, fetcher, 2378001, settings=app_settings)
    counts = {
        model: count(session, model)
        for model in (Match, MapPlayed, Veto, MatchLineup, PlayerMapStats, Demo, Player, Team, Tournament)
    }
    second = sync_match(session, fetcher, 2378001, settings=app_settings)
    assert first.created == 1 and second.created == 0 and second.updated == 1 and second.ok
    assert {model: count(session, model) for model in counts} == counts
    session.commit()  # the whole result is a valid transaction


def test_round_history_is_kept_and_exposed_as_records(session: Session, app_settings: Settings) -> None:
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings)
    m = match_by_hltv(session, 2378001)
    anubis = session.scalar(select(MapPlayed).where(MapPlayed.match_id == m.id, MapPlayed.map_number == 3))
    assert anubis is not None
    stored = anubis.extra["hltv"]["rounds"]
    assert len(stored) == 36 and anubis.extra["hltv"]["rounds_complete"] is True
    records = hltv_round_records(session, anubis)
    assert len(records) == 36
    assert sum(r.winner is TeamSlot.TEAM1 for r in records) == 19
    assert records[0].team1_id == m.team1_id and records[0].team1_side is Side.CT
    assert records[-1].team1_score_before + records[-1].team2_score_before == 35
    assert {r.end_reason for r in records} <= set(RoundEndReason)


def test_round_rows_can_be_switched_off(session: Session, app_settings: Settings) -> None:
    opts = HLTVSyncOptions(write_round_rows=False)
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings, options=opts)
    assert count(session, Round) == 0


def test_round_rows_are_idempotent_and_never_overwrite_demo_rounds(
    session: Session, app_settings: Settings
) -> None:
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings)
    assert count(session, Round) == 24 + 20 + 36
    m = match_by_hltv(session, 2378001)
    maps = {mp.map_number: mp for mp in session.scalars(select(MapPlayed).where(MapPlayed.match_id == m.id))}
    # a corrupted HLTV row is repaired on the next sync, an extra one removed
    first = session.scalar(select(Round).where(Round.map_played_id == maps[1].id, Round.round_number == 1))
    assert first is not None and first.source is DataSource.HLTV
    expected_winner = first.winner_team_id
    first.winner_team_id = None
    session.add(
        Round(
            map_played_id=maps[1].id,
            round_number=99,
            team1_side=Side.CT,
            winner_side=Side.CT,
            source=DataSource.HLTV,
        )
    )
    # map 2 was parsed from a demo: its rounds must survive the HLTV sync untouched
    for r in session.scalars(select(Round).where(Round.map_played_id == maps[2].id)):
        session.delete(r)
    session.flush()
    session.add(
        Round(
            map_played_id=maps[2].id,
            round_number=1,
            team1_side=Side.T,
            winner_side=Side.T,
            source=DataSource.DEMO,
            ct_equipment_value=4100,
        )
    )
    session.flush()
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings)
    session.expire_all()
    assert count(session, Round) == 24 + 1 + 36
    repaired = session.scalar(select(Round).where(Round.map_played_id == maps[1].id, Round.round_number == 1))
    assert repaired is not None and repaired.winner_team_id == expected_winner
    assert session.scalar(select(Round).where(Round.round_number == 99)) is None
    demo_rows = list(session.scalars(select(Round).where(Round.map_played_id == maps[2].id)))
    assert len(demo_rows) == 1 and demo_rows[0].source is DataSource.DEMO
    assert demo_rows[0].ct_equipment_value == 4100


def test_links_pandascore_match_in_swapped_orientation(session: Session, app_settings: Settings) -> None:
    navi = resolve_team(session, name="Natus Vincere", source=DataSource.PANDASCORE, external_id=1001)
    vit = resolve_team(session, name="Team Vitality", source=DataSource.PANDASCORE, external_id=1002)
    assert navi is not None and vit is not None
    ps_zywoo = resolve_player(
        session, nickname="ZywOo", source=DataSource.PANDASCORE, external_id=777, team_id=vit.id
    )
    assert ps_zywoo is not None
    existing = Match(
        team1_id=navi.id,  # PandaScore lists NAVI first; HLTV lists Vitality first
        team2_id=vit.id,
        scheduled_at=datetime(2026, 11, 21, 16, 0, tzinfo=UTC),
        status=MatchStatus.SCHEDULED,
        format=MatchFormat.BO3,
        tier=Tier.S,
        pandascore_id=555,
        in_scope=True,
    )
    session.add(existing)
    session.flush()

    report = sync_match(session, FakeFetcher(), 2378001, settings=app_settings)
    assert report.ok and report.updated == 1 and report.created == 0
    assert count(session, Match) == 1 and count(session, Team) == 2
    m = session.get(Match, existing.id)
    assert m is not None and m.hltv_id == 2378001 and m.pandascore_id == 555
    assert (m.team1_id, m.team2_id) == (navi.id, vit.id)  # our orientation is kept
    assert m.scheduled_at == datetime(2026, 11, 21, 16, 0, tzinfo=UTC)  # PandaScore schedule is kept
    assert (m.team1_score, m.team2_score, m.winner_team_id) == (1, 2, vit.id)
    assert m.status is MatchStatus.FINISHED
    assert m.extra["hltv"]["team1_rank"] == 3 and m.extra["hltv"]["team2_rank"] == 1
    assert navi.hltv_id == 4608 and vit.hltv_id == 9565 and vit.pandascore_id == 1002

    maps = {mp.map_number: mp for mp in session.scalars(select(MapPlayed).where(MapPlayed.match_id == m.id))}
    mirage, inferno = maps[1], maps[2]
    assert (mirage.team1_rounds, mirage.team2_rounds, mirage.winner_team_id) == (13, 11, navi.id)
    assert mirage.team1_start_side is Side.T  # NAVI started on T (Vitality on CT)
    assert (mirage.team1_ct_rounds, mirage.team1_t_rounds, mirage.team2_ct_rounds, mirage.team2_t_rounds) == (
        6,
        7,
        5,
        6,
    )
    assert mirage.picked_by_team_id == navi.id
    assert (inferno.team1_rounds, inferno.team2_rounds) == (7, 13) and inferno.team1_start_side is Side.CT

    first_veto = session.scalar(select(Veto).where(Veto.match_id == m.id, Veto.step == 1))
    assert first_veto is not None and first_veto.team_id == navi.id

    assert ps_zywoo.hltv_id == 11893 and ps_zywoo.pandascore_id == 777  # same player row, linked
    zywoo_lineup = session.scalar(select(MatchLineup).where(MatchLineup.player_id == ps_zywoo.id))
    assert zywoo_lineup is not None and zywoo_lineup.team_id == vit.id
    zywoo_stats = list(session.scalars(select(PlayerMapStats).where(PlayerMapStats.player_id == ps_zywoo.id)))
    assert len(zywoo_stats) == 3 and {s.team_id for s in zywoo_stats} == {vit.id}

    anubis = maps[3]
    records = hltv_round_records(session, anubis)
    assert sum(r.winner is TeamSlot.TEAM2 for r in records) == 19  # Vitality is our team2 here
    assert records[0].team1_side is Side.T


def test_does_not_link_a_match_already_owned_by_another_hltv_id(
    session: Session, app_settings: Settings
) -> None:
    vit = resolve_team(session, name="Vitality", source=DataSource.HLTV, external_id=9565)
    navi = resolve_team(session, name="Natus Vincere", source=DataSource.HLTV, external_id=4608)
    assert vit is not None and navi is not None
    other = Match(
        team1_id=vit.id, team2_id=navi.id, scheduled_at=datetime(2026, 11, 21, 15, 0, tzinfo=UTC), hltv_id=1
    )
    session.add(other)
    session.flush()
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings)
    assert count(session, Match) == 2 and other.hltv_id == 1


def test_upcoming_without_veto_then_veto_published(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    report = sync_match(session, fetcher, 2378010, settings=app_settings, with_mapstats=False)
    assert report.ok
    m = match_by_hltv(session, 2378010)
    assert m.status is MatchStatus.SCHEDULED and m.team1_score is None and m.winner_team_id is None
    assert count(session, Veto) == 0 and count(session, MapPlayed) == 0 and count(session, Demo) == 0
    assert count(session, MatchLineup) == 10
    assert m.extra["hltv"]["has_veto"] is False and m.extra["hltv"]["complete"] is False

    sync_match(session, fetcher, 2378011, settings=app_settings, with_mapstats=False)
    m2 = match_by_hltv(session, 2378011)
    vetoes = list(session.scalars(select(Veto).where(Veto.match_id == m2.id)))
    assert len(vetoes) == 7 and count(session, MapPlayed) == 0  # maps are only stored once played
    molodoy = player(session, 20000)
    lineup = session.scalar(select(MatchLineup).where(MatchLineup.player_id == molodoy.id))
    assert lineup is not None and lineup.is_standin


def test_lineup_change_replaces_stale_hltv_rows(session: Session, app_settings: Settings) -> None:
    html = load_fixture("match_upcoming.html")
    swapped = html.replace('data-player-id="23054"', 'data-player-id="30001"').replace(">zweih<", ">newguy<")
    fetcher = FakeFetcher()
    sync_match(session, fetcher, 2378010, settings=app_settings, with_mapstats=False)
    m = match_by_hltv(session, 2378010)
    fetcher.routes[r"^/matches/2378010(/|$)"] = swapped
    sync_match(session, fetcher, 2378010, settings=app_settings, with_mapstats=False)
    nicks = {
        p.nickname
        for p in session.scalars(
            select(Player)
            .join(MatchLineup, MatchLineup.player_id == Player.id)
            .where(MatchLineup.match_id == m.id)
        )
    }
    assert "newguy" in nicks and "zweih" not in nicks and len(nicks) == 10


def test_postponed_forfeit_and_partial_forfeit(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    sync_match(session, fetcher, 2378020, settings=app_settings)
    assert match_by_hltv(session, 2378020).status is MatchStatus.POSTPONED

    report = sync_match(session, fetcher, 2378030, settings=app_settings)
    assert report.ok
    forfeit = match_by_hltv(session, 2378030)
    assert forfeit.status is MatchStatus.CANCELED and forfeit.team1_score is None
    assert forfeit.extra["hltv"]["forfeit"] is True
    assert forfeit.extra["hltv"]["forfeit_winner_team_id"] == team(session, 11283).id
    assert count(session, MapPlayed) == 0 and count(session, Demo) == 0

    report = sync_match(session, fetcher, 2378040, settings=app_settings, with_mapstats=False)
    bo5 = match_by_hltv(session, 2378040)
    assert bo5.status is MatchStatus.FINISHED and (bo5.team1_score, bo5.team2_score) == (3, 0)
    assert bo5.format is MatchFormat.BO5 and bo5.extra["hltv"]["forfeited_maps"] == [3]
    maps = list(
        session.scalars(select(MapPlayed).where(MapPlayed.match_id == bo5.id).order_by(MapPlayed.map_number))
    )
    assert [(mp.map_number, mp.map_name) for mp in maps] == [(1, "mirage"), (2, "overpass")]
    assert (
        maps[0].team1_ct_rounds == 6 and maps[0].team1_t_rounds == 7
    )  # regulation split from the match page
    assert bo5.extra["hltv"]["complete"] is False  # map stats were not fetched


def test_finished_match_status_never_regresses(session: Session, app_settings: Settings) -> None:
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings)
    live = load_fixture("match_bo3_finished.html").replace("Match over", "LIVE")
    sync_match(
        session,
        FakeFetcher({r"^/matches/2378001(/|$)": live}),
        2378001,
        settings=app_settings,
        with_mapstats=False,
    )
    assert match_by_hltv(session, 2378001).status is MatchStatus.FINISHED


def test_malformed_mapstats_page_does_not_abort_the_match(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher(
        {r"^/stats/matches/mapstatsid/210002(/|$)": "<html><body>maintenance</body></html>"}
    )
    report = sync_match(session, fetcher, 2378001, settings=app_settings)
    assert any("mapstats 210002" in e for e in report.errors)
    m = match_by_hltv(session, 2378001)
    assert m.status is MatchStatus.FINISHED and m.extra["hltv"]["complete"] is False
    assert count(session, PlayerMapStats) == 20  # the other two maps
    inferno = session.scalar(select(MapPlayed).where(MapPlayed.match_id == m.id, MapPlayed.map_number == 2))
    assert inferno is not None and inferno.team1_rounds == 13  # from the match page
    # once the page is fine again, a re-sync completes the match
    report2 = sync_match(session, FakeFetcher(), 2378001, settings=app_settings)
    assert report2.ok and count(session, PlayerMapStats) == 30
    assert match_by_hltv(session, 2378001).extra["hltv"]["complete"] is True


def test_mapstats_with_foreign_teams_is_rejected(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher({r"^/stats/matches/mapstatsid/210002(/|$)": "mapstats_210010.html"})
    report = sync_match(session, fetcher, 2378001, settings=app_settings)
    assert any("do not match" in e for e in report.errors)
    m = match_by_hltv(session, 2378001)
    inferno = session.scalar(select(MapPlayed).where(MapPlayed.match_id == m.id, MapPlayed.map_number == 2))
    assert inferno is not None and inferno.map_name == "inferno"  # not overwritten with "nuke"


def test_sync_mapstats_standalone(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    report = sync_mapstats(session, fetcher, 210001, settings=app_settings)
    assert report.skipped == 1 and not report.errors  # map row does not exist yet
    sync_match(session, fetcher, 2378001, settings=app_settings, with_mapstats=False)
    assert count(session, PlayerMapStats) == 0
    report = sync_mapstats(session, fetcher, 210001, settings=app_settings)
    assert report.ok and report.created == 1 and count(session, PlayerMapStats) == 10


def test_teams_tbd_are_skipped(session: Session, app_settings: Settings) -> None:
    html = load_fixture("match_upcoming.html")
    html = re.sub(r'<div class="team2-gradient">.*?</div>\s*</div>', "", html, count=1, flags=re.S)
    report = sync_match(
        session, FakeFetcher({r"^/matches/2378010(/|$)": html}), 2378010, settings=app_settings
    )
    assert report.skipped == 1 and count(session, Match) == 0


def test_blocked_match_fetch_is_reported(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher(
        {r"^/matches/2378001(/|$)": HLTVBlockedError("cf", reason="title", retry_after_s=60)}
    )
    report = sync_match(session, fetcher, 2378001, settings=app_settings)
    assert report.blocked and not report.ok and count(session, Match) == 0


# ------------------------------------------------------------------------------------------- results


def _results_without(*match_ids: int) -> str:
    html = load_fixture("results_offset0.html")
    for mid in match_ids:
        html = re.sub(
            rf'<div class="result-con"[^>]*><a href="/matches/{mid}/.*?</tr></tbody></table></div></a></div>',
            "",
            html,
            flags=re.S,
        )
    return html


def test_sync_results_walks_pages_until_since(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    report = sync_results(session, fetcher, settings=app_settings, since=datetime(2026, 11, 15, tzinfo=UTC))
    # relevant: two starred matches + the starred BO5; tier-3 rows and the unranked forfeit are skipped
    synced = {mid for (mid,) in session.execute(select(Match.hltv_id))}
    assert synced == {2378001, 2378002, 2378040}
    assert fetcher.requested(r"^/results\?offset=100$")  # page 1 had new rows -> page 2 fetched
    assert not fetcher.requested(r"^/results\?offset=200$")  # page 2 is older than `since`
    assert report.details["pages"] == 2 and report.details["matches_synced"] == 3
    assert all(
        age == HLTVSyncOptions().listing_ttl_minutes
        for path, age in fetcher.calls
        if path.startswith("/results")
    )
    nuke = session.scalar(
        select(MapPlayed).join(Match, Match.id == MapPlayed.match_id).where(Match.hltv_id == 2378002)
    )
    assert nuke is not None and nuke.overtime_rounds == 6 and nuke.team1_ct_rounds + nuke.team1_t_rounds == 16
    assert match_by_hltv(session, 2378002).extra["hltv"]["stars"] == 2


def test_sync_results_stops_when_nothing_new(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher({r"^/results$": _results_without(2378040)})
    since = datetime(2026, 11, 1, tzinfo=UTC)
    sync_results(session, fetcher, settings=app_settings, since=since)
    fetcher.calls.clear()
    report = sync_results(session, fetcher, settings=app_settings, since=since)
    assert report.created == 0 and report.details["matches_synced"] == 0
    assert fetcher.requested(r"^/results$") and not fetcher.requested(r"^/results\?offset=100$")
    assert not fetcher.requested(r"^/matches/")
    # a backfill can force walking on
    backfill = sync_results(
        session, fetcher, settings=app_settings, since=since, stop_when_nothing_new=False, max_pages=2
    )
    assert backfill.details["pages"] == 2


def test_sync_results_uses_db_ranks_for_unstarred_matches(session: Session, app_settings: Settings) -> None:
    ence = resolve_team(session, name="ENCE", source=DataSource.HLTV, external_id=4869)
    assert ence is not None
    ence.hltv_rank = 38  # top-50 -> the unstarred forfeit becomes relevant
    # the ranking history makes the test independent of today's date (old matches use history)
    session.add(TeamRanking(team_id=ence.id, source=DataSource.HLTV, rank=38, ranked_on=date(2026, 11, 16)))
    session.flush()
    fetcher = FakeFetcher()
    sync_results(session, fetcher, settings=app_settings, since=datetime(2026, 11, 15, tzinfo=UTC))
    assert match_by_hltv(session, 2378030).status is MatchStatus.CANCELED
    assert session.scalar(select(Match).where(Match.hltv_id == 2378100)) is None


def test_sync_results_out_of_scope_mode(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    sync_results(
        session,
        fetcher,
        settings=app_settings,
        since=datetime(2026, 11, 20, tzinfo=UTC),
        in_scope_only=False,
        max_pages=1,
    )
    synced = {mid for (mid,) in session.execute(select(Match.hltv_id))}
    # 2378100 (Kubix vs Rebels) has no fixture page -> reported, not fatal
    assert {2378001, 2378002, 2378030}.issubset(synced) and 2378040 not in synced  # older than since


def test_one_broken_match_page_does_not_abort_results(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher(
        {r"^/matches/2378002(/|$)": "<html><body><h1>500 Internal error</h1></body></html>"}
    )
    report = sync_results(session, fetcher, settings=app_settings, since=datetime(2026, 11, 15, tzinfo=UTC))
    assert report.failed >= 1 and any("2378002" in e for e in report.errors)
    synced = {mid for (mid,) in session.execute(select(Match.hltv_id))}
    assert {2378001, 2378040} <= synced and 2378002 not in synced


def test_block_during_results_stops_and_keeps_progress(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher(
        {r"^/matches/2378002(/|$)": HLTVBlockedError("cf", reason="title", retry_after_s=900)}
    )
    report = sync_results(session, fetcher, settings=app_settings, since=datetime(2026, 11, 15, tzinfo=UTC))
    assert report.blocked
    synced = {mid for (mid,) in session.execute(select(Match.hltv_id))}
    assert synced == {2378001}  # synced before the block, never anything after it
    assert not fetcher.requested(r"^/matches/2378040")


def test_sync_results_commit_every(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    opts = HLTVSyncOptions(commit_every=1)
    sync_results(
        session, fetcher, settings=app_settings, since=datetime(2026, 11, 15, tzinfo=UTC), options=opts
    )
    session.rollback()  # nothing to lose: every match was committed
    assert count(session, Match) == 3


# ------------------------------------------------------------------------------------------- upcoming


def test_sync_upcoming(session: Session, app_settings: Settings) -> None:
    now = datetime(2026, 11, 22, 18, 45, tzinfo=UTC)
    fetcher = FakeFetcher()
    report = sync_upcoming(session, fetcher, settings=app_settings, now=now)
    synced = {mid for (mid,) in session.execute(select(Match.hltv_id))}
    # starred matches with known teams; live, TBD and unstarred unranked matches are skipped
    assert synced == {2378010, 2378011}
    assert report.skipped == 5 and report.details["listed"] == 7
    ages = dict(fetcher.calls)
    near = next(path for path in ages if path.startswith("/matches/2378011"))
    far = next(path for path in ages if path.startswith("/matches/2378010"))
    assert ages[near] == HLTVSyncOptions().near_start_ttl_minutes  # starts in 45 min: vetoes / lineups appear
    assert ages[far] == HLTVSyncOptions().upcoming_match_ttl_minutes
    assert ages["/matches"] == HLTVSyncOptions().listing_ttl_minutes
    assert not fetcher.requested(r"^/stats/")  # no map stats for upcoming matches
    assert match_by_hltv(session, 2378011).extra["hltv"]["stars"] == 1


def test_sync_upcoming_uses_rankings_and_horizon(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    sync_rankings(session, fetcher)  # Heroic #17, Astralis #21 -> the unstarred postponed match is relevant
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    sync_upcoming(session, fetcher, settings=app_settings, now=now)
    assert match_by_hltv(session, 2378020).status is MatchStatus.POSTPONED
    narrow = app_settings.model_copy(
        update={"scope": app_settings.scope.model_copy(update={"max_hours_ahead": 12})}
    )
    fetcher.calls.clear()
    sync_upcoming(session, fetcher, settings=narrow, now=now)
    assert not fetcher.requested(r"^/matches/2378010")  # 30 h ahead: outside the horizon


# ------------------------------------------------------------------------------------------- rankings


def test_sync_rankings(session: Session) -> None:
    stale = resolve_team(session, name="Old Guard", source=DataSource.HLTV, external_id=999)
    assert stale is not None
    stale.hltv_rank, stale.hltv_rank_updated_at = 12, datetime(2026, 11, 9, tzinfo=UTC)
    report = sync_rankings(session, FakeFetcher())
    assert report.ok and report.updated == 32
    vit = team(session, 9565)
    assert vit.hltv_rank == 1 and vit.hltv_rank_updated_at == datetime(2026, 11, 16, tzinfo=UTC)
    big = team(session, 7532)
    assert big.name == "BIG" and big.hltv_rank == 12
    rows = list(session.scalars(select(TeamRanking).where(TeamRanking.ranked_on == date(2026, 11, 16))))
    assert len(rows) == 32 and {r.source for r in rows} == {DataSource.HLTV}
    assert next(r for r in rows if r.team_id == vit.id).points == 1000
    assert stale.hltv_rank is None  # dropped out of the latest full ranking
    again = sync_rankings(session, FakeFetcher())
    assert again.ok and count(session, TeamRanking) == 32


def test_older_ranking_does_not_override_newer(session: Session) -> None:
    fetcher = FakeFetcher()
    sync_rankings(session, fetcher)
    vit = team(session, 9565)
    old = load_fixture("ranking_2026_11_16.html").replace("November 16th, 2026", "November 2nd, 2026")
    old = old.replace('<span class="position">#1</span>', '<span class="position">#7</span>', 1)
    report = sync_rankings(session, FakeFetcher({r"^/ranking/teams": old}), date(2026, 11, 2))
    assert report.ok and vit.hltv_rank == 1
    hist = session.scalar(
        select(TeamRanking).where(TeamRanking.team_id == vit.id, TeamRanking.ranked_on == date(2026, 11, 2))
    )
    assert hist is not None and hist.rank == 7
    assert FakeFetcher._path(fetcher.calls[0][0]) == "/ranking/teams"


def test_sync_rankings_empty_page_is_an_error(session: Session) -> None:
    report = sync_rankings(session, FakeFetcher({r"^/ranking/teams": "<html><body>new layout</body></html>"}))
    assert not report.ok and count(session, TeamRanking) == 0


# ------------------------------------------------------------------------------------------- news


def test_sync_news_tags_teams_and_players(session: Session, app_settings: Settings) -> None:
    fetcher = FakeFetcher()
    sync_match(session, fetcher, 2378001, settings=app_settings)  # teams + players known
    sync_match(session, fetcher, 2378002, settings=app_settings)  # MOUZ / FaZe (+ "rain")
    aurora = resolve_team(session, name="Aurora", source=DataSource.HLTV, external_id=11595)
    big = resolve_team(session, name="BIG", source=DataSource.HLTV, external_id=7532)
    assert aurora is not None and big is not None
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    report = sync_news(session, fetcher, months=1, now=now)
    assert report.ok, report.errors
    items = {n.url: n for n in session.scalars(select(NewsItem))}
    # the archive lists 4 articles; only 43100 has a fixture page, the others are reported as missing
    article = items["https://www.hltv.org/news/43100/natus-vincere-bench-jl-iam-to-stand-in"]
    assert article.source == "hltv" and article.title == "Natus Vincere bench jL, iM steps up to IGL"
    assert article.published_at == datetime.fromtimestamp(1795290000, tz=UTC)
    assert article.body and "in-game leading" in article.body
    navi, vit, faze = team(session, 4608), team(session, 9565), team(session, 6667)
    assert {navi.id, vit.id, faze.id, aurora.id} <= set(article.team_ids)
    assert big.id not in article.team_ids
    jl, zywoo, im = player(session, 19206), player(session, 11893), player(session, 19230)
    assert {jl.id, zywoo.id, im.id} <= set(article.player_ids)
    assert player(session, 8183).id not in article.player_ids  # "rain" only counts when quoted
    assert article.extra["hltv"]["author"] == "Striker" and article.extra["hltv"]["comments"] == 412
    # second run: stored articles are not fetched again
    fetcher.calls.clear()
    sync_news(session, fetcher, months=1, now=now)
    assert not fetcher.requested(r"^/news/43100")


def test_sync_news_window_and_article_cap(session: Session) -> None:
    fetcher = FakeFetcher(
        {
            r"^/news/43098(/|$)": "news_article_43100.html",
            r"^/news/43095(/|$)": "news_article_43100.html",
            r"^/news/archive/2026/october$": "<html><body></body></html>",
        }
    )
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    report = sync_news(session, fetcher, months=1, now=now, options=HLTVSyncOptions(max_news_articles=2))
    assert report.details["articles_fetched"] == 2 and report.details["truncated"] is True
    assert count(session, NewsItem) == 2
    assert fetcher.requested(r"^/news/archive/2026/november$")
    archive_ages = {p: a for p, a in fetcher.calls if p.startswith("/news/archive")}
    assert archive_ages["/news/archive/2026/november"] == HLTVSyncOptions().listing_ttl_minutes


def test_news_older_than_window_is_ignored(session: Session) -> None:
    fetcher = FakeFetcher({r"^/news/archive/2027/january$": "news_archive_2026_november.html"})
    report = sync_news(session, fetcher, months=1, now=datetime(2027, 1, 30, tzinfo=UTC))
    # window = last 31 days: the January and December archives; every listed article is too old
    assert fetcher.requested(r"^/news/archive/2027/january$") and fetcher.requested(
        r"^/news/archive/2026/december$"
    )
    assert not fetcher.requested(r"^/news/archive/2026/november$")
    assert count(session, NewsItem) == 0 and not fetcher.requested(r"^/news/43")
    assert report.details["articles_fetched"] == 0 and not report.errors


# ------------------------------------------------------------------------------------------- scope


def test_scope_fallback_and_external_rule(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setitem(sys.modules, "cs2bot.ingest.scope", None)  # module missing -> fallback rule
    assert sync_common.in_scope(None, 12, None, settings.scope)
    assert not sync_common.in_scope(None, 51, 90, settings.scope)
    assert sync_common.in_scope(Tier.A, None, None, settings.scope)
    assert not sync_common.in_scope(Tier.C, None, None, settings.scope)
    fake = types.ModuleType("cs2bot.ingest.scope")
    calls: list[tuple[object, ...]] = []

    def is_in_scope(tier: Tier, r1: int | None, r2: int | None, scope: object) -> bool:
        calls.append((tier, r1, r2))
        return r1 == 99

    fake.is_in_scope = is_in_scope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cs2bot.ingest.scope", fake)
    assert sync_common.in_scope(None, 99, None, settings.scope)
    assert not sync_common.in_scope(None, 1, None, settings.scope)
    assert calls[0] == (Tier.UNRANKED, 99, None)


def test_player_current_team_follows_newest_sighting(session: Session) -> None:
    a = resolve_team(session, name="Alpha", source=DataSource.HLTV, external_id=1)
    b = resolve_team(session, name="Bravo", source=DataSource.HLTV, external_id=2)
    assert a is not None and b is not None
    newer = datetime(2026, 11, 20, tzinfo=UTC)
    p = sync_common.resolve_hltv_player(session, nickname="mover", hltv_id=5, team_id=b.id, seen_at=newer)
    assert p is not None and p.current_team_id == b.id
    # backfill: an older match on the old team must not move the player back
    sync_common.resolve_hltv_player(
        session, nickname="mover", hltv_id=5, team_id=a.id, seen_at=newer - timedelta(days=90)
    )
    assert p.current_team_id == b.id
    sync_common.resolve_hltv_player(
        session, nickname="mover2", hltv_id=5, team_id=a.id, seen_at=newer + timedelta(days=1)
    )
    assert p.current_team_id == a.id and p.nickname == "mover2"


class _StaleCacheFetcher(FakeFetcher):
    """Serves a pre-kick-off copy of match 2378001 unless a refresh (max_age 0) is requested."""

    def get(self, path_or_url: str, *, max_age_minutes: int | None = None) -> str:
        html = super().get(path_or_url, max_age_minutes=max_age_minutes)
        if self._path(path_or_url).startswith("/matches/2378001") and max_age_minutes != 0:
            return html.replace("Match over", "0 : 00 : 10 : 00")
        return html


def test_results_sync_refreshes_pages_cached_before_kickoff(session: Session, app_settings: Settings) -> None:
    fetcher = _StaleCacheFetcher({r"^/results$": _results_without(2378002, 2378040)})
    report = sync_results(session, fetcher, settings=app_settings, since=datetime(2026, 11, 15, tzinfo=UTC))
    assert report.ok
    assert [age for path, age in fetcher.calls if path.startswith("/matches/2378001")] == [None, 0]
    m = match_by_hltv(session, 2378001)
    assert m.status is MatchStatus.FINISHED and m.extra["hltv"]["complete"] is True


def test_standin_does_not_move_a_known_players_team(session: Session, app_settings: Settings) -> None:
    home = resolve_team(session, name="Home Team", source=DataSource.HLTV, external_id=4242)
    assert home is not None
    known = sync_common.resolve_hltv_player(
        session, nickname="molodoy", hltv_id=20000, team_id=home.id, seen_at=datetime(2026, 11, 1, tzinfo=UTC)
    )
    assert known is not None
    sync_match(session, FakeFetcher(), 2378011, settings=app_settings, with_mapstats=False)
    lineup = session.scalar(select(MatchLineup).where(MatchLineup.player_id == known.id))
    assert lineup is not None and lineup.is_standin and lineup.team_id == team(session, 8297).id
    assert known.current_team_id == home.id  # still his own team
    regular = player(session, 9216)  # FalleN, not a stand-in
    assert regular.current_team_id == team(session, 8297).id


_OLD_RESULT_ROW = """<div class="results-sublist">
<div class="standard-headline">Results for March 1st 2024</div>
<div class="result-con" data-zonedgrouping-entry-unix="1709301600000">
<a href="/matches/2300001/ence-vs-nobodies" class="a-reset"><div class="result"><table><tbody><tr>
<td class="team-cell"><div class="line-align team1"><div class="team team-won">ENCE</div></div></td>
<td class="result-score"><span class="score-won">2</span> - <span class="score-lost">0</span></td>
<td class="team-cell"><div class="line-align team2"><div class="team">Nobodies</div></div></td>
<td class="event"><span class="event-name">Some Cup 2024</span></td>
<td class="star-cell"><div class="map-text">bo3</div></td>
</tr></tbody></table></div></a></div></div>"""


def test_backfill_relevance_uses_rank_at_match_time(session: Session, app_settings: Settings) -> None:
    """A team that is top-5 *today* does not make its unstarred 2024 matches relevant by itself."""
    ence = resolve_team(session, name="ENCE", source=DataSource.HLTV, external_id=4869)
    assert ence is not None
    ence.hltv_rank, ence.hltv_rank_updated_at = 5, datetime(2026, 11, 16, tzinfo=UTC)
    session.flush()
    html = f"<html><body><div class='results-all'>{_OLD_RESULT_ROW}</div></body></html>"
    fetcher = FakeFetcher({r"^/results": html}, use_defaults=False)
    since = datetime(2024, 1, 1, tzinfo=UTC)
    report = sync_results(session, fetcher, settings=app_settings, since=since, max_pages=1)
    assert not fetcher.requested(r"^/matches/2300001") and report.skipped == 1

    # with a ranking from that time (rank 7 in February 2024) the match is relevant
    session.add(TeamRanking(team_id=ence.id, source=DataSource.HLTV, rank=7, ranked_on=date(2024, 2, 26)))
    session.flush()
    sync_results(session, fetcher, settings=app_settings, since=since, max_pages=1)
    assert fetcher.requested(r"^/matches/2300001")


def test_foreign_host_links_are_reported_not_raised(session: Session, tmp_path: object) -> None:
    """A scraped link to another host is refused by the real fetcher and must not abort the sync."""
    from pathlib import Path

    from cs2bot.config import HLTVSettings
    from cs2bot.ingest.hltv import HLTVFetcher

    archive = (
        "<html><body>"
        '<a class="newsline article" href="https://evil.example.com/news/43100/x">'
        '<div class="newstext">Phishing</div><div class="newsrecent">2026-11-21</div></a>'
        '<a class="newsline article" href="/news/43100/natus-vincere-bench-jl-iam-to-stand-in">'
        '<div class="newstext">NAVI bench jL</div><div class="newsrecent">2026-11-21</div></a>'
        "</body></html>"
    )
    article = load_fixture("news_article_43100.html")
    urls: list[str] = []

    def loader(url: str) -> str:
        urls.append(url)
        return archive if "/news/archive/" in url else article

    assert isinstance(tmp_path, Path)
    settings = HLTVSettings(cache_dir=tmp_path / "c", min_delay_s=0.0, max_delay_s=0.0)
    with HLTVFetcher(settings, page_loader=loader, sleep=lambda _s: None) as fetcher:
        report = sync_news(session, fetcher, months=1, now=datetime(2026, 11, 22, tzinfo=UTC))
    assert any("non-HLTV URL" in e for e in report.errors) and report.failed >= 1
    assert not any("evil" in u for u in urls)
    assert count(session, NewsItem) == 1


def test_one_missing_lineup_block_keeps_that_teams_stored_lineup(
    session: Session, app_settings: Settings
) -> None:
    html = load_fixture("match_upcoming.html")
    fetcher = FakeFetcher()
    sync_match(session, fetcher, 2378010, settings=app_settings, with_mapstats=False)
    m = match_by_hltv(session, 2378010)
    assert count(session, MatchLineup) == 10
    # the second team's block lost its player cells (layout hiccup / not shown): keep its stored rows
    start = html.index(
        '<div class="lineup standard-box">', html.index('<div class="lineup standard-box">') + 1
    )
    end = html.index("</table>", start)
    broken = html[:start] + '<div class="lineup standard-box"><div class="players"><table>' + html[end:]
    fetcher.routes[r"^/matches/2378010(/|$)"] = broken
    sync_match(session, fetcher, 2378010, settings=app_settings, with_mapstats=False)
    rows = list(session.scalars(select(MatchLineup).where(MatchLineup.match_id == m.id)))
    assert len(rows) == 10 and {r.team_id for r in rows} == {m.team1_id, m.team2_id}


def test_latest_ranking_with_settings_refreshes_upcoming_scope(session: Session, settings: Settings) -> None:
    heroic = resolve_team(session, name="Heroic", source=DataSource.HLTV, external_id=7175)
    nobody = resolve_team(session, name="Nobody Five", source=DataSource.HLTV, external_id=999_001)
    assert heroic is not None and nobody is not None
    upcoming = Match(
        team1_id=nobody.id,
        team2_id=heroic.id,
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
        status=MatchStatus.SCHEDULED,
    )
    session.add(upcoming)
    session.flush()
    assert not upcoming.in_scope
    sync_rankings(session, FakeFetcher())  # without settings: ranks only
    assert not upcoming.in_scope
    report = sync_rankings(session, FakeFetcher(), settings=settings)
    assert report.ok and upcoming.in_scope and report.details["scope_changed"] == 1
