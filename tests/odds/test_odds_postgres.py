"""The odds store on PostgreSQL (production database). Skipped unless CS2BOT_TEST_DATABASE_URL is set."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from cs2bot.config import Settings
from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, OddsSnapshot, TeamAlias
from cs2bot.domain.enums import DataSource, LinkStatus
from cs2bot.odds.collector import collect_once
from cs2bot.odds.market_mapping import MarketMapper, list_unmapped, list_unmapped_selections
from cs2bot.odds.quotes import closing_odds, latest_quotes, odds_history
from cs2bot.odds.sources import StaticOddsSource
from cs2bot.utils.paths import project_root
from tests.odds.odds_testkit import event, make_match, make_team, market, sel

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 11, 23, 17, 0, tzinfo=UTC)
NOW = T0 - timedelta(hours=5)


def _event(navi: str = "1.62", *, with_total: bool = True):
    markets = [
        market("Zwycięzca meczu", sel("Natus Vincere", navi, "s1"), sel("FaZe Clan", "2.25", "s2"), ext="m1"),
        market(
            "Handicap map",
            sel("Natus Vincere (-1,5)", "2.95", "s3"),
            sel("FaZe Clan (+1,5)", "1.38", "s4"),
            sel("Natus Vincere (+1,5)", "1.20", "s5"),
            ext="m2",
        ),
        market("Pierwsza krew - mapa 1", sel("Natus Vincere", "1.8", "x1"), ext="X" * 80),
    ]
    if with_total:
        markets.append(
            market("Liczba map", sel("Powyżej 2,5", "2.10", "s6"), sel("Poniżej 2,5", "1.68", "s7"), ext="m3")
        )
    return event("1001", starts_at=T0, markets=markets)


def test_collect_link_remap_and_quote_on_postgres(pg_session, settings: Settings) -> None:
    session = pg_session
    mapper = MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    match = make_match(session, faze, navi, T0)  # swapped on purpose
    session.commit()

    def run(events, now, **kw):
        report = collect_once(session, StaticOddsSource(events, **kw), mapper, settings=settings, now=now)
        session.commit()
        return report

    bad = event("2002", "Alpha", "Bravo", starts_at=datetime(2026, 11, 23, 17), markets=[])  # naive datetime
    first = run([bad, _event()], NOW)
    assert first.events_failed == 1 and first.events_new == 1 and first.link_status_counts == {"auto": 1}
    assert first.snapshots_written == 8 and first.markets_new == 5  # the handicap ladder is split in two
    ev = session.scalar(select(BookmakerEvent))
    assert ev.match_id == match.id and ev.teams_swapped is True and ev.link_status is LinkStatus.AUTO
    assert (
        session.scalar(
            select(func.count()).select_from(TeamAlias).where(TeamAlias.source == DataSource.BETCLIC)
        )
        == 2
    )

    assert run([_event()], NOW + timedelta(minutes=3)).snapshots_written == 0
    changed = run([_event("1.70", with_total=False)], NOW + timedelta(minutes=6))
    assert changed.snapshots_written == 3 and changed.selections_marked_unavailable == 2

    quotes = latest_quotes(session, match.id, max_age_s=600, now=NOW + timedelta(minutes=7))
    assert {(q.spec.key(), q.selection_key, q.odds) for q in quotes} == {
        ("match_winner", "team2", Decimal("1.700")),
        ("match_winner", "team1", Decimal("2.250")),
        ("map_handicap|line=1.5", "team2", Decimal("2.950")),
        ("map_handicap|line=1.5", "team1", Decimal("1.380")),
        ("map_handicap|line=-1.5", "team2", Decimal("1.200")),
    }
    assert all(q.market_external_id in ("m1", "m2") for q in quotes)
    s1 = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert closing_odds(session, s1.id, T0) == Decimal("1.700")
    assert [o for _, o, _ in odds_history(session, s1.id)] == [Decimal("1.620"), Decimal("1.700")]
    assert list_unmapped(session) == [("Pierwsza krew - mapa 1", 1)]
    assert list_unmapped_selections(session) == []

    # manual review (as the API does it): confirmed with the other orientation, mappings cleared, flagged
    ev.link_status, ev.teams_swapped = LinkStatus.CONFIRMED, False
    for market_row in ev.markets:
        market_row.is_mapped, market_row.spec, market_row.spec_key, market_row.market_type = (
            False,
            None,
            None,
            None,
        )
        for sel_row in market_row.selections:
            sel_row.is_mapped, sel_row.selection_key = False, None
    ev.extra = {**ev.extra, "remap_required": True}
    session.commit()
    report = run([], NOW + timedelta(minutes=9))
    assert report.events_remapped == 1 and report.errors == []
    session.refresh(ev)
    session.refresh(s1)
    assert "remap_required" not in ev.extra and s1.selection_key == "team1"
    assert session.scalar(select(func.count()).select_from(OddsSnapshot)) == 11
    assert session.scalar(select(func.count()).select_from(BookmakerMarket)) == 5


def test_manual_review_committed_during_a_pass_is_not_overwritten(pg_session, settings: Settings) -> None:
    """The collector re-reads (and locks) the event row: a stale copy must never undo a manual decision."""
    from sqlalchemy.orm import sessionmaker

    session = pg_session
    mapper = MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    match = make_match(session, navi, faze, T0)
    collect_once(session, StaticOddsSource([_event()]), mapper, settings=settings, now=NOW)
    session.commit()
    stale = session.scalar(select(BookmakerEvent))  # stays in this session's identity map
    assert stale.link_status is LinkStatus.AUTO and stale.teams_swapped is False

    other = sessionmaker(bind=session.get_bind(), expire_on_commit=False)()
    try:
        reviewed = other.scalar(select(BookmakerEvent))
        reviewed.link_status, reviewed.teams_swapped = LinkStatus.CONFIRMED, True
        reviewed.extra = {**reviewed.extra, "remap_required": True, "manual_review": {"action": "confirm"}}
        other.commit()
    finally:
        other.close()

    report = collect_once(
        session, StaticOddsSource([_event()]), mapper, settings=settings, now=NOW + timedelta(minutes=3)
    )
    session.commit()
    assert report.link_status_counts == {"confirmed": 1} and report.events_remapped == 1
    session.refresh(stale)
    assert (stale.link_status, stale.teams_swapped, stale.match_id) == (LinkStatus.CONFIRMED, True, match.id)
    assert stale.extra["manual_review"] == {"action": "confirm"} and "remap_required" not in stale.extra
    s1 = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert s1.selection_key == "team2"  # keyed for the manually confirmed orientation
