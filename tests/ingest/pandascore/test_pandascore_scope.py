"""Scope rule (PLAN.md 4.1): PandaScore tier S/A/B or an HLTV top-N team."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from cs2bot.config import ScopeSettings, Settings
from cs2bot.db.models import Match, Team, TeamRanking, Tournament
from cs2bot.domain.enums import DataSource, MatchStatus, Tier
from cs2bot.ingest.scope import (
    is_in_scope,
    match_tier,
    refresh_match_scope,
    refresh_upcoming_scope,
    team_rank_as_of,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("tier", "rank1", "rank2", "expected"),
    [
        (Tier.S, None, None, True),
        (Tier.A, 200, 300, True),
        (Tier.B, None, None, True),
        (Tier.C, None, None, False),
        (Tier.D, 51, 80, False),
        (Tier.UNRANKED, None, None, False),
        (Tier.C, 50, None, True),  # the boundary is inclusive
        (Tier.C, None, 1, True),
        (Tier.UNRANKED, 51, 49, True),
        (Tier.D, 0, -3, False),  # non-positive ranks mean "unknown"
    ],
)
def test_is_in_scope(tier: Tier, rank1: int | None, rank2: int | None, expected: bool) -> None:
    assert is_in_scope(tier, rank1, rank2, ScopeSettings()) is expected


def test_is_in_scope_follows_the_settings() -> None:
    strict = ScopeSettings(pandascore_tiers=["S"], hltv_top_n=10)
    assert is_in_scope(Tier.S, None, None, strict) is True  # configured tiers are case-insensitive
    assert is_in_scope(Tier.A, None, None, strict) is False
    assert is_in_scope(Tier.A, 10, None, strict) is True and is_in_scope(Tier.A, 11, 30, strict) is False
    assert is_in_scope("b", None, None, ScopeSettings()) is True  # plain strings and None are accepted
    assert is_in_scope(None, None, None, ScopeSettings()) is False
    assert is_in_scope(Tier.S, 1, 2, ScopeSettings(pandascore_tiers=[], hltv_top_n=0)) is False


def make(
    session: Session,
    *,
    tier: Tier = Tier.UNRANKED,
    rank1: int | None = None,
    rank2: int | None = None,
    when: datetime | None = None,
    status: MatchStatus = MatchStatus.SCHEDULED,
    tournament_tier: Tier | None = None,
) -> Match:
    team1, team2 = Team(name=f"T{rank1}-a", hltv_rank=rank1), Team(name=f"T{rank2}-b", hltv_rank=rank2)
    session.add_all([team1, team2])
    tournament = None
    if tournament_tier is not None:
        tournament = Tournament(name="Event", tier=tournament_tier)
        session.add(tournament)
    session.flush()
    match = Match(
        team1_id=team1.id,
        team2_id=team2.id,
        tournament_id=tournament.id if tournament else None,
        scheduled_at=when or NOW + timedelta(hours=5),
        status=status,
        tier=tier,
        in_scope=False,
    )
    session.add(match)
    session.flush()
    return match


def test_refresh_match_scope_uses_tier_and_team_ranks(session: Session, settings: Settings) -> None:
    by_tier = make(session, tier=Tier.A)
    by_rank = make(session, tier=Tier.C, rank2=37)
    neither = make(session, tier=Tier.C, rank1=120, rank2=64)
    by_tournament = make(session, tournament_tier=Tier.B)
    assert refresh_match_scope(session, by_tier, settings.scope, now=NOW) is True
    assert refresh_match_scope(session, by_rank, settings, now=NOW) is True  # Settings works as well
    assert refresh_match_scope(session, neither, settings.scope, now=NOW) is False
    assert refresh_match_scope(session, by_tournament, settings.scope, now=NOW) is True
    assert (by_tier.in_scope, by_rank.in_scope, neither.in_scope, by_tournament.in_scope) == (
        True,
        True,
        False,
        True,
    )
    assert match_tier(session, by_tournament) is Tier.B and match_tier(session, neither) is Tier.C


def test_scope_is_sticky_unless_asked(session: Session, settings: Settings) -> None:
    match = make(session, tier=Tier.C, rank1=40)
    assert refresh_match_scope(session, match, settings.scope, now=NOW) is True
    team = session.get(Team, match.team1_id)
    assert team is not None
    team.hltv_rank = 77  # dropped out of the top 50 after the match was analysed
    assert refresh_match_scope(session, match, settings.scope, now=NOW) is True
    assert refresh_match_scope(session, match, settings.scope, sticky=False, now=NOW) is False
    assert match.in_scope is False


def test_old_matches_use_the_ranking_history(session: Session, settings: Settings) -> None:
    old_day = datetime(2024, 5, 12, 15, 0, tzinfo=UTC)
    match = make(session, tier=Tier.C, rank1=3, when=old_day, status=MatchStatus.FINISHED)
    team = session.get(Team, match.team1_id)
    assert team is not None
    # today's rank 3 says nothing about May 2024: without history the rank is unknown
    assert team_rank_as_of(session, team, old_day, now=NOW) is None
    assert refresh_match_scope(session, match, settings.scope, now=NOW) is False
    session.add_all(
        [
            TeamRanking(team_id=team.id, source=DataSource.HLTV, rank=95, ranked_on=date(2024, 4, 1)),
            TeamRanking(team_id=team.id, source=DataSource.HLTV, rank=44, ranked_on=date(2024, 5, 6)),
            TeamRanking(
                team_id=team.id, source=DataSource.HLTV, rank=8, ranked_on=date(2024, 5, 13)
            ),  # later
        ]
    )
    session.flush()
    assert team_rank_as_of(session, team, old_day, now=NOW) == 44
    assert refresh_match_scope(session, match, settings.scope, now=NOW) is True
    # recent and upcoming matches use the current rank
    assert team_rank_as_of(session, team, NOW - timedelta(days=3), now=NOW) == 3
    assert team_rank_as_of(session, team, None, now=NOW) == 3 and team_rank_as_of(session, None, NOW) is None


def test_refresh_upcoming_scope_after_a_ranking_update(session: Session, settings: Settings) -> None:
    upcoming = make(session, tier=Tier.C, rank1=60)
    finished = make(session, tier=Tier.C, rank1=61, status=MatchStatus.FINISHED)
    already = make(session, tier=Tier.S)
    already.in_scope = True
    assert refresh_upcoming_scope(session, settings, now=NOW) == 0
    for match in (upcoming, finished):
        team = session.get(Team, match.team1_id)
        assert team is not None
        team.hltv_rank = 20
    assert refresh_upcoming_scope(session, settings.scope, now=NOW) == 1
    assert upcoming.in_scope is True and finished.in_scope is False  # finished matches are left alone
    assert refresh_upcoming_scope(session, settings, now=NOW) == 0
