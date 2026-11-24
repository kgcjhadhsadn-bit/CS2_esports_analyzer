"""PandaScore -> DB sync: upserts, rescheduling, linking with HLTV, robustness, idempotency."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import PandaScoreSettings, Settings
from cs2bot.db.base import Base
from cs2bot.db.models import (
    MapPlayed,
    Match,
    MatchLineup,
    Player,
    PlayerMapStats,
    Team,
    TeamAlias,
    TeamRanking,
    Tournament,
)
from cs2bot.db.repo import resolve_player, resolve_team
from cs2bot.domain.enums import DataSource, MatchFormat, MatchStatus, RoundEndReason, Side, TeamSlot, Tier
from cs2bot.domain.sync import SyncReport
from cs2bot.ingest.pandascore import (
    PandaScoreClient,
    PandaScoreSyncOptions,
    pandascore_round_records,
    sync_match,
    sync_modified,
    sync_past,
    sync_upcoming,
)
from tests.ingest.pandascore.conftest import (
    COLOGNE_PLAYOFFS_ID,
    FINISHED_ID,
    MAJOR_PLAYOFFS_ID,
    NOW,
    PS_G2,
    PS_NAVI,
    PS_SPIRIT,
    PS_VITALITY,
    UPCOMING_ID,
    FakePandaScore,
    NoWait,
    finished_games,
    finished_match,
    load_json,
    make_match,
    ps_team,
    upcoming_match,
)

SINCE = datetime(2026, 11, 20, tzinfo=UTC)
KICKOFF = datetime(2026, 11, 21, 16, 30, tzinfo=UTC)


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def team_by_ps(session: Session, ps_id: int) -> Team:
    row = session.scalar(select(Team).where(Team.pandascore_id == ps_id))
    assert row is not None
    return row


def match_by_ps(session: Session, ps_id: int) -> Match:
    row = session.scalar(select(Match).where(Match.pandascore_id == ps_id))
    assert row is not None
    return row


def maps_of(session: Session, match: Match) -> dict[int, MapPlayed]:
    rows = session.scalars(select(MapPlayed).where(MapPlayed.match_id == match.id))
    return {row.map_number: row for row in rows}


def lineups_of(session: Session, match: Match) -> list[MatchLineup]:
    return list(session.scalars(select(MatchLineup).where(MatchLineup.match_id == match.id)))


def snapshot(session: Session) -> dict[str, list[dict[str, Any]]]:
    """Every row of every table, without the bookkeeping timestamps a re-run is allowed to touch."""
    session.flush()
    session.expire_all()
    volatile = {"updated_at", "created_at", "last_synced_at"}
    out: dict[str, list[dict[str, Any]]] = {}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        columns = [c.key for c in inspect(model).mapper.column_attrs if c.key not in volatile]
        rows = [{c: getattr(row, c) for c in columns} for row in session.scalars(select(model))]
        out[model.__tablename__] = sorted(rows, key=lambda r: str(r.get("id")))
    return out


def serve_finished(api: FakePandaScore) -> None:
    api.past = [finished_match()]
    api.games[FINISHED_ID] = finished_games()
    api.rosters[COLOGNE_PLAYOFFS_ID] = load_json("rosters_iem_cologne_playoffs.json")


def serve_upcoming(api: FakePandaScore) -> None:
    api.upcoming = [upcoming_match()]
    api.rosters[MAJOR_PLAYOFFS_ID] = load_json("rosters_major_playoffs.json")


# ------------------------------------------------------------------------------------- upcoming


def test_sync_upcoming_creates_match_with_entities_and_lineups(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_upcoming(api)
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert isinstance(report, SyncReport) and report.job == "pandascore.sync_upcoming"
    assert report.ok, report.errors
    assert (report.created, report.updated, report.skipped, report.failed) == (1, 0, 0, 0)

    match = match_by_ps(session, UPCOMING_ID)
    spirit, g2 = team_by_ps(session, PS_SPIRIT), team_by_ps(session, PS_G2)
    assert report.match_ids == [match.id]
    assert (match.team1_id, match.team2_id) == (spirit.id, g2.id)  # PandaScore's opponent order
    assert match.scheduled_at == datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
    assert match.status is MatchStatus.SCHEDULED and match.format is MatchFormat.BO3
    assert match.tier is Tier.S and match.in_scope is True and match.is_lan is True
    assert match.stage == "Playoffs - Quarterfinal 2"
    assert (match.team1_score, match.team2_score, match.winner_team_id) == (None, None, None)
    assert match.last_synced_at == NOW and match.hltv_id is None
    assert match.extra["pandascore"]["slug"] == "g2-vs-team-spirit-2026-11-23"
    assert match.extra["pandascore"]["forfeit"] is False
    assert count(session, MapPlayed) == 0

    tournament = session.get(Tournament, match.tournament_id)
    assert tournament is not None and tournament.pandascore_id == MAJOR_PLAYOFFS_ID
    assert tournament.name == "StarLadder Major Budapest 2026 - Playoffs"
    assert tournament.tier is Tier.S and tournament.is_lan is True
    assert tournament.prize_pool_usd == 1_250_000 and tournament.pandascore_serie_id == 9844
    assert tournament.start_at == datetime(2026, 11, 23, 14, 0, tzinfo=UTC) and tournament.location == "HU"

    assert spirit.name == "Team Spirit" and spirit.country == "RU" and spirit.slug == "team-spirit-cs-go"
    assert spirit.extra["pandascore"]["acronym"] == "TS"
    aliases = {(a.alias, a.source) for a in session.scalars(select(TeamAlias))}
    assert aliases == {("spirit", DataSource.PANDASCORE), ("g2", DataSource.PANDASCORE)}

    lineups = lineups_of(session, match)
    assert len(lineups) == 10 and {lu.source for lu in lineups} == {DataSource.PANDASCORE}
    assert sum(lu.team_id == spirit.id for lu in lineups) == 5
    donk = session.scalar(select(Player).where(Player.pandascore_id == 30412))
    assert donk is not None and donk.nickname == "donk" and donk.real_name == "Danil Kryshkovets"
    assert donk.country == "RU" and donk.current_team_id == spirit.id
    # one list page for upcoming, one for running, one roster call for the tournament stage
    assert report.fetched_pages == 2 and report.details["api_requests"] == 3
    assert api.paths() == ["/csgo/matches/upcoming", "/tournaments/17260/rosters", "/csgo/matches/running"]


def test_sync_upcoming_requests_the_configured_horizon(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    far = make_match(900, ps_team(1, "Alpha"), ps_team(2, "Bravo"), scheduled_at="2026-11-27T12:00:00Z")
    api.upcoming = [upcoming_match(), far]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    params = api.params("/csgo/matches/upcoming")[0]
    assert params["range[begin_at]"] == "2026-11-22T11:00:00Z,2026-11-25T12:00:00Z"  # now-1h .. now+72h
    assert params["filter[videogame_title]"] == "cs-2" and params["sort"] == "begin_at"
    assert report.created == 1 and count(session, Match) == 1  # the match 5 days ahead is not listed


def test_sync_twice_is_idempotent(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_upcoming(api)
    serve_finished(api)
    first = [
        sync_upcoming(session, client, app_settings, now=NOW),
        sync_past(session, client, app_settings, SINCE, now=NOW),
    ]
    assert [r.created for r in first] == [1, 1] and all(r.ok for r in first)
    before = snapshot(session)
    assert len(before["matches"]) == 2 and len(before["maps_played"]) == 3

    later = NOW + timedelta(minutes=30)
    again = [
        sync_upcoming(session, client, app_settings, now=later),
        sync_past(session, client, app_settings, SINCE, now=later),
    ]
    for report in again:
        assert report.ok, report.errors
        assert (report.created, report.updated, report.failed) == (0, 0, 0)
        assert report.details["unchanged"] == 1
    assert snapshot(session) == before
    assert match_by_ps(session, UPCOMING_ID).last_synced_at == later
    # the finished match's game details are final: they are fetched exactly once
    assert len(api.paths(r"/games$")) == 1


def test_rescheduled_match_is_updated(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_upcoming(api)
    sync_upcoming(session, client, app_settings, now=NOW)
    match = match_by_ps(session, UPCOMING_ID)

    moved = upcoming_match()
    moved.update(
        scheduled_at="2026-11-24T15:30:00Z",
        begin_at="2026-11-24T15:30:00Z",
        rescheduled=True,
        modified_at="2026-11-22T13:00:00Z",
        number_of_games=5,
    )
    api.upcoming = [moved]
    report = sync_upcoming(session, client, app_settings, now=NOW + timedelta(hours=2))
    assert (report.created, report.updated) == (0, 1) and report.details["rescheduled"] == 1
    assert count(session, Match) == 1
    assert match.scheduled_at == datetime(2026, 11, 24, 15, 30, tzinfo=UTC)
    assert match.format is MatchFormat.BO5 and match.status is MatchStatus.SCHEDULED
    assert match.extra["pandascore"]["previous_scheduled_at"] == "2026-11-23T18:00:00+00:00"
    assert match.extra["pandascore"]["rescheduled"] is True
    assert len(lineups_of(session, match)) == 10  # lineups survive


def test_tbd_opponents_are_skipped_until_both_teams_are_known(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    g2 = ps_team(PS_G2, "G2", "G2")
    api.upcoming = [make_match(2001, g2, None), make_match(2002, None, None), upcoming_match()]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert (report.created, report.skipped) == (1, 2) and report.details["tbd"] == 2
    assert report.ok and count(session, Match) == 1

    api.upcoming = [make_match(2001, g2, ps_team(PS_VITALITY, "Vitality", "VIT")), upcoming_match()]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert (report.created, report.skipped) == (1, 0)
    decided = match_by_ps(session, 2001)
    assert decided.team1_id == team_by_ps(session, PS_G2).id
    assert decided.team2_id == team_by_ps(session, PS_VITALITY).id
    assert count(session, Team) == 3  # G2 is one row for both of its matches


def test_running_match_turns_live(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_upcoming(api)
    sync_upcoming(session, client, app_settings, now=NOW)
    live = upcoming_match()
    live.update(status="running", begin_at="2026-11-23T18:04:11Z")
    api.upcoming, api.running = [], [live]
    report = sync_upcoming(session, client, app_settings, now=datetime(2026, 11, 23, 18, 30, tzinfo=UTC))
    match = match_by_ps(session, UPCOMING_ID)
    assert report.updated == 1 and match.status is MatchStatus.LIVE
    assert match.scheduled_at == datetime(2026, 11, 23, 18, 0, tzinfo=UTC)  # the official time stays


# ------------------------------------------------------------------------------------- results


def test_sync_past_writes_result_maps_rounds_and_lineups(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_finished(api)
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok, report.errors
    assert report.created == 1 and report.job == "pandascore.sync_past"
    params = api.params("/csgo/matches/past")[0]
    assert params["range[begin_at]"] == "2026-11-20T00:00:00Z,2026-11-22T12:00:00Z"

    match = match_by_ps(session, FINISHED_ID)
    navi, vitality = team_by_ps(session, PS_NAVI), team_by_ps(session, PS_VITALITY)
    assert (match.team1_id, match.team2_id) == (navi.id, vitality.id)
    assert match.status is MatchStatus.FINISHED and match.format is MatchFormat.BO3
    assert (match.team1_score, match.team2_score, match.winner_team_id) == (1, 2, vitality.id)
    assert match.scheduled_at == KICKOFF and match.tier is Tier.S and match.in_scope
    assert match.stage == "Playoffs - Semifinal 1"

    maps = maps_of(session, match)
    assert [maps[n].map_name for n in (1, 2, 3)] == ["mirage", "inferno", "anubis"]
    mirage, inferno, anubis = maps[1], maps[2], maps[3]
    assert (mirage.team1_rounds, mirage.team2_rounds, mirage.winner_team_id) == (13, 11, navi.id)
    assert mirage.status is MatchStatus.FINISHED and mirage.overtime_rounds == 0
    assert mirage.played_at == datetime(2026, 11, 21, 16, 36, 4, tzinfo=UTC)
    assert mirage.team1_start_side is Side.T  # NAVI began on the T side
    assert (mirage.team1_ct_rounds, mirage.team1_t_rounds, mirage.team2_ct_rounds, mirage.team2_t_rounds) == (
        6,
        7,
        5,
        6,
    )
    assert (inferno.team1_rounds, inferno.team2_rounds, inferno.winner_team_id) == (7, 13, vitality.id)
    assert inferno.team1_start_side is Side.CT
    assert (anubis.team1_rounds, anubis.team2_rounds, anubis.overtime_rounds) == (17, 19, 12)
    assert anubis.team1_ct_rounds + anubis.team1_t_rounds == 17
    assert anubis.extra["pandascore"]["game_id"] == 2301103 and anubis.hltv_mapstats_id is None

    records = pandascore_round_records(session, anubis)
    assert len(records) == 36 and sum(r.winner is TeamSlot.TEAM2 for r in records) == 19
    assert records[0].team1_side is Side.T and records[0].team1_id == navi.id
    assert records[-1].team1_score_before + records[-1].team2_score_before == 35
    assert {r.end_reason for r in records} <= set(RoundEndReason) - {RoundEndReason.OTHER}

    # the players who actually played (stat lines) become the lineup; no roster call is needed
    lineups = lineups_of(session, match)
    assert len(lineups) == 10 and {lu.source for lu in lineups} == {DataSource.PANDASCORE}
    zywoo = session.scalar(select(Player).where(Player.pandascore_id == 17512))
    assert zywoo is not None and zywoo.nickname == "ZywOo" and zywoo.current_team_id == vitality.id
    assert api.paths(r"rosters") == [] and len(api.paths(r"/games$")) == 1
    assert count(session, PlayerMapStats) == 0  # opt-in only


def test_player_stats_are_written_on_request(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_finished(api)
    options = PandaScoreSyncOptions(write_player_stats=True)
    report = sync_past(session, client, app_settings, SINCE, options=options, now=NOW)
    assert report.ok
    stats = list(session.scalars(select(PlayerMapStats)))
    assert len(stats) == 30 and {s.source for s in stats} == {DataSource.PANDASCORE}
    line = finished_games()[0]["players"][5]  # ZywOo on Mirage
    zywoo = session.scalar(select(Player).where(Player.pandascore_id == line["player"]["id"]))
    mirage = maps_of(session, match_by_ps(session, FINISHED_ID))[1]
    assert zywoo is not None
    row = next(s for s in stats if s.player_id == zywoo.id and s.map_played_id == mirage.id)
    assert (row.kills, row.deaths, row.assists) == (line["kills"], line["deaths"], line["assists"])
    assert row.rounds_played == 24 and row.team_id == team_by_ps(session, PS_VITALITY).id
    again = sync_past(session, client, app_settings, SINCE, options=options, now=NOW)
    assert (again.created, again.updated) == (0, 0) and count(session, PlayerMapStats) == 30


def test_upcoming_match_later_gets_its_result(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    before = finished_match()
    before.update(
        status="not_started", winner=None, winner_id=None, end_at=None, begin_at=before["scheduled_at"]
    )
    before["results"] = [{"score": 0, "team_id": PS_NAVI}, {"score": 0, "team_id": PS_VITALITY}]
    for game in before["games"]:
        game.update(status="not_started", finished=False, complete=False, winner={"id": None, "type": "Team"})
    api.upcoming = [before]
    api.rosters[COLOGNE_PLAYOFFS_ID] = load_json("rosters_iem_cologne_playoffs.json")
    early = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    assert sync_upcoming(session, client, app_settings, now=early).created == 1
    match = match_by_ps(session, FINISHED_ID)
    assert match.status is MatchStatus.SCHEDULED and len(lineups_of(session, match)) == 10

    api.upcoming = []
    serve_finished(api)
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert (report.created, report.updated) == (0, 1) and count(session, Match) == 1
    assert match.status is MatchStatus.FINISHED and (match.team1_score, match.team2_score) == (1, 2)
    assert len(maps_of(session, match)) == 3 and len(lineups_of(session, match)) == 10


def test_bo2_draw_and_forfeit(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    alpha, bravo = ps_team(11, "Alpha Five", "A5"), ps_team(12, "Bravo Six", "B6")
    draw = make_match(
        3001,
        alpha,
        bravo,
        status="finished",
        scheduled_at="2026-11-21T10:00:00Z",
        match_type="all_games_played",
        number_of_games=2,
        scores=(1, 1),
        draw=True,
        tier="a",
    )
    walkover = make_match(
        3002,
        alpha,
        bravo,
        status="canceled",
        scheduled_at="2026-11-21T14:00:00Z",
        scores=(0, 0),
        forfeit=True,
        winner=bravo,
        winner_id=12,
        tier="a",
        tournament_id=17300,
    )
    api.past = [draw, walkover]
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok and report.created == 2
    tie = match_by_ps(session, 3001)
    assert tie.format is MatchFormat.BO2 and tie.status is MatchStatus.FINISHED
    assert (tie.team1_score, tie.team2_score, tie.winner_team_id) == (1, 1, None)
    forfeited = match_by_ps(session, 3002)
    assert forfeited.status is MatchStatus.CANCELED and forfeited.winner_team_id is None
    assert forfeited.extra["pandascore"]["forfeit"] is True
    assert forfeited.extra["pandascore"]["forfeit_winner_team_id"] == team_by_ps(session, 12).id
    assert count(session, MapPlayed) == 0


def test_games_endpoint_outside_the_plan_is_a_warning(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_finished(api)
    second = make_match(
        3100,
        ps_team(PS_G2, "G2", "G2"),
        ps_team(PS_SPIRIT, "Team Spirit", "TS"),
        status="finished",
        scheduled_at="2026-11-21T20:00:00Z",
        scores=(2, 0),
    )
    api.past.append(second)
    api.override(r"/games$", lambda request: httpx.Response(403, json={"error": "Forbidden"}))
    api.override(r"/rosters$", lambda request: httpx.Response(403, json={"error": "Forbidden"}))
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok and report.created == 2 and len(report.warnings) == 2
    assert any("game details" in w for w in report.warnings)
    assert len(api.paths(r"/games$")) == 1 and len(api.paths(r"/rosters$")) == 1  # asked once per run
    match = match_by_ps(session, FINISHED_ID)
    assert (match.team1_score, match.team2_score) == (1, 2) and count(session, MapPlayed) == 0
    assert match.extra["pandascore"]["games"][0]["winner_team_id"] == team_by_ps(session, PS_NAVI).id


# ------------------------------------------------------------------------------------- HLTV linking


def hltv_scheduled_match(session: Session) -> tuple[Match, Team, Team, Player]:
    """What the HLTV sync leaves behind for an upcoming match: Vitality listed first."""
    vitality = resolve_team(session, name="Vitality", source=DataSource.HLTV, external_id=9565)
    navi = resolve_team(session, name="Natus Vincere", source=DataSource.HLTV, external_id=4608)
    assert vitality is not None and navi is not None
    match = Match(
        team1_id=vitality.id,
        team2_id=navi.id,
        scheduled_at=KICKOFF + timedelta(minutes=30),  # HLTV's time differs a little
        status=MatchStatus.SCHEDULED,
        format=MatchFormat.BO3,
        hltv_id=2378001,
        is_lan=True,
        in_scope=True,
        extra={"hltv": {"status": "scheduled", "has_lineups": False}},
    )
    session.add(match)
    session.flush()
    zywoo = resolve_player(
        session, nickname="ZywOo", source=DataSource.HLTV, external_id=11893, team_id=vitality.id
    )
    assert zywoo is not None
    session.add(
        MatchLineup(match_id=match.id, team_id=vitality.id, player_id=zywoo.id, source=DataSource.HLTV)
    )
    session.flush()
    return match, vitality, navi, zywoo


def test_links_hltv_match_in_swapped_orientation(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    existing, vitality, navi, zywoo = hltv_scheduled_match(session)
    serve_finished(api)
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok, report.errors
    assert (report.created, report.updated) == (0, 1) and report.details["linked"] == 1
    assert count(session, Match) == 1 and count(session, Team) == 2

    match = session.get(Match, existing.id)
    assert match is not None and match.pandascore_id == FINISHED_ID and match.hltv_id == 2378001
    assert (match.team1_id, match.team2_id) == (vitality.id, navi.id)  # existing orientation is kept
    assert vitality.pandascore_id == PS_VITALITY and navi.pandascore_id == PS_NAVI
    assert vitality.hltv_id == 9565
    # PandaScore lists NAVI first with 1-2: in OUR orientation that is Vitality 2-1 NAVI
    assert (match.team1_score, match.team2_score, match.winner_team_id) == (2, 1, vitality.id)
    assert match.status is MatchStatus.FINISHED and match.scheduled_at == KICKOFF
    assert match.tier is Tier.S and match.stage == "Playoffs - Semifinal 1"

    maps = maps_of(session, match)
    mirage, inferno, anubis = maps[1], maps[2], maps[3]
    assert (mirage.team1_rounds, mirage.team2_rounds, mirage.winner_team_id) == (11, 13, navi.id)
    assert mirage.team1_start_side is Side.CT
    assert (mirage.team1_ct_rounds, mirage.team1_t_rounds, mirage.team2_ct_rounds, mirage.team2_t_rounds) == (
        5,
        6,
        6,
        7,
    )
    assert (inferno.team1_rounds, inferno.team2_rounds, inferno.winner_team_id) == (13, 7, vitality.id)
    assert inferno.team1_start_side is Side.T and (inferno.team1_t_rounds, inferno.team1_ct_rounds) == (8, 5)
    assert (anubis.team1_rounds, anubis.team2_rounds) == (19, 17)
    assert (anubis.team1_ct_rounds, anubis.team1_t_rounds) == (10, 9)
    records = pandascore_round_records(session, anubis)
    assert sum(r.winner is TeamSlot.TEAM1 for r in records) == 19 and records[0].team1_side is Side.CT

    # the HLTV lineup row is untouched, ZywOo is ONE player known to both sources, and Vitality (which
    # has HLTV rows) gets no PandaScore rows while NAVI (no rows yet) does
    lineups = lineups_of(session, match)
    hltv_rows = [lu for lu in lineups if lu.source is DataSource.HLTV]
    assert [(lu.player_id, lu.team_id) for lu in hltv_rows] == [(zywoo.id, vitality.id)]
    assert zywoo.pandascore_id == 17512 and zywoo.hltv_id == 11893
    assert sum(lu.team_id == navi.id and lu.source is DataSource.PANDASCORE for lu in lineups) == 5
    assert sum(lu.team_id == vitality.id for lu in lineups) == 1
    assert count(session, Player) == 10


def test_hltv_result_and_map_data_are_kept(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    existing, vitality, navi, _ = hltv_scheduled_match(session)
    aleksib = resolve_player(
        session, nickname="Aleksib", source=DataSource.HLTV, external_id=9816, team_id=navi.id
    )
    assert aleksib is not None
    session.add(
        MatchLineup(match_id=existing.id, team_id=navi.id, player_id=aleksib.id, source=DataSource.HLTV)
    )
    existing.status = MatchStatus.FINISHED
    existing.team1_score, existing.team2_score, existing.winner_team_id = 2, 1, vitality.id
    existing.extra = {"hltv": {"status": "finished", "has_lineups": True}}
    hltv_map = MapPlayed(
        match_id=existing.id,
        map_number=1,
        map_name="mirage",
        status=MatchStatus.FINISHED,
        team1_rounds=11,
        team2_rounds=13,
        winner_team_id=navi.id,
        hltv_mapstats_id=210001,
        picked_by_team_id=navi.id,
        extra={"hltv": {"raw_name": "Mirage"}},
    )
    session.add(hltv_map)
    session.flush()

    payload = finished_match()
    payload["results"] = [{"score": 2, "team_id": PS_NAVI}, {"score": 0, "team_id": PS_VITALITY}]  # wrong
    payload["winner_id"] = PS_NAVI
    games = finished_games()
    games[0]["map"].update(name="Nuke", slug="nuke")  # disagrees with HLTV
    api.past, api.games[FINISHED_ID] = [payload], games
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok and report.updated == 1
    assert (existing.team1_score, existing.team2_score, existing.winner_team_id) == (2, 1, vitality.id)
    assert any("differs from the HLTV result" in w for w in report.warnings)
    assert any("kept HLTV" in w and "'nuke'" in w for w in report.warnings)
    maps = maps_of(session, existing)
    assert maps[1].map_name == "mirage" and maps[1].hltv_mapstats_id == 210001
    assert maps[1].picked_by_team_id == navi.id and maps[1].team1_start_side is Side.CT  # only filled in
    assert maps[2].map_name == "inferno" and maps[3].map_name == "anubis"  # new rows from PandaScore
    # HLTV has lineup rows for both teams: PandaScore adds none and deletes none
    assert [lu.source for lu in lineups_of(session, existing)] == [DataSource.HLTV, DataSource.HLTV]
    assert aleksib.pandascore_id == 17548  # ... but the players are linked to their PandaScore ids


def test_both_sync_orders_converge_on_one_match(
    session_factory: sessionmaker[Session], ps_settings: PandaScoreSettings, api: FakePandaScore
) -> None:
    """HLTV first or PandaScore first: one match row with the same result either way."""
    from cs2bot.ingest.hltv import sync_match as hltv_sync_match
    from tests.ingest.hltv.conftest import FakeFetcher

    serve_finished(api)
    settings = Settings().model_copy(update={"pandascore": ps_settings})
    outcomes = []
    for order in ("hltv_first", "pandascore_first"):
        session = session_factory()
        try:
            client = PandaScoreClient(
                ps_settings, sleep=lambda _s: None, rate_limiter=NoWait(), now=lambda: NOW
            )

            def run_pandascore(s: Session = session, c: PandaScoreClient = client) -> None:
                report = sync_past(s, c, settings, SINCE, now=NOW)
                assert report.ok, report.errors

            def run_hltv(s: Session = session) -> None:
                report = hltv_sync_match(s, FakeFetcher(), 2378001, settings=settings)
                assert report.ok, report.errors

            steps = [run_hltv, run_pandascore] if order == "hltv_first" else [run_pandascore, run_hltv]
            for step in steps:
                step()
            run_pandascore()  # a later PandaScore run changes nothing more
            session.flush()
            assert count(session, Match) == 1 and count(session, Team) == 2
            match = session.scalars(select(Match)).one()
            assert match.pandascore_id == FINISHED_ID and match.hltv_id == 2378001
            vitality = team_by_ps(session, PS_VITALITY)
            assert vitality.hltv_id == 9565 and match.winner_team_id == vitality.id
            vitality_first = match.team1_id == vitality.id
            scores = (match.team1_score, match.team2_score)
            assert scores == ((2, 1) if vitality_first else (1, 2))
            assert (
                match.scheduled_at == KICKOFF
                and match.tier is Tier.S
                and match.status is MatchStatus.FINISHED
            )
            maps = maps_of(session, match)
            per_map = []
            for number in (1, 2, 3):
                row = maps[number]
                rounds = (row.team1_rounds, row.team2_rounds)
                ours = rounds if vitality_first else rounds[::-1]
                side = row.team1_start_side
                assert side is not None
                per_map.append(
                    (row.map_name, ours, side if vitality_first else side.other, row.overtime_rounds)
                )
                assert row.hltv_mapstats_id is not None
            lineups = lineups_of(session, match)
            assert len(lineups) == 10 and count(session, Player) == 10
            assert all(p.hltv_id and p.pandascore_id for p in session.scalars(select(Player)))
            outcomes.append(per_map)
            session.rollback()
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
        finally:
            session.close()
    expected = [
        ("mirage", (11, 13), Side.CT, 0),
        ("inferno", (13, 7), Side.T, 0),
        ("anubis", (19, 17), Side.CT, 12),
    ]
    assert outcomes == [expected, expected]


# ------------------------------------------------------------------------------------- entity resolution


def test_academy_teams_are_not_glued_to_the_main_team(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    mouz = resolve_team(session, name="MOUZ", source=DataSource.HLTV, external_id=4494)
    legion = resolve_team(session, name="GamerLegion", source=DataSource.HLTV, external_id=9928)
    assert mouz is not None and legion is not None
    api.upcoming = [make_match(8101, ps_team(81, "MOUZ NXT", "MOUZ"), ps_team(82, "Gamer Legion", "GL"))]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert report.ok and report.created == 1
    academy = team_by_ps(session, 81)
    assert academy.id != mouz.id and academy.name == "MOUZ NXT" and mouz.pandascore_id is None
    assert legion.pandascore_id == 82  # a pure spelling variant IS the same team
    assert count(session, Team) == 3
    # the main team shows up later under its own PandaScore id
    api.upcoming = [make_match(8102, ps_team(83, "MOUZ", "MOUZ"), ps_team(82, "Gamer Legion", "GL"))]
    sync_upcoming(session, client, app_settings, now=NOW)
    assert mouz.pandascore_id == 83 and count(session, Team) == 3


def test_stand_in_from_hltv_lineup_is_not_duplicated(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    existing, _vitality, navi, _ = hltv_scheduled_match(session)
    other = resolve_team(session, name="Falcons", source=DataSource.HLTV, external_id=11283)
    assert other is not None
    # HLTV listed jL as a NAVI stand-in for this match; his current team is another one
    stand_in = resolve_player(
        session, nickname="jL", source=DataSource.HLTV, external_id=19206, team_id=other.id
    )
    assert stand_in is not None
    session.add(
        MatchLineup(
            match_id=existing.id,
            team_id=navi.id,
            player_id=stand_in.id,
            is_standin=True,
            source=DataSource.HLTV,
        )
    )
    session.flush()
    serve_finished(api)
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok, report.errors
    assert stand_in.pandascore_id == 21543  # the HLTV row was recognised: one player, both ids
    assert stand_in.current_team_id == other.id  # a stand-in is not moved to the team he helped out
    assert len(list(session.scalars(select(Player).where(Player.nickname == "jL")))) == 1
    rows = [lu for lu in lineups_of(session, existing) if lu.player_id == stand_in.id]
    assert len(rows) == 1 and rows[0].source is DataSource.HLTV and rows[0].is_standin is True


def test_backfilled_old_match_does_not_move_players(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_finished(api)
    assert sync_past(session, client, app_settings, SINCE, now=NOW).ok
    ropz = session.scalar(select(Player).where(Player.pandascore_id == 18234))
    vitality = team_by_ps(session, PS_VITALITY)
    assert ropz is not None and ropz.current_team_id == vitality.id

    # an old FaZe match in which ropz played, processed AFTER the recent one (backfill order)
    old = finished_match()
    faze, heroic = ps_team(3212, "FaZe", "FaZe"), ps_team(3246, "Heroic", "HERO")
    old.update(
        id=990001,
        scheduled_at="2024-02-10T15:00:00Z",
        begin_at="2024-02-10T15:05:00Z",
        end_at="2024-02-10T17:00:00Z",
        opponents=[{"opponent": faze, "type": "Team"}, {"opponent": heroic, "type": "Team"}],
        results=[{"score": 2, "team_id": 3212}, {"score": 0, "team_id": 3246}],
        winner_id=3212,
        number_of_games=3,
    )
    old["tournament"].update(id=15001, slug="old-event", tier="a")
    old["games"] = old["games"][:2]
    games = finished_games()[:2]
    for game in games:
        game["match_id"] = 990001
        game["winner"] = {"id": 3212, "type": "Team"}
        game["rounds"] = []
        game["rounds_score"] = [{"score": 13, "team_id": 3212}, {"score": 8, "team_id": 3246}]
        for line in game["players"]:
            line["team"] = faze if line["team"]["id"] == PS_VITALITY else heroic
    api.past, api.games[990001] = [old], games
    report = sync_past(
        session,
        client,
        app_settings,
        datetime(2024, 2, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
        now=NOW,
    )
    assert report.ok and report.created == 1
    old_match = match_by_ps(session, 990001)
    faze_row = team_by_ps(session, 3212)
    assert any(lu.player_id == ropz.id and lu.team_id == faze_row.id for lu in lineups_of(session, old_match))
    assert ropz.current_team_id == vitality.id  # still on his current team
    assert count(session, Player) == 10  # the same ten people, no duplicates


# ------------------------------------------------------------------------------------- robustness


def test_malformed_matches_are_reported_and_the_rest_succeeds(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    garbage = make_match(4001, ps_team(21, "Gamma"), ps_team(22, "Delta"))
    garbage["opponents"] = "not-a-list"  # fails validation
    same_team = make_match(4002, ps_team(23, "Epsilon"), ps_team(23, "Epsilon"))  # fails while persisting
    bad_time = make_match(4003, ps_team(24, "Zeta"), ps_team(25, "Eta"), scheduled_at="yesterday-ish")
    good = make_match(4004, ps_team(26, "Theta"), ps_team(27, "Iota"), scheduled_at="2026-11-23T20:00:00Z")
    api.upcoming = [garbage, same_team, upcoming_match(), bad_time, good]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert (report.created, report.failed) == (2, 3) and not report.ok
    assert len(report.errors) == 3
    assert any("match 4001" in e and "malformed" in e for e in report.errors)
    assert any("match 4002" in e and "same team" in e for e in report.errors)
    assert any("match 4003" in e for e in report.errors)
    assert {m.pandascore_id for m in session.scalars(select(Match))} == {UPCOMING_ID, 4004}
    # nothing of the broken match is left behind (its savepoint was rolled back)
    assert session.scalar(select(Team).where(Team.pandascore_id == 23)) is None


def test_broken_game_details_do_not_lose_the_match(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_finished(api)
    games = finished_games()
    games[1]["map"]["name"] = "x" * 80  # normalised name is cut to the column size; still fine
    games[2]["rounds"] = [{"round": 1, "ct": 1, "terrorists": 2, "winner_team": 1}]  # foreign teams
    api.games[FINISHED_ID] = games
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok and report.created == 1
    maps = maps_of(session, match_by_ps(session, FINISHED_ID))
    assert len(maps) == 3 and len(maps[2].map_name) == 32
    assert maps[3].team1_start_side is None and (maps[3].team1_rounds, maps[3].team2_rounds) == (17, 19)
    assert pandascore_round_records(session, maps[3]) == []


def test_rate_limit_stops_the_run_and_keeps_progress(
    session: Session,
    client: PandaScoreClient,
    api: FakePandaScore,
    app_settings: Settings,
    sleeps: list[float],
) -> None:
    serve_finished(api)
    api.override(r"/games$", lambda request: httpx.Response(429, headers={"Retry-After": "1800"}))
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.blocked and not report.ok
    assert any("rate limit" in e and "1800" in e for e in report.errors)
    assert sleeps == []  # a half-hour Retry-After is not slept through
    match = match_by_ps(session, FINISHED_ID)  # the match itself was stored before the limit hit
    assert (match.team1_score, match.team2_score) == (1, 2) and count(session, MapPlayed) == 0
    # next run (limit lifted): the missing details are fetched
    api.overrides.clear()
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert report.ok and report.updated == 1 and count(session, MapPlayed) == 3


def test_missing_token_is_reported_not_raised(
    session: Session, api: FakePandaScore, settings: Settings
) -> None:
    # deep_data: PandaScore is optional - no token means "disabled" (a note, not an error)
    client = PandaScoreClient(settings.pandascore, sleep=lambda _s: None, rate_limiter=NoWait())
    report = sync_upcoming(session, client, settings, now=NOW)
    assert report.ok and not report.blocked and api.requests == []
    assert report.failed == 0 and report.errors == [] and report.details["disabled"] is True
    assert "no API token" in report.warnings[0]


def test_list_failure_midway_keeps_earlier_pages(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    teams = [ps_team(100 + n, f"Squad {n:02d}") for n in range(8)]
    api.past = [
        make_match(
            5000 + n,
            teams[2 * n],
            teams[2 * n + 1],
            status="finished",
            scores=(2, 1),
            scheduled_at=f"2026-11-21T1{n}:00:00Z",
        )
        for n in range(4)
    ]
    small = app_settings.model_copy(
        update={"pandascore": app_settings.pandascore.model_copy(update={"page_size": 2})}
    )
    paged = PandaScoreClient(small.pandascore, sleep=lambda _s: None, rate_limiter=NoWait(), max_attempts=2)

    def second_page_fails(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page[number]") == "2":
            return httpx.Response(500)
        return api._page(request, api.past)

    api.override(r"/csgo/matches/past$", second_page_fails)
    options = PandaScoreSyncOptions(game_details="never", rosters="never")
    report = sync_past(session, paged, small, SINCE, options=options, now=NOW)
    assert report.created == 2 and report.failed == 1 and not report.ok
    assert "HTTP 500" in report.errors[0] and count(session, Match) == 2


def test_commit_every_persists_progress(
    session_factory: sessionmaker[Session],
    client: PandaScoreClient,
    api: FakePandaScore,
    app_settings: Settings,
) -> None:
    teams = [ps_team(200 + n, f"Crew {n:02d}") for n in range(6)]
    api.past = [
        make_match(
            6000 + n,
            teams[2 * n],
            teams[2 * n + 1],
            status="finished",
            scores=(0, 2),
            scheduled_at=f"2026-11-21T1{n}:00:00Z",
        )
        for n in range(3)
    ]
    options = PandaScoreSyncOptions(commit_every=2, game_details="never", rosters="never")
    writer = session_factory()
    commits: list[int] = []
    real_commit = writer.commit

    def counting_commit() -> None:
        commits.append(count(writer, Match))
        real_commit()

    writer.commit = counting_commit  # type: ignore[method-assign]
    try:
        report = sync_past(writer, client, app_settings, SINCE, options=options, now=NOW)
        assert report.created == 3
        assert commits == [2]  # committed once, after the second match; the caller commits the rest
    finally:
        writer.close()


def test_long_ranges_are_listed_in_windows(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    teams = [ps_team(300 + n, f"Unit {n:02d}") for n in range(6)]
    days = ["2026-09-03T12:00:00Z", "2026-10-20T12:00:00Z", "2026-11-21T12:00:00Z"]
    api.past = [
        make_match(
            6100 + n, teams[2 * n], teams[2 * n + 1], status="finished", scores=(2, 0), scheduled_at=day
        )
        for n, day in enumerate(days)
    ]
    options = PandaScoreSyncOptions(game_details="never", rosters="never")
    since = datetime(2026, 9, 1, tzinfo=UTC)
    report = sync_past(session, client, app_settings, since, options=options, now=NOW)
    assert report.ok and report.created == 3
    assert [p["range[begin_at]"] for p in api.params("/csgo/matches/past")] == [
        "2026-09-01T00:00:00Z,2026-10-01T00:00:00Z",
        "2026-10-01T00:00:00Z,2026-10-31T00:00:00Z",
        "2026-10-31T00:00:00Z,2026-11-22T12:00:00Z",
    ]
    assert report.details["completed_until"] == NOW.isoformat() and report.fetched_pages == 3

    # a window that cannot be listed ends the run; what was completed is reported for the re-run
    calls = {"n": 0}

    def second_window_fails(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return (
            httpx.Response(400, json={"error": "Bad Request"})
            if calls["n"] == 2
            else api._page(request, api.past)
        )

    api.override(r"/csgo/matches/past$", second_window_fails)
    report = sync_past(session, client, app_settings, since, options=options, now=NOW)
    assert not report.ok and calls["n"] == 2
    assert report.details["completed_until"] == "2026-10-01T00:00:00+00:00"

    single = PandaScoreSyncOptions(game_details="never", rosters="never", past_window_days=None)
    api.overrides.clear()
    api.requests.clear()
    assert sync_past(session, client, app_settings, since, options=single, now=NOW).ok
    assert len(api.requests) == 1
    assert sync_past(session, client, app_settings, NOW, since, options=single, now=NOW).fetched_pages == 0


# ------------------------------------------------------------------------------------- scope


def test_scope_follows_tier_and_hltv_rank(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    ranked = resolve_team(session, name="Kappa", source=DataSource.HLTV, external_id=7001)
    assert ranked is not None
    ranked.hltv_rank = 31
    low_tier = make_match(7101, ps_team(31, "Kappa"), ps_team(32, "Lambda"), tier="c")
    nobody = make_match(7102, ps_team(33, "Mu"), ps_team(34, "Nu"), tier="d", tournament_id=17400)
    unrated = make_match(7103, ps_team(35, "Xi"), ps_team(36, "Omicron"), tier=None, tournament_id=17401)
    b_tier = make_match(7104, ps_team(37, "Pi"), ps_team(38, "Rho"), tier="b", tournament_id=17402)
    api.upcoming = [low_tier, nobody, unrated, b_tier]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert report.created == 4
    scope = {m.pandascore_id: (m.in_scope, m.tier) for m in session.scalars(select(Match))}
    assert scope == {
        7101: (True, Tier.C),  # a top-50 team (the HLTV row was linked by name)
        7102: (False, Tier.D),
        7103: (False, Tier.UNRANKED),
        7104: (True, Tier.B),
    }
    assert ranked.pandascore_id == 31 and count(session, MatchLineup) == 0
    # rosters are only requested for in-scope matches
    assert sorted(api.paths("rosters")) == ["/tournaments/17260/rosters", "/tournaments/17402/rosters"]

    # a team enters the top 50 later: the next sync promotes its match
    team_by_ps(session, 34).hltv_rank = 50
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert report.updated == 1 and match_by_ps(session, 7102).in_scope is True


def test_out_of_scope_matches_can_be_left_out(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    api.upcoming = [
        make_match(7201, ps_team(41, "Sigma"), ps_team(42, "Tau"), tier="d"),
        make_match(7202, ps_team(43, "Upsilon"), ps_team(44, "Phi"), tier="a", tournament_id=17500),
    ]
    options = PandaScoreSyncOptions(store_out_of_scope=False)
    report = sync_upcoming(session, client, app_settings, options=options, now=NOW)
    assert (report.created, report.skipped) == (1, 1) and report.details["out_of_scope"] == 1
    assert [m.pandascore_id for m in session.scalars(select(Match))] == [7202]
    assert count(session, Team) == 2


def test_historic_matches_use_the_rank_of_their_day(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    star = resolve_team(session, name="Chi", source=DataSource.HLTV, external_id=7301)
    assert star is not None
    star.hltv_rank = 5  # today
    session.add(TeamRanking(team_id=star.id, source=DataSource.HLTV, rank=140, ranked_on=date(2024, 3, 4)))
    session.flush()
    old = make_match(
        7301,
        ps_team(51, "Chi"),
        ps_team(52, "Psi"),
        tier="c",
        status="finished",
        scores=(2, 0),
        scheduled_at="2024-03-10T15:00:00Z",
    )
    api.past = [old]
    since = datetime(2024, 3, 1, tzinfo=UTC)
    report = sync_past(session, client, app_settings, since, datetime(2024, 4, 1, tzinfo=UTC), now=NOW)
    assert report.created == 1 and match_by_ps(session, 7301).in_scope is False


def test_cs_go_matches_are_skipped(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    legacy = make_match(7401, ps_team(61, "Omega"), ps_team(62, "Digamma"))
    legacy["videogame_title"] = {"id": 12, "name": "Counter-Strike: Global Offensive", "slug": "cs-go"}
    api.matches[7401] = legacy
    report = sync_match(session, client, app_settings, 7401, now=NOW)
    assert report.skipped == 1 and report.details["other_title"] == 1 and count(session, Match) == 0


# ------------------------------------------------------------------------------ single match / reconcile


def test_sync_match_fetches_details_whatever_the_scope(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    payload = finished_match()
    payload["tournament"]["tier"] = "d"
    api.matches[FINISHED_ID] = payload
    api.games[FINISHED_ID] = finished_games()
    report = sync_match(session, client, app_settings, FINISHED_ID, now=NOW)
    assert report.ok and report.created == 1 and report.job == "pandascore.sync_match"
    match = match_by_ps(session, FINISHED_ID)
    assert match.in_scope is False and len(maps_of(session, match)) == 3
    assert len(lineups_of(session, match)) == 10

    missing = sync_match(session, client, app_settings, 999, now=NOW)
    assert missing.ok and missing.skipped == 1 and "not found" in missing.warnings[0]


def test_vanished_open_match_is_reconciled(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_upcoming(api)
    other = make_match(8001, ps_team(71, "Sampi"), ps_team(72, "Koppa"), tournament_id=17600)
    api.upcoming.append(other)
    sync_upcoming(session, client, app_settings, now=NOW)

    canceled = copy.deepcopy(upcoming_match())
    canceled.update(status="canceled", modified_at="2026-11-22T14:00:00Z")
    api.upcoming = []  # both vanished from the list: one was cancelled, one deleted altogether
    api.matches[UPCOMING_ID] = canceled
    report = sync_upcoming(session, client, app_settings, now=NOW + timedelta(hours=3))
    assert report.details["reconciled"] == 2 and report.updated == 2
    assert match_by_ps(session, UPCOMING_ID).status is MatchStatus.CANCELED
    deleted = match_by_ps(session, 8001)
    assert deleted.status is MatchStatus.CANCELED and deleted.extra["pandascore"]["deleted"] is True
    assert any("no longer exists" in w for w in report.warnings)
    # cancelled matches are closed: nothing left to reconcile on the next run
    api.requests.clear()
    report = sync_upcoming(session, client, app_settings, now=NOW + timedelta(hours=4))
    assert (report.updated, report.details.get("reconciled", 0)) == (0, 0)
    assert api.paths() == ["/csgo/matches/upcoming", "/csgo/matches/running"]


def test_sync_modified_catches_reschedules(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    serve_upcoming(api)
    sync_upcoming(session, client, app_settings, now=NOW)
    postponed = upcoming_match()
    postponed.update(status="postponed", modified_at="2026-11-22T12:30:00Z")
    api.upcoming = [postponed]
    report = sync_modified(session, client, app_settings, NOW, now=NOW + timedelta(hours=1))
    assert report.updated == 1 and match_by_ps(session, UPCOMING_ID).status is MatchStatus.POSTPONED
    params = api.params(r"/csgo/matches$")[0]
    assert params["range[modified_at]"] == "2026-11-22T12:00:00Z,2026-11-22T13:00:00Z"
