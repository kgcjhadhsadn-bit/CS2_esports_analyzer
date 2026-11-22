from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, OddsSnapshot
from cs2bot.domain.enums import LinkStatus
from cs2bot.domain.odds import CollectedEvent
from cs2bot.odds.collector import CollectorConfig, collect_once, original_market_external_id, remap_event
from cs2bot.odds.market_mapping import MarketMapper
from cs2bot.odds.sources import FetchInfo, StaticOddsSource
from cs2bot.utils.paths import project_root
from tests.odds.odds_testkit import add_lineup, event, make_match, make_team, market, sel

T0 = datetime(2026, 11, 23, 17, 0, tzinfo=UTC)
NOW = T0 - timedelta(hours=5)


@pytest.fixture(scope="module")
def mapper() -> MarketMapper:
    return MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")


def standard_event(*, navi: str = "1.62", with_total: bool = True, live: bool = False) -> CollectedEvent:
    markets = [
        market("Zwycięzca meczu", sel("Natus Vincere", navi, "s1"), sel("FaZe Clan", "2.25", "s2"), ext="m1"),
        market(
            "Handicap map",
            sel("Natus Vincere (-1,5)", "2.95", "s3"),
            sel("FaZe Clan (+1,5)", "1.38", "s4"),
            ext="m2",
        ),
    ]
    if with_total:
        markets.append(
            market("Liczba map", sel("Powyżej 2,5", "2.10", "s5"), sel("Poniżej 2,5", "1.68", "s6"), ext="m3")
        )
    return event("1001", starts_at=T0, markets=markets, is_live=live)


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def linked_match(session):
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    return make_match(session, navi, faze, T0)


def run(session, settings, mapper, events, now, **kw):
    return collect_once(session, StaticOddsSource(events, **kw), mapper, settings=settings, now=now)


def test_first_run_inserts_everything(session, settings: Settings, mapper, linked_match) -> None:
    report = run(session, settings, mapper, [standard_event()], NOW)
    assert report.errors == [] and report.events_new == 1
    assert (report.markets_new, report.selections_new, report.snapshots_written) == (3, 6, 6)
    assert report.markets_mapped == 3 and report.selections_mapped == 6
    assert report.link_status_counts == {"auto": 1}
    assert (
        count(session, BookmakerEvent),
        count(session, BookmakerMarket),
        count(session, BookmakerSelection),
    ) == (1, 3, 6)
    ev = session.scalar(select(BookmakerEvent))
    assert ev.match_id == linked_match.id and ev.link_status is LinkStatus.AUTO and ev.teams_swapped is False
    winner = session.scalar(select(BookmakerMarket).where(BookmakerMarket.raw_name == "Zwycięzca meczu"))
    fields = ("map_number", "maps_scope", "line", "team", "player_id", "player_name")
    empty = dict.fromkeys((*fields, "opponent_player_id", "half", "round_number"))
    assert winner.spec_key == "match_winner" and winner.spec == {"market_type": "match_winner", **empty}
    navi_sel = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert (
        navi_sel.selection_key == "team1"
        and navi_sel.last_odds == Decimal("1.62")
        and navi_sel.last_odds_at == NOW
    )


def test_second_identical_run_writes_no_snapshot_but_refreshes_timestamps(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    later = NOW + timedelta(minutes=3)
    report = run(session, settings, mapper, [standard_event()], later)
    assert report.snapshots_written == 0 and report.events_new == 0 and report.selections_new == 0
    assert count(session, OddsSnapshot) == 6
    sel_row = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert sel_row.last_odds_at == later
    ev = session.scalar(select(BookmakerEvent))
    assert ev.last_seen_at == later and ev.first_seen_at == NOW


def test_price_change_writes_exactly_one_snapshot(session, settings, mapper, linked_match) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    report = run(session, settings, mapper, [standard_event(navi="1.70")], NOW + timedelta(minutes=3))
    assert report.snapshots_written == 1
    sel_row = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    snaps = session.scalars(
        select(OddsSnapshot).where(OddsSnapshot.selection_id == sel_row.id).order_by(OddsSnapshot.id)
    ).all()
    assert [s.odds for s in snaps] == [Decimal("1.62"), Decimal("1.70")]


def test_disappearing_market_and_selection_become_unavailable(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    smaller = standard_event(with_total=False)
    smaller.markets[1].selections = smaller.markets[1].selections[:1]  # one handicap selection vanished
    report = run(session, settings, mapper, [smaller], NOW + timedelta(minutes=3))
    assert report.markets_disappeared == 1
    assert report.selections_marked_unavailable == 3 and report.snapshots_written == 3
    gone = session.scalars(select(BookmakerSelection).where(BookmakerSelection.is_available.is_(False))).all()
    assert {s.external_id for s in gone} == {"s4", "s5", "s6"}
    snap = session.scalar(
        select(OddsSnapshot).where(
            OddsSnapshot.selection_id == gone[0].id, OddsSnapshot.is_available.is_(False)
        )
    )
    assert snap is not None and snap.odds == gone[0].last_odds  # unavailable snapshot keeps the last price
    # coming back writes an "available again" snapshot
    report = run(session, settings, mapper, [standard_event()], NOW + timedelta(minutes=6))
    assert report.snapshots_written == 3 and report.selections_marked_unavailable == 0


def test_partial_events_never_mark_missing_markets_unavailable(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    report = run(
        session,
        settings,
        mapper,
        [standard_event(with_total=False)],
        NOW + timedelta(minutes=3),
        partial_event_ids={"1001"},
    )
    assert report.events_partial == 1 and report.selections_marked_unavailable == 0
    ev = session.scalar(select(BookmakerEvent))
    assert ev.extra["partial"] is True


def test_suspended_selection_from_source_is_snapshotted(session, settings, mapper, linked_match) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    suspended = standard_event()
    suspended.markets[0].selections[0].is_available = False
    report = run(session, settings, mapper, [suspended], NOW + timedelta(minutes=3))
    assert report.snapshots_written == 1
    row = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert row.is_available is False


def test_relink_with_swap_remaps_selections(session, settings, mapper) -> None:
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    report = run(session, settings, mapper, [standard_event()], NOW)
    assert report.link_status_counts == {"unlinked": 1}
    s1 = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert s1.selection_key == "team1"  # bookmaker orientation while unlinked
    make_match(session, faze, navi, T0)  # our team1 is FaZe -> the Betclic event is swapped
    report = run(session, settings, mapper, [standard_event()], NOW + timedelta(minutes=3))
    assert report.link_status_counts == {"auto": 1}
    assert report.snapshots_written == 0  # re-mapping never touches prices
    session.refresh(s1)
    assert s1.selection_key == "team2"
    handicap = session.scalar(select(BookmakerMarket).where(BookmakerMarket.raw_name == "Handicap map"))
    assert handicap.spec_key == "map_handicap|line=1.5"  # NAVI -1.5 is now our team2's handicap


def test_remap_event_after_manual_swap(session, settings, mapper, linked_match) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    ev = session.scalar(select(BookmakerEvent))
    ev.link_status, ev.teams_swapped = LinkStatus.CONFIRMED, True  # dashboard decision
    changed = remap_event(session, ev, mapper)
    assert changed == 4  # both winner and both handicap selections flip; totals are orientation-free
    s1 = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert s1.selection_key == "team2"
    report = run(session, settings, mapper, [standard_event()], NOW + timedelta(minutes=3))
    assert report.link_status_counts == {"confirmed": 1}
    session.refresh(s1)
    assert s1.selection_key == "team2"  # the manual orientation is kept on later passes


def test_live_events_are_stored_but_not_priced(session, settings, mapper, linked_match) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    report = run(
        session, settings, mapper, [standard_event(navi="1.20", live=True)], NOW + timedelta(minutes=3)
    )
    assert report.events_live == 1 and report.snapshots_written == 0
    ev = session.scalar(select(BookmakerEvent))
    assert ev.is_live is True
    s1 = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    assert s1.last_odds == Decimal("1.62")  # the last pre-match price is kept
    stored = collect_once(
        session,
        StaticOddsSource([standard_event(navi="1.20", live=True)]),
        mapper,
        settings=settings,
        now=NOW + timedelta(minutes=6),
        config=CollectorConfig(store_live_odds=True),
    )
    assert stored.snapshots_written == 1


def test_one_bad_event_does_not_abort_the_run(session, settings, mapper, linked_match) -> None:
    bad = event(
        "2002", "Alpha", "Bravo", starts_at=datetime(2026, 11, 23, 17), markets=[]
    )  # naive -> DB rejects
    report = run(session, settings, mapper, [bad, standard_event()], NOW)
    assert report.events_failed == 1 and "2002" in report.errors[0]
    assert count(session, BookmakerEvent) == 1 and report.snapshots_written == 6


def test_multi_line_market_is_split_and_quotes_keep_betclic_id(
    session, settings, mapper, linked_match
) -> None:
    ladder = market(
        "Handicap map",
        sel("Natus Vincere (-1,5)", "2.95", "a"),
        sel("FaZe Clan (+1,5)", "1.38", "b"),
        sel("Natus Vincere (+1,5)", "1.20", "c"),
        sel("FaZe Clan (-1,5)", "4.10", "d"),
        ext="HM",
    )
    report = run(session, settings, mapper, [event("1001", starts_at=T0, markets=[ladder])], NOW)
    assert report.markets_new == 2 and report.selections_mapped == 4
    rows = session.scalars(select(BookmakerMarket)).all()
    assert {r.spec_key for r in rows} == {"map_handicap|line=-1.5", "map_handicap|line=1.5"}
    assert all(
        r.external_id.startswith("HM~") and original_market_external_id(r.external_id) == "HM" for r in rows
    )
    again = run(
        session, settings, mapper, [event("1001", starts_at=T0, markets=[ladder])], NOW + timedelta(minutes=3)
    )
    assert again.markets_new == 0 and again.snapshots_written == 0  # the split is deterministic


def test_player_markets_use_the_match_lineups(session, settings, mapper) -> None:
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    match = make_match(session, navi, faze, T0)
    ids = add_lineup(session, match, navi, ["s1mple", "b1t"])
    kills = market(
        "Zabójstwa gracza - mapa 1",
        sel("s1mple Powyżej 19,5", "1.83", "k1"),
        sel("s1mple Poniżej 19,5", "1.93", "k2"),
        ext="K1",
    )
    run(session, settings, mapper, [event("1001", starts_at=T0, markets=[kills])], NOW)
    row = session.scalar(select(BookmakerMarket))
    assert row.is_mapped and row.spec["player_id"] == ids["s1mple"]
    assert row.spec_key == f"player_kills|map=1|player={ids['s1mple']}|line=19.5"


def test_duplicate_events_and_markets_are_merged(session, settings, mapper, linked_match) -> None:
    a = event(
        "1001", starts_at=T0, markets=[market("Zwycięzca meczu", sel("Natus Vincere", "1.6", "s1"), ext="m1")]
    )
    b = event(
        "1001", starts_at=None, markets=[market("Zwycięzca meczu", sel("FaZe Clan", "2.3", "s2"), ext="m1")]
    )
    report = run(session, settings, mapper, [a, b], NOW)
    assert report.events_seen == 1 and report.markets_new == 1 and report.selections_new == 2


def test_raw_payload_paths_and_source_summary_recorded(session, settings, mapper, linked_match) -> None:
    class Source(StaticOddsSource):
        def last_fetch_info(self) -> FetchInfo:
            return FetchInfo(
                pages_visited=3,
                payloads_captured=7,
                raw_paths_by_event={"1001": ["/raw/2026-11-22/a.json", "/raw/2026-11-22/b.bin"]},
            )

    report = collect_once(session, Source([standard_event()]), mapper, settings=settings, now=NOW)
    assert report.source["pages_visited"] == 3
    ev = session.scalar(select(BookmakerEvent))
    assert ev.extra["raw_payloads"] == ["/raw/2026-11-22/a.json", "/raw/2026-11-22/b.bin"]
    assert ev.extra["raw_payloads_at"] == NOW.isoformat()


def test_unmapped_markets_listed_in_report(session, settings, mapper, linked_match) -> None:
    odd = market(
        "Pierwsza krew - mapa 1", sel("Natus Vincere", "1.8", "x1"), sel("FaZe Clan", "1.9", "x2"), ext="X"
    )
    report = run(session, settings, mapper, [event("1001", starts_at=T0, markets=[odd])], NOW)
    assert report.unmapped_markets == ["Pierwsza krew - mapa 1"]
    row = session.scalar(select(BookmakerMarket))
    assert row.is_mapped is False and row.spec is None
    assert report.snapshots_written == 2  # unmapped prices are still recorded


def test_source_errors_propagate(session, settings, mapper) -> None:
    class Broken:
        def fetch_cs2_events(self) -> list[CollectedEvent]:
            raise RuntimeError("browser crashed")

    with pytest.raises(RuntimeError, match="browser crashed"):
        collect_once(session, Broken(), mapper, settings=settings, now=NOW)


def test_prices_are_stamped_with_the_source_capture_time(session, settings, mapper, linked_match) -> None:
    captured = datetime.now(UTC) - timedelta(minutes=7)

    class Source(StaticOddsSource):
        def last_fetch_info(self) -> FetchInfo:
            return FetchInfo(captured_at_by_event={"1001": captured})

    collect_once(session, Source([standard_event()]), mapper, settings=settings)
    row = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == "s1"))
    snap = session.scalar(select(OddsSnapshot).where(OddsSnapshot.selection_id == row.id))
    assert row.last_odds_at == captured and snap.captured_at == captured
    ev = session.scalar(select(BookmakerEvent))
    assert ev.last_seen_at == captured
    # without capture times the clock after the fetch is used (never the run start)
    before = datetime.now(UTC)
    collect_once(session, StaticOddsSource([standard_event(navi="1.75")]), mapper, settings=settings)
    session.refresh(row)
    assert row.last_odds_at >= before
