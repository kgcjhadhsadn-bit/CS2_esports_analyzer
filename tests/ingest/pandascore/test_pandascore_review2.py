"""Regression tests added by the second pass of review round 1 (see docs/dev/reports/pandascore.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import MapPlayed, Match, Team
from cs2bot.db.repo import resolve_team
from cs2bot.domain.enums import DataSource, MatchFormat, MatchStatus
from cs2bot.ingest.pandascore import (
    PandaScoreClient,
    PandaScorePlanError,
    PandaScoreSyncOptions,
    PSMatch,
    sync_match,
    sync_past,
    sync_upcoming,
)
from tests.ingest.pandascore.conftest import (
    BASE_URL,
    FINISHED_ID,
    NOW,
    TOKEN,
    UPCOMING_ID,
    FakePandaScore,
    finished_games,
    finished_match,
    make_match,
    ps_team,
    upcoming_match,
)

SINCE = datetime(2026, 11, 20, tzinfo=UTC)


def _maps(session: Session) -> list[MapPlayed]:
    return list(session.scalars(select(MapPlayed).order_by(MapPlayed.map_number)))


# ------------------------------------------------------------------------------------- game details


def test_empty_games_list_right_after_the_match_is_fetched_again(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    # PandaScore has not filled in the games yet: that must not be remembered as "final" (the match's
    # modified_at does not necessarily change when the games arrive)
    api.past = [finished_match()]
    api.games[FINISHED_ID] = []
    first = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert first.ok, first.errors
    assert _maps(session) == []
    api.games[FINISHED_ID] = finished_games()
    second = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert second.ok and second.updated == 1
    assert [m.map_name for m in _maps(session)] == ["mirage", "inferno", "anubis"]
    third = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert third.details.get("game_calls", 0) == 0  # now final: not fetched again


def test_partial_games_list_is_not_final(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    api.past = [finished_match()]
    api.games[FINISHED_ID] = finished_games()[:2]  # the third map is still missing
    sync_past(session, client, app_settings, SINCE, now=NOW)
    assert len(_maps(session)) == 2
    api.games[FINISHED_ID] = finished_games()
    sync_past(session, client, app_settings, SINCE, now=NOW)
    assert len(_maps(session)) == 3


def test_empty_games_list_becomes_final_once_the_match_is_old(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    api.past = [finished_match()]
    api.games[FINISHED_ID] = []
    later = NOW + timedelta(days=5)
    sync_past(session, client, app_settings, SINCE, NOW, now=later)
    report = sync_past(session, client, app_settings, SINCE, NOW, now=later)
    assert report.details.get("game_calls", 0) == 0


def test_absurd_map_positions_are_ignored(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    games = finished_games()
    games[0]["position"] = 0
    games[1]["position"] = 99
    api.past = [finished_match()]
    api.games[FINISHED_ID] = games
    report = sync_past(session, client, app_settings, SINCE, now=NOW)
    assert not report.errors, report.errors
    assert [m.map_number for m in _maps(session)] == [3]


# ------------------------------------------------------------------------------------- clock / ids


def test_naive_now_is_taken_as_utc(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    api.past = [finished_match()]
    report = sync_past(session, client, app_settings, SINCE, now=NOW.replace(tzinfo=None))
    assert report.ok, report.errors
    match = session.scalar(select(Match))
    assert match is not None and match.last_synced_at == NOW


@pytest.mark.parametrize("bad", ["../teams/1", "1/games", "a?b", "", "..", True, -3])
def test_ids_that_are_not_safe_url_path_segments_are_refused(
    client: PandaScoreClient, api: FakePandaScore, bad: object
) -> None:
    with pytest.raises(ValueError):
        client.match(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        client.team(bad)  # type: ignore[arg-type]
    assert api.requests == []


def test_sync_match_reports_an_invalid_id(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    report = sync_match(session, client, app_settings, "../x")  # type: ignore[arg-type]
    assert report.failed == 1 and report.errors and api.requests == []


def test_single_match_fallback_ignores_a_server_that_ignores_the_filter(
    client: PandaScoreClient, api: FakePandaScore
) -> None:
    api.override(r"/csgo/matches/\d+$", httpx.Response(403, json={"error": "Forbidden"}))
    api.override(r"/csgo/matches$", httpx.Response(200, json=[upcoming_match()]))  # wrong match
    from cs2bot.ingest.pandascore import PandaScoreNotFoundError

    with pytest.raises(PandaScoreNotFoundError):
        client.match(42)


# ------------------------------------------------------------------------------------- client hardening


def test_pagination_stops_on_a_page_without_any_object(client: PandaScoreClient, api: FakePandaScore) -> None:
    calls: list[int] = []

    def garbage(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) > 20:  # safety net for the test itself
            return httpx.Response(200, json=[])
        url = f"{BASE_URL}{request.url.path}"
        return httpx.Response(200, json=[1, "x"], headers={"Link": f'<{url}?page=2>; rel="next"'})

    api.override(r"/csgo/matches/upcoming", garbage)
    assert list(client.paginate("csgo/matches/upcoming")) == []
    assert len(calls) == 1 and client.stats.invalid_items == 2


def test_a_token_echoed_by_the_server_is_masked(client: PandaScoreClient, api: FakePandaScore) -> None:
    api.override(r"/teams/", httpx.Response(403, json={"error": "Forbidden", "message": f"token {TOKEN}"}))
    with pytest.raises(PandaScorePlanError) as info:
        client.team(1)
    assert TOKEN not in str(info.value) and "***" in str(info.value)
    api.override(r"/tournaments/", httpx.Response(418, json={"error": TOKEN}))
    with pytest.raises(Exception) as other:
        client.tournament(1)
    assert TOKEN not in str(other.value)


# ------------------------------------------------------------------------------------- status / result


def _hltv_live_match(session: Session) -> Match:
    spirit = resolve_team(session, name="Team Spirit", source=DataSource.HLTV, external_id=7020)
    g2 = resolve_team(session, name="G2", source=DataSource.HLTV, external_id=5995)
    assert spirit is not None and g2 is not None
    match = Match(
        team1_id=g2.id,
        team2_id=spirit.id,
        scheduled_at=PSMatch.model_validate(upcoming_match()).kickoff,
        status=MatchStatus.LIVE,
        format=MatchFormat.BO3,
        hltv_id=2379998,
        in_scope=True,
        extra={"hltv": {"status": "live"}},
    )
    session.add(match)
    session.flush()
    return match


def test_lagging_not_started_does_not_reopen_a_match_hltv_sees_live(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    # SCHEDULED is bettable for the decision engine: a live match must not flip back to it
    existing = _hltv_live_match(session)
    api.upcoming = [upcoming_match()]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert not report.errors, report.errors
    match = session.get(Match, existing.id)
    assert match is not None and match.pandascore_id == UPCOMING_ID and match.status is MatchStatus.LIVE


def test_pandascore_only_match_follows_pandascore_back_to_scheduled(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    running = upcoming_match()
    running["status"] = "running"
    api.running = [running]
    sync_upcoming(session, client, app_settings, now=NOW)
    api.running = []
    api.upcoming = [upcoming_match()]  # PandaScore corrected itself
    sync_upcoming(session, client, app_settings, now=NOW)
    match = session.scalar(select(Match).where(Match.pandascore_id == UPCOMING_ID))
    assert match is not None and match.status is MatchStatus.SCHEDULED


def test_result_withdrawn_by_a_cancellation_is_cleared(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    done = make_match(
        5101,
        ps_team(51, "Alpha Five"),
        ps_team(52, "Bravo Six"),
        status="finished",
        scheduled_at="2026-11-21T10:00:00Z",
        scores=(2, 0),
    )
    api.past = [done]
    options = PandaScoreSyncOptions(game_details="never", rosters="never")
    sync_past(session, client, app_settings, SINCE, now=NOW, options=options)
    match = session.scalar(select(Match).where(Match.pandascore_id == 5101))
    assert match is not None and match.winner_team_id is not None
    cancelled = {**done, "status": "canceled", "winner": None, "winner_id": None}
    api.past = [cancelled]
    report = sync_past(session, client, app_settings, SINCE, now=NOW, options=options)
    assert report.updated == 1
    session.refresh(match)
    assert match.status is MatchStatus.CANCELED
    assert (match.team1_score, match.team2_score, match.winner_team_id) == (None, None, None)


# ------------------------------------------------------------------------------------- scope pre-check


def test_store_out_of_scope_false_sees_hltv_only_top_teams(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    # HLTV created the team (with its rank) but PandaScore has not linked it yet
    hltv_team = resolve_team(session, name="Omega Gaming", source=DataSource.HLTV, external_id=9001)
    assert hltv_team is not None
    hltv_team.hltv_rank = 7
    session.flush()
    payload = make_match(6101, ps_team(71, "Omega Gaming"), ps_team(72, "Kappa"), tier="c")
    api.upcoming = [payload]
    options = PandaScoreSyncOptions(store_out_of_scope=False, game_details="never", rosters="never")
    report = sync_upcoming(session, client, app_settings, options=options, now=NOW)
    assert report.created == 1, report.details
    match = session.scalar(select(Match).where(Match.pandascore_id == 6101))
    assert match is not None and match.in_scope
    team = session.get(Team, hltv_team.id)
    assert team is not None and team.pandascore_id == 71


def test_store_out_of_scope_false_links_an_in_scope_hltv_row(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    alpha = resolve_team(session, name="Alpha Squad", source=DataSource.HLTV, external_id=9101)
    beta = resolve_team(session, name="Beta Squad", source=DataSource.HLTV, external_id=9102)
    assert alpha is not None and beta is not None
    payload = make_match(6201, ps_team(81, "Alpha Squad"), ps_team(82, "Beta Squad"), tier="d")
    hltv_row = Match(
        team1_id=beta.id,
        team2_id=alpha.id,
        scheduled_at=PSMatch.model_validate(payload).kickoff,
        status=MatchStatus.SCHEDULED,
        hltv_id=2370001,
        in_scope=True,  # HLTV decided (e.g. a star player): PandaScore must not ignore the row
        extra={"hltv": {"status": "scheduled"}},
    )
    session.add(hltv_row)
    session.flush()
    api.upcoming = [payload]
    options = PandaScoreSyncOptions(store_out_of_scope=False, game_details="never", rosters="never")
    report = sync_upcoming(session, client, app_settings, options=options, now=NOW)
    assert report.details.get("linked") == 1 and "out_of_scope" not in report.details
    assert hltv_row.pandascore_id == 6201
