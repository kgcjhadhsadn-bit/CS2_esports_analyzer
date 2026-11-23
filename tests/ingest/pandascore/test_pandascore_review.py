"""Regression tests added by the round-1 review (adversarial payloads, cross-source status merge)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import Match, Team, TeamRanking
from cs2bot.db.repo import resolve_team
from cs2bot.domain.enums import DataSource, MatchFormat, MatchStatus
from cs2bot.ingest.pandascore import (
    PandaScoreClient,
    PandaScoreSyncOptions,
    PSMatch,
    format_timestamp,
    sync_past,
    sync_upcoming,
)
from tests.ingest.pandascore.conftest import (
    BASE_URL,
    NOW,
    UPCOMING_ID,
    FakePandaScore,
    make_match,
    ps_team,
    upcoming_match,
)


@pytest.mark.parametrize("value", [1e20, -1e18, 1e300, float("inf"), True])
def test_absurd_timestamps_are_validation_errors(value: Any) -> None:
    # OverflowError / OSError from datetime.fromtimestamp must not escape pydantic's error handling
    with pytest.raises(ValidationError):
        PSMatch.model_validate({"id": 1, "end_at": value})


def test_absurd_timestamp_and_unhashable_id_do_not_abort_the_sync(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    overflow = make_match(4101, ps_team(31, "Alpha"), ps_team(32, "Beta"))
    overflow["end_at"] = 1e20  # out of range for datetime.fromtimestamp
    list_id = make_match(4102, ps_team(33, "Gamma"), ps_team(34, "Delta"))
    list_id["id"] = [4102]  # unhashable: used to crash the pagination de-duplication
    good = make_match(4103, ps_team(35, "Epsilon"), ps_team(36, "Zeta"), scheduled_at="2026-11-23T20:00:00Z")
    api.upcoming = [overflow, list_id, upcoming_match(), good]
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert report.created == 2 and report.failed == 2 and len(report.errors) == 2
    assert all("malformed payload" in e for e in report.errors)
    assert {m.pandascore_id for m in session.scalars(select(Match))} == {UPCOMING_ID, 4103}


def test_pagination_stops_when_the_server_ignores_page_numbers(
    client: PandaScoreClient, api: FakePandaScore
) -> None:
    def same_page(request: httpx.Request) -> httpx.Response:
        url = f"{BASE_URL}{request.url.path}"
        return httpx.Response(200, json=[upcoming_match()], headers={"Link": f'<{url}?page=2>; rel="next"'})

    api.override(r"/csgo/matches/upcoming", same_page)
    items = list(client.paginate("csgo/matches/upcoming"))
    assert [item["id"] for item in items] == [UPCOMING_ID]
    assert len(api.paths("upcoming")) == 2  # page 2 repeated page 1: stop instead of looping forever


def test_unhashable_ids_are_passed_on_not_fatal(client: PandaScoreClient, api: FakePandaScore) -> None:
    odd = upcoming_match()
    odd["id"] = {"nested": 1}
    api.override(r"/csgo/matches/upcoming", httpx.Response(200, json=[odd, upcoming_match()]))
    assert [type(item["id"]) for item in client.paginate("csgo/matches/upcoming")] == [dict, int]


def _hltv_deleted_match(session: Session) -> Match:
    """A match HLTV marked "Match deleted" (CANCELED) that PandaScore still lists as not started."""
    spirit = resolve_team(session, name="Team Spirit", source=DataSource.HLTV, external_id=7020)
    g2 = resolve_team(session, name="G2", source=DataSource.HLTV, external_id=5995)
    assert spirit is not None and g2 is not None
    payload = upcoming_match()
    match = Match(
        team1_id=g2.id,
        team2_id=spirit.id,
        scheduled_at=PSMatch.model_validate(payload).kickoff,
        status=MatchStatus.CANCELED,
        format=MatchFormat.BO3,
        hltv_id=2379999,
        in_scope=True,
        extra={"hltv": {"status": "deleted", "forfeit": False}},
    )
    session.add(match)
    session.flush()
    return match


def test_hltv_deleted_match_is_not_reopened_by_pandascore(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    existing = _hltv_deleted_match(session)
    api.upcoming = [upcoming_match()]
    for _ in range(2):  # the HLTV sync keeps CANCELED too: no flip-flop between the sources
        report = sync_upcoming(session, client, app_settings, now=NOW)
        assert not report.errors, report.errors
        match = session.get(Match, existing.id)
        assert match is not None and match.pandascore_id == UPCOMING_ID
        assert match.status is MatchStatus.CANCELED


def test_pandascore_finished_still_overrides_an_hltv_deletion(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    existing = _hltv_deleted_match(session)
    finished = upcoming_match()
    finished["status"] = "finished"
    finished["begin_at"] = format_timestamp(NOW - timedelta(hours=3))
    api.running = [finished]  # served by the running list just to get it into this run
    report = sync_upcoming(session, client, app_settings, now=NOW)
    assert not report.errors, report.errors
    match = session.get(Match, existing.id)
    assert match is not None and match.status is MatchStatus.FINISHED


def test_store_out_of_scope_false_uses_the_rank_as_of_the_match(
    session: Session, client: PandaScoreClient, api: FakePandaScore, app_settings: Settings
) -> None:
    star = Team(name="Omega", pandascore_id=61, hltv_rank=5)  # top 5 today ...
    session.add(star)
    session.flush()
    session.add(TeamRanking(team_id=star.id, source=DataSource.HLTV, rank=140, ranked_on=date(2024, 3, 4)))
    session.flush()  # ... but rank 140 in March 2024
    old = make_match(
        7401,
        ps_team(61, "Omega"),
        ps_team(62, "Kappa"),
        tier="c",
        status="finished",
        scores=(2, 0),
        scheduled_at="2024-03-10T15:00:00Z",
    )
    upcoming = make_match(7402, ps_team(61, "Omega"), ps_team(63, "Lambda"), tier="c")
    api.past = [old]
    api.upcoming = [upcoming]
    options = PandaScoreSyncOptions(store_out_of_scope=False, game_details="never", rosters="never")
    past = sync_past(
        session,
        client,
        app_settings,
        datetime(2024, 3, 1, tzinfo=UTC),
        datetime(2024, 3, 31, tzinfo=UTC),
        options=options,
        now=NOW,
    )
    assert past.created == 0 and past.details["out_of_scope"] == 1
    upcoming_report = sync_upcoming(session, client, app_settings, options=options, now=NOW)
    assert upcoming_report.created == 1
    assert [m.pandascore_id for m in session.scalars(select(Match))] == [7402]
