from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from cs2bot.config import Settings
from cs2bot.db import state
from cs2bot.db.models import BookmakerEvent, TeamAlias
from cs2bot.domain.enums import Bookmaker, DataSource, LinkStatus, MatchStatus
from cs2bot.odds.event_matching import competition_score, link_event, team_name_score, time_score
from tests.odds.odds_testkit import make_match, make_team

T0 = datetime(2026, 11, 23, 17, 0, tzinfo=UTC)
NOW = T0 - timedelta(hours=6)


def make_event(
    session,
    team1: str,
    team2: str,
    starts_at: datetime | None = T0,
    *,
    external_id: str = "1001",
    competition: str | None = None,
    status: LinkStatus = LinkStatus.UNLINKED,
    match_id: int | None = None,
    swapped: bool = False,
) -> BookmakerEvent:
    row = BookmakerEvent(
        bookmaker=Bookmaker.BETCLIC,
        external_id=external_id,
        name=f"{team1} - {team2}",
        team1_raw=team1,
        team2_raw=team2,
        competition_raw=competition,
        starts_at=starts_at,
        first_seen_at=NOW,
        last_seen_at=NOW,
        link_status=status,
        match_id=match_id,
        teams_swapped=swapped,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def teams(session):
    return {
        "navi": make_team(session, "Natus Vincere"),
        "faze": make_team(session, "FaZe Clan"),
        "ef": make_team(session, "Eternal Fire", aliases=("EF",)),
        "heroic": make_team(session, "HEROIC"),
        "spirit": make_team(session, "Team Spirit"),
        "spirit_ac": make_team(session, "Spirit Academy"),
    }


def test_exact_link_is_auto_and_stores_betclic_aliases(session, settings: Settings, teams) -> None:
    match = make_match(session, teams["navi"], teams["faze"], T0, tournament="IEM Chengdu 2026")
    ev = make_event(session, "Natus Vincere", "FaZe Clan", competition="Counter-Strike 2 - IEM Chengdu 2026")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert result.status is LinkStatus.AUTO and ev.link_status is LinkStatus.AUTO
    assert ev.match_id == match.id and ev.teams_swapped is False
    assert result.confidence is not None and result.confidence >= 0.99
    assert result.changed is True and result.status_changed is True
    aliases = {
        (a.alias, a.team_id)
        for a in session.scalars(select(TeamAlias).where(TeamAlias.source == DataSource.BETCLIC))
    }
    assert aliases == {("natus vincere", teams["navi"].id), ("faze", teams["faze"].id)}


def test_alias_link(session, settings: Settings, teams) -> None:
    match = make_match(session, teams["ef"], teams["heroic"], T0 + timedelta(minutes=10))
    ev = make_event(session, "EF", "HEROIC Esports")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert result.status is LinkStatus.AUTO
    assert ev.match_id == match.id and ev.teams_swapped is False


def test_swapped_orientation_detected(session, settings: Settings, teams) -> None:
    match = make_match(session, teams["faze"], teams["navi"], T0)
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert result.status is LinkStatus.AUTO
    assert ev.match_id == match.id and ev.teams_swapped is True
    navi_alias = session.scalar(
        select(TeamAlias).where(TeamAlias.alias == "natus vincere", TeamAlias.source == DataSource.BETCLIC)
    )
    assert navi_alias is not None and navi_alias.team_id == teams["navi"].id  # bookmaker team1 -> our team2


def test_ambiguous_duplicate_matches_go_to_review(session, settings: Settings, teams) -> None:
    make_match(session, teams["navi"], teams["faze"], T0)
    make_match(session, teams["navi"], teams["faze"], T0)
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert result.status is LinkStatus.PENDING_REVIEW
    assert ev.match_id is not None and len(result.candidates) == 2
    assert "ambiguous" in result.reason
    assert session.scalar(select(TeamAlias).where(TeamAlias.source == DataSource.BETCLIC)) is None


def test_time_distance_breaks_ties_between_same_teams(session, settings: Settings, teams) -> None:
    near = make_match(session, teams["navi"], teams["faze"], T0 + timedelta(minutes=15))
    make_match(session, teams["navi"], teams["faze"], T0 + timedelta(hours=9))
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert result.status is LinkStatus.AUTO and ev.match_id == near.id


def test_weak_team_match_is_pending_review(session, settings: Settings, teams) -> None:
    make_match(session, teams["navi"], teams["heroic"], T0)  # only one team matches
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert result.status is LinkStatus.PENDING_REVIEW
    assert result.confidence is not None and result.confidence < 0.9


def test_academy_team_is_not_linked_to_main_team(session, settings: Settings, teams) -> None:
    main = make_match(session, teams["spirit"], teams["faze"], T0)
    make_match(session, teams["spirit_ac"], teams["faze"], T0)
    ev = make_event(session, "Spirit", "FaZe Clan")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert ev.match_id == main.id and result.status is LinkStatus.AUTO


def test_manual_decisions_are_never_overridden(session, settings: Settings, teams) -> None:
    good = make_match(session, teams["navi"], teams["faze"], T0)
    other = make_match(session, teams["ef"], teams["heroic"], T0)
    confirmed = make_event(
        session,
        "Natus Vincere",
        "FaZe Clan",
        status=LinkStatus.CONFIRMED,
        match_id=other.id,
        swapped=True,
        external_id="a",
    )
    rejected = make_event(
        session, "Natus Vincere", "FaZe Clan", status=LinkStatus.REJECTED, match_id=good.id, external_id="b"
    )
    r1 = link_event(session, confirmed, settings=settings, now=NOW)
    r2 = link_event(session, rejected, settings=settings, now=NOW)
    assert (confirmed.link_status, confirmed.match_id, confirmed.teams_swapped) == (
        LinkStatus.CONFIRMED,
        other.id,
        True,
    )
    assert (rejected.link_status, rejected.match_id) == (LinkStatus.REJECTED, good.id)
    assert r1.changed is False and r2.changed is False and "manual" in r1.reason


def test_no_candidate_unlinks(session, settings: Settings, teams) -> None:
    match = make_match(session, teams["navi"], teams["faze"], T0, status=MatchStatus.CANCELED)
    ev = make_event(
        session, "Natus Vincere", "FaZe Clan", status=LinkStatus.AUTO, match_id=match.id, swapped=True
    )
    result = link_event(session, ev, settings=settings, now=NOW)
    assert result.status is LinkStatus.UNLINKED
    assert ev.match_id is None and ev.teams_swapped is False and ev.link_confidence is None
    assert result.changed is True


def test_outside_window_is_ignored(session, settings: Settings, teams) -> None:
    make_match(session, teams["navi"], teams["faze"], T0 + timedelta(hours=13))
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    assert link_event(session, ev, settings=settings, now=NOW).status is LinkStatus.UNLINKED


def test_missing_start_time_uses_local_day_and_needs_review(session, settings: Settings, teams) -> None:
    today = make_match(session, teams["navi"], teams["faze"], datetime(2026, 11, 22, 20, tzinfo=UTC))
    make_match(session, teams["navi"], teams["faze"], datetime(2026, 11, 24, 20, tzinfo=UTC))  # another day
    ev = make_event(session, "Natus Vincere", "FaZe Clan", starts_at=None)
    result = link_event(session, ev, settings=settings, now=datetime(2026, 11, 22, 9, tzinfo=UTC))
    assert ev.match_id == today.id
    assert result.status is LinkStatus.PENDING_REVIEW  # no time information -> never AUTO


def test_in_scope_preferred_on_ties(session, settings: Settings, teams) -> None:
    make_match(session, teams["navi"], teams["faze"], T0, in_scope=False)
    scoped = make_match(session, teams["navi"], teams["faze"], T0, in_scope=True)
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    result = link_event(session, ev, settings=settings, now=NOW)
    assert ev.match_id == scoped.id and result.status is LinkStatus.PENDING_REVIEW


def test_dashboard_override_of_link_threshold(session, settings: Settings, teams) -> None:
    make_match(session, teams["navi"], teams["faze"], T0 + timedelta(hours=2))
    state.set_config_overrides(session, {"decision": {"min_event_link_confidence": 0.99}}, actor="test")
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    assert link_event(session, ev, settings=settings, now=NOW).status is LinkStatus.PENDING_REVIEW


def test_relink_moves_to_better_candidate(session, settings: Settings, teams) -> None:
    first = make_match(session, teams["navi"], teams["faze"], T0 + timedelta(hours=5))
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    link_event(session, ev, settings=settings, now=NOW)
    assert ev.match_id == first.id
    first.status = MatchStatus.POSTPONED
    second = make_match(session, teams["faze"], teams["navi"], T0)
    result = link_event(session, ev, settings=settings, now=NOW)
    assert ev.match_id == second.id and ev.teams_swapped is True and result.changed is True


def test_existing_betclic_alias_for_other_team_is_kept(session, settings: Settings, teams) -> None:
    session.add(TeamAlias(team_id=teams["heroic"].id, alias="faze", source=DataSource.BETCLIC))
    session.flush()
    make_match(session, teams["navi"], teams["faze"], T0)
    ev = make_event(session, "Natus Vincere", "FaZe Clan")
    link_event(session, ev, settings=settings, now=NOW)
    alias = session.scalar(
        select(TeamAlias).where(TeamAlias.alias == "faze", TeamAlias.source == DataSource.BETCLIC)
    )
    assert alias is not None and alias.team_id == teams["heroic"].id


def test_scoring_helpers() -> None:
    assert team_name_score("NAVI", ["Natus Vincere"]) == 1.0
    assert team_name_score("Spirit", ["Spirit Academy"]) < 0.8
    assert team_name_score("", ["x"]) == 0.0
    assert time_score(timedelta(minutes=20)) == 1.0
    assert time_score(timedelta(hours=6)) == pytest.approx(0.5)
    assert time_score(timedelta(hours=-13)) == 0.0
    assert time_score(None) == 0.4
    assert competition_score("Counter-Strike 2 - IEM Chengdu", "IEM Chengdu 2026 Playoffs") == 1.0
    assert competition_score(None, "x") is None
