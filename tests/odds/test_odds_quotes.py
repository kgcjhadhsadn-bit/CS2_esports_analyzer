from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cs2bot.db.models import BookmakerEvent, BookmakerSelection, OddsSnapshot
from cs2bot.domain.enums import LinkStatus
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.odds.collector import collect_once
from cs2bot.odds.market_mapping import MarketMapper
from cs2bot.odds.quotes import closing_odds, latest_quote, latest_quotes, odds_history
from cs2bot.odds.sources import StaticOddsSource
from cs2bot.utils.paths import project_root
from tests.odds.odds_testkit import event, make_match, make_team, market, sel

T0 = datetime(2026, 11, 23, 17, 0, tzinfo=UTC)
NOW = T0 - timedelta(hours=5)


@pytest.fixture(scope="module")
def mapper() -> MarketMapper:
    return MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")


def sample(navi: str = "1.62", *, live: bool = False, starts_at: datetime = T0):
    return event(
        "1001",
        starts_at=starts_at,
        is_live=live,
        markets=[
            market(
                "Zwycięzca meczu", sel("Natus Vincere", navi, "s1"), sel("FaZe Clan", "2.25", "s2"), ext="m1"
            ),
            market(
                "Pierwsza krew - mapa 1",
                sel("Natus Vincere", "1.8", "s3"),
                sel("FaZe Clan", "1.9", "s4"),
                ext="m2",
            ),
        ],
    )


@pytest.fixture
def match(session):
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    return make_match(session, navi, faze, T0)


def collect(session, settings, mapper, ev, now):
    return collect_once(session, StaticOddsSource([ev]), mapper, settings=settings, now=now)


def test_latest_quotes_returns_mapped_fresh_available_selections(session, settings, mapper, match) -> None:
    collect(session, settings, mapper, sample(), NOW)
    quotes = latest_quotes(session, match.id, max_age_s=600, now=NOW + timedelta(minutes=5))
    assert [(q.selection_key, q.odds) for q in quotes] == [
        ("team1", Decimal("1.62")),
        ("team2", Decimal("2.25")),
    ]
    q = quotes[0]
    assert q.spec == MarketSpec(market_type=MarketType.MATCH_WINNER)
    assert (q.market_raw_name, q.selection_raw_name) == ("Zwycięzca meczu", "Natus Vincere")
    assert (q.external_event_id, q.market_external_id, q.selection_external_id) == ("1001", "m1", "s1")
    assert q.captured_at == NOW and q.event_url and q.event_url.endswith("-m1001")
    assert latest_quote(session, q.selection_id, max_age_s=600, now=NOW + timedelta(minutes=5)) == q


def test_stale_quotes_are_dropped(session, settings, mapper, match) -> None:
    collect(session, settings, mapper, sample(), NOW)
    assert latest_quotes(session, match.id, max_age_s=600, now=NOW + timedelta(minutes=11)) == []
    # an unchanged price seen again is fresh again (last_odds_at refreshed without a snapshot)
    collect(session, settings, mapper, sample(), NOW + timedelta(minutes=11))
    assert len(latest_quotes(session, match.id, max_age_s=600, now=NOW + timedelta(minutes=12))) == 2


def test_live_events_and_started_events_are_not_quoted(session, settings, mapper, match) -> None:
    collect(session, settings, mapper, sample(), NOW)
    ev = session.scalar(select(BookmakerEvent))
    ev.is_live = True
    session.flush()
    assert latest_quotes(session, match.id, max_age_s=600, now=NOW) == []
    ev.is_live = False
    session.flush()
    assert latest_quotes(session, match.id, max_age_s=10**9, now=T0 + timedelta(minutes=1)) == []


def test_link_status_gate(session, settings, mapper, match) -> None:
    collect(session, settings, mapper, sample(), NOW)
    ev = session.scalar(select(BookmakerEvent))
    ev.link_status = LinkStatus.PENDING_REVIEW
    session.flush()
    assert latest_quotes(session, match.id, max_age_s=600, now=NOW) == []
    ev.link_status = LinkStatus.CONFIRMED
    session.flush()
    assert len(latest_quotes(session, match.id, max_age_s=600, now=NOW)) == 2
    ev.link_status = LinkStatus.REJECTED
    session.flush()
    assert latest_quotes(session, match.id, max_age_s=600, now=NOW) == []


def test_unavailable_selections_are_not_quoted(session, settings, mapper, match) -> None:
    collect(session, settings, mapper, sample(), NOW)
    s1 = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    s1.is_available = False
    session.flush()
    assert [q.selection_external_id for q in latest_quotes(session, match.id, max_age_s=600, now=NOW)] == [
        "s2"
    ]
    assert latest_quote(session, s1.id, max_age_s=600, now=NOW) is None


def test_closing_odds_is_last_available_snapshot_strictly_before_kickoff(
    session, settings, mapper, match
) -> None:
    collect(session, settings, mapper, sample("1.62"), T0 - timedelta(hours=3))
    collect(session, settings, mapper, sample("1.70"), T0 - timedelta(minutes=30))
    s1 = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    # suspension shortly before kick-off and a price at kick-off must not count
    session.add(
        OddsSnapshot(
            selection_id=s1.id,
            odds=Decimal("1.70"),
            is_available=False,
            captured_at=T0 - timedelta(minutes=2),
        )
    )
    session.add(OddsSnapshot(selection_id=s1.id, odds=Decimal("1.40"), is_available=True, captured_at=T0))
    session.flush()
    assert closing_odds(session, s1.id, T0) == Decimal("1.70")
    assert closing_odds(session, s1.id, T0 - timedelta(hours=1)) == Decimal("1.62")
    assert closing_odds(session, s1.id, T0 - timedelta(hours=4)) is None
    history = odds_history(session, s1.id)
    assert [(odds, avail) for _, odds, avail in history] == [
        (Decimal("1.62"), True),
        (Decimal("1.70"), True),
        (Decimal("1.70"), False),
        (Decimal("1.40"), True),
    ]
    assert [ts for ts, _, _ in history] == sorted(ts for ts, _, _ in history)


def test_unknown_selection_and_match(session) -> None:
    assert latest_quotes(session, 12345, max_age_s=600) == []
    assert latest_quote(session, 999) is None
    assert closing_odds(session, 999, T0) is None
    assert odds_history(session, 999) == []
