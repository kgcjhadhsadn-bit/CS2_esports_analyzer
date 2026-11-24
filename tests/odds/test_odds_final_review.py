"""Final review (money path): a linked event whose Betclic team order changes must keep its orientation.

``teams_swapped`` is positional (bookmaker team1 == our team2) while the market mapper places each
selection by matching its text against the CURRENT ``team1_raw`` / ``team2_raw``. A CONFIRMED link is never
re-scored, so before the fix a payload listing the teams the other way round keyed every selection for the
other team: the model's probability of team A was priced against Betclic's odds of team B.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from cs2bot.db.models import BookmakerEvent, BookmakerMarket, Match
from cs2bot.domain.enums import LinkStatus
from cs2bot.odds.market_mapping import MarketMapper
from cs2bot.odds.quotes import latest_quotes
from cs2bot.utils.paths import project_root
from tests.odds.odds_testkit import event, make_match, make_team
from tests.odds.test_odds_collector import NOW, T0, _selection, run, standard_event


@pytest.fixture(scope="module")
def mapper() -> MarketMapper:
    return MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")


@pytest.fixture
def linked_match(session) -> Match:  # type: ignore[no-untyped-def]
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    return make_match(session, navi, faze, T0)


def _flipped(team1: str = "FaZe Clan", team2: str = "Natus Vincere"):  # type: ignore[no-untyped-def]
    """The standard event's markets, with Betclic now listing ``team1 - team2``."""
    return event("1001", team1, team2, starts_at=T0, markets=standard_event().markets)


def test_confirmed_link_keeps_the_teams_when_betclic_flips_their_order(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    ev = session.scalar(select(BookmakerEvent))
    ev.link_status = LinkStatus.CONFIRMED  # the owner confirmed "Natus Vincere - FaZe Clan", not swapped
    session.flush()
    assert _selection(session, "s1").selection_key == "team1"  # NaVi is our team1

    report = run(session, settings, mapper, [_flipped()], NOW + timedelta(minutes=3))
    assert report.errors == [] and report.events_remapped == 1
    session.refresh(ev)
    assert ev.link_status is LinkStatus.CONFIRMED and ev.teams_swapped is True
    assert (ev.team1_raw, ev.team2_raw) == ("FaZe Clan", "Natus Vincere")
    assert "remap_required" not in ev.extra and "team_order_flipped_at" in ev.extra
    # every selection still belongs to the same team
    assert _selection(session, "s1").selection_key == "team1"  # Natus Vincere
    assert _selection(session, "s2").selection_key == "team2"  # FaZe Clan
    assert _selection(session, "s3").selection_key == "team1"  # Natus Vincere (-1,5)
    handicap = session.scalar(select(BookmakerMarket).where(BookmakerMarket.raw_name == "Handicap map"))
    assert handicap.spec_key == "map_handicap|line=-1.5"  # our team1 (NaVi) gives 1.5 maps
    quotes = {
        (q.spec.key(), q.selection_raw_name): q.selection_key
        for q in latest_quotes(session, linked_match.id, max_age_s=600, now=NOW + timedelta(minutes=4))
    }
    assert quotes[("match_winner", "Natus Vincere")] == "team1"
    assert quotes[("match_winner", "FaZe Clan")] == "team2"

    # flipping back restores the original flag; a stable order changes nothing
    run(session, settings, mapper, [standard_event()], NOW + timedelta(minutes=6))
    session.refresh(ev)
    assert ev.teams_swapped is False and _selection(session, "s1").selection_key == "team1"
    again = run(session, settings, mapper, [standard_event()], NOW + timedelta(minutes=9))
    assert again.events_remapped == 0


def test_auto_link_keeps_the_teams_when_betclic_flips_their_order(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    run(session, settings, mapper, [_flipped()], NOW + timedelta(minutes=3))
    ev = session.scalar(select(BookmakerEvent))
    assert ev.link_status is LinkStatus.AUTO and ev.teams_swapped is True
    assert _selection(session, "s1").selection_key == "team1"
    assert _selection(session, "s2").selection_key == "team2"


def test_confirmed_link_goes_back_to_review_when_both_teams_are_renamed(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    ev = session.scalar(select(BookmakerEvent))
    ev.link_status = LinkStatus.CONFIRMED
    session.flush()
    renamed = event("1001", "Team Alpha", "Team Omega", starts_at=T0, markets=standard_event().markets)
    run(session, settings, mapper, [renamed], NOW + timedelta(minutes=3))
    session.refresh(ev)
    assert ev.link_status not in (LinkStatus.CONFIRMED, LinkStatus.AUTO)
    assert "review_reason" in ev.extra
    assert latest_quotes(session, linked_match.id, max_age_s=600, now=NOW + timedelta(minutes=4)) == []


def test_one_renamed_team_keeps_a_confirmed_link(session, settings, mapper, linked_match) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    ev = session.scalar(select(BookmakerEvent))
    ev.link_status = LinkStatus.CONFIRMED
    session.flush()
    renamed = event("1001", "Natus Vincere", "FaZe", starts_at=T0, markets=standard_event().markets)
    run(session, settings, mapper, [renamed], NOW + timedelta(minutes=3))
    session.refresh(ev)
    assert ev.link_status is LinkStatus.CONFIRMED and ev.teams_swapped is False
    assert _selection(session, "s1").selection_key == "team1"
