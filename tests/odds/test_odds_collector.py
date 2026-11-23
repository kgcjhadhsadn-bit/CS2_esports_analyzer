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


# --------------------------------------------------------------------------- review round 1


def _review(session, ev: BookmakerEvent, *, swapped: bool, match_id: int | None, status: LinkStatus) -> None:
    """What the API's link review does (docs/dev/reports/api.md): clear the mappings and flag the event."""
    ev.link_status, ev.match_id, ev.teams_swapped = status, match_id, swapped
    for market_row in ev.markets:
        market_row.is_mapped, market_row.spec, market_row.spec_key, market_row.market_type = (
            False,
            None,
            None,
            None,
        )
        for sel_row in market_row.selections:
            sel_row.is_mapped, sel_row.selection_key = False, None
    ev.extra = {**(ev.extra or {}), "remap_required": True}
    session.flush()


def _selection(session, external_id: str) -> BookmakerSelection:
    row = session.scalar(select(BookmakerSelection).where(BookmakerSelection.external_id == external_id))
    assert row is not None
    return row


def _winner_only() -> CollectedEvent:
    full = standard_event()
    full.markets = full.markets[:1]
    return full


def test_link_review_flag_remaps_every_stored_market_even_on_a_partial_sighting(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    ev = session.scalar(select(BookmakerEvent))
    _review(session, ev, swapped=True, match_id=linked_match.id, status=LinkStatus.CONFIRMED)
    assert _selection(session, "s3").selection_key is None
    # the next pass only sees the match winner (listing page): the handicap must be re-keyed anyway
    report = run(
        session, settings, mapper, [_winner_only()], NOW + timedelta(minutes=3), partial_event_ids={"1001"}
    )
    assert report.events_remapped == 1 and report.errors == []
    session.refresh(ev)
    assert "remap_required" not in ev.extra and ev.link_status is LinkStatus.CONFIRMED
    assert _selection(session, "s1").selection_key == "team2"  # the confirmed (swapped) orientation
    assert _selection(session, "s3").selection_key == "team2" and _selection(session, "s3").is_mapped
    handicap = session.scalar(select(BookmakerMarket).where(BookmakerMarket.raw_name == "Handicap map"))
    assert handicap.is_mapped and handicap.spec_key == "map_handicap|line=1.5"
    assert _selection(session, "s5").selection_key == "over"
    # nothing was re-priced: only the first pass wrote snapshots
    assert count(session, OddsSnapshot) == 6
    again = run(session, settings, mapper, [_winner_only()], NOW + timedelta(minutes=6))
    assert again.events_remapped == 0


def test_flagged_event_that_is_no_longer_listed_is_remapped_too(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    ev = session.scalar(select(BookmakerEvent))
    _review(session, ev, swapped=True, match_id=linked_match.id, status=LinkStatus.CONFIRMED)
    report = run(session, settings, mapper, [], NOW + timedelta(minutes=3))
    assert report.events_seen == 0 and report.events_remapped == 1
    session.refresh(ev)
    assert "remap_required" not in ev.extra
    assert _selection(session, "s4").selection_key == "team1"  # FaZe is our team1 after the manual swap
    assert count(session, OddsSnapshot) == 6


def test_rejected_review_keeps_bookmaker_orientation_and_is_never_quoted(
    session, settings, mapper, linked_match
) -> None:
    from cs2bot.odds.quotes import latest_quotes

    run(session, settings, mapper, [standard_event()], NOW)
    ev = session.scalar(select(BookmakerEvent))
    _review(session, ev, swapped=False, match_id=None, status=LinkStatus.REJECTED)
    report = run(session, settings, mapper, [standard_event()], NOW + timedelta(minutes=3))
    assert report.link_status_counts == {"rejected": 1} and report.events_remapped == 1
    session.refresh(ev)
    assert ev.match_id is None and "remap_required" not in ev.extra
    assert latest_quotes(session, linked_match.id, max_age_s=600, now=NOW + timedelta(minutes=4)) == []


def test_relink_remaps_markets_this_pass_did_not_see(session, settings, mapper) -> None:
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    run(session, settings, mapper, [standard_event()], NOW)  # unlinked: bookmaker orientation
    assert _selection(session, "s3").selection_key == "team1"
    make_match(session, faze, navi, T0)  # our team1 is FaZe -> swapped
    report = run(
        session, settings, mapper, [_winner_only()], NOW + timedelta(minutes=3), partial_event_ids={"1001"}
    )
    assert report.events_relinked == 1 and report.events_remapped == 1
    assert _selection(session, "s3").selection_key == "team2"  # not seen in this pass, re-keyed anyway
    handicap = session.scalar(select(BookmakerMarket).where(BookmakerMarket.raw_name == "Handicap map"))
    assert handicap.spec_key == "map_handicap|line=1.5"


def test_capture_times_per_event_and_per_market(session, settings, mapper, linked_match) -> None:
    """Prices carry the time their own payload was captured, never a later payload's time."""
    event_at = datetime.now(UTC) - timedelta(minutes=2)
    handicap_at = event_at - timedelta(minutes=5)

    class Source(StaticOddsSource):
        def last_fetch_info(self) -> FetchInfo:
            return FetchInfo(captured_at_by_market={"1001": {("Handicap map", "m2"): handicap_at}})

    ev = standard_event()
    ev.captured_at = event_at  # CollectedEvent.captured_at (foundation field)
    collect_once(session, Source([ev]), mapper, settings=settings)
    assert _selection(session, "s1").last_odds_at == event_at
    assert _selection(session, "s3").last_odds_at == handicap_at
    assert _selection(session, "s3").last_seen_at == handicap_at
    snap = session.scalar(
        select(OddsSnapshot).where(OddsSnapshot.selection_id == _selection(session, "s3").id)
    )
    assert snap.captured_at == handicap_at
    row = session.scalar(select(BookmakerEvent))
    assert row.last_seen_at == event_at and row.first_seen_at == event_at


def test_capture_times_in_the_future_are_clamped(session, settings, mapper, linked_match) -> None:
    ev = standard_event()
    ev.captured_at = datetime.now(UTC) + timedelta(hours=2)  # a source with a broken clock
    collect_once(session, StaticOddsSource([ev]), mapper, settings=settings)
    assert _selection(session, "s1").last_odds_at <= datetime.now(UTC)


def test_older_observations_never_overwrite_newer_ones(session, settings, mapper, linked_match) -> None:
    """Replaying an archive (or a late payload) must not rewind prices or make them look fresh."""
    later = NOW + timedelta(minutes=10)
    run(session, settings, mapper, [standard_event(navi="1.70")], later)
    stale = standard_event(navi="1.20", with_total=False)
    report = run(session, settings, mapper, [stale], NOW)  # an observation from BEFORE the stored one
    assert report.selections_stale_ignored == 4 and report.snapshots_written == 0
    assert report.selections_marked_unavailable == 0  # the older sighting cannot "disappear" a market
    s1 = _selection(session, "s1")
    assert s1.last_odds == Decimal("1.70") and s1.last_odds_at == later and s1.last_seen_at == later
    assert all(s.is_available for s in session.scalars(select(BookmakerSelection)))
    ev = session.scalar(select(BookmakerEvent))
    assert ev.last_seen_at == later  # never moves backwards


def test_suspended_sighting_is_seen_but_does_not_confirm_the_price(
    session, settings, mapper, linked_match
) -> None:
    from cs2bot.odds.quotes import latest_quotes

    run(session, settings, mapper, [standard_event()], NOW)
    later = NOW + timedelta(minutes=3)
    suspended = standard_event()
    suspended.markets[0].selections[0].is_available = False
    run(session, settings, mapper, [suspended], later)
    s1, s2 = _selection(session, "s1"), _selection(session, "s2")
    assert (s1.last_seen_at, s1.last_odds_at, s1.is_available) == (later, NOW, False)
    assert (s2.last_seen_at, s2.last_odds_at, s2.is_available) == (later, later, True)
    quoted = {q.selection_external_id for q in latest_quotes(session, linked_match.id, now=later)}
    assert "s1" not in quoted and "s2" in quoted
    back = later + timedelta(minutes=3)
    report = run(session, settings, mapper, [standard_event()], back)
    assert report.snapshots_written == 1  # "available again" at the same price is a change
    assert _selection(session, "s1").last_odds_at == back


def test_disappeared_selection_keeps_its_last_confirmation_time(
    session, settings, mapper, linked_match
) -> None:
    run(session, settings, mapper, [standard_event()], NOW)
    later = NOW + timedelta(minutes=3)
    run(session, settings, mapper, [standard_event(with_total=False)], later)
    s5 = _selection(session, "s5")
    assert s5.is_available is False and s5.last_odds_at == NOW and s5.last_seen_at == NOW
    snaps = session.scalars(
        select(OddsSnapshot).where(OddsSnapshot.selection_id == s5.id).order_by(OddsSnapshot.id)
    ).all()
    assert [(s.is_available, s.captured_at) for s in snaps] == [(True, NOW), (False, later)]


def test_market_extra_records_betclic_id_rule_and_split_signature(
    session, settings, mapper, linked_match
) -> None:
    ladder = market(
        "Liczba rund - mapa 1",
        sel("Powyżej 21,5", "1.85", "a"),
        sel("Poniżej 21,5", "1.95", "b"),
        sel("Powyżej 23,5", "2.60", "c"),
        sel("Poniżej 23,5", "1.48", "d"),
        ext="80021",
    )
    blood = market("Pierwsza krew - mapa 1", sel("Natus Vincere", "1.8", "x1"), ext="X")
    ev = standard_event()
    ev.markets += [ladder, blood]
    run(session, settings, mapper, [ev], NOW)
    rows = {(m.raw_name, m.spec_key): m for m in session.scalars(select(BookmakerMarket))}
    winner = rows[("Zwycięzca meczu", "match_winner")]
    assert winner.extra == {"betclic_market_id": "m1", "rule": "match_winner"}
    for key in ("total_rounds|map=1|line=21.5", "total_rounds|map=1|line=23.5"):
        split = rows[("Liczba rund - mapa 1", key)]
        assert split.external_id.startswith("80021~") and split.extra["betclic_market_id"] == "80021"
        assert split.extra["rule"] == "total_rounds"  # the rule that mapped it, not the first that fits
        assert split.extra["split_signature"].startswith("total_rounds|map=1|")
    unmapped = rows[("Pierwsza krew - mapa 1", None)]
    assert unmapped.extra == {"betclic_market_id": "X"}
    # unchanged on the next pass (no JSON rewrite, same content)
    run(session, settings, mapper, [ev], NOW + timedelta(minutes=3))
    session.refresh(winner)
    assert winner.extra == {"betclic_market_id": "m1", "rule": "match_winner"}


def test_settlement_settings_decide_whether_regulation_markets_are_mapped(
    session, settings, mapper, linked_match
) -> None:
    """BetclicSettings.totals_include_overtime / h2h_tie_rule are honoured on every pass."""
    regulation = market(
        "Liczba rund - mapa 1 (bez dogrywki)",
        sel("Powyżej 21,5", "1.85", "r1"),
        sel("Poniżej 21,5", "1.95", "r2"),
        ext="R",
    )
    ev = event("1001", starts_at=T0, markets=[regulation])
    assert settings.betclic.totals_include_overtime is True
    report = run(session, settings, mapper, [ev], NOW)
    row = session.scalar(select(BookmakerMarket))
    assert report.markets_mapped == 0 and row.is_mapped is False and "rule" not in row.extra
    no_overtime = settings.model_copy(
        update={"betclic": settings.betclic.model_copy(update={"totals_include_overtime": False})}
    )
    report = run(session, no_overtime, mapper, [ev], NOW + timedelta(minutes=3))
    session.refresh(row)
    assert report.markets_mapped == 1 and row.spec_key == "total_rounds|map=1|line=21.5"
    assert mapper.settlement.totals_include_overtime is True  # the caller's mapper is not mutated
    # remap_event binds the same way when given settings
    row_event = session.scalar(select(BookmakerEvent))
    assert remap_event(session, row_event, mapper, settings=settings) == 2
    session.refresh(row)
    assert row.is_mapped is False


def test_three_way_match_winner_is_only_mapped_for_a_bo2(session, settings, mapper, linked_match) -> None:
    from cs2bot.domain.enums import MatchFormat

    three_way = market(
        "Wynik meczu",
        sel("Natus Vincere", "1.90", "w1"),
        sel("Remis", "3.40", "wx"),
        sel("FaZe Clan", "2.60", "w2"),
        ext="W",
    )
    ev = event("1001", starts_at=T0, markets=[three_way])
    linked_match.format = MatchFormat.BO3
    session.flush()
    report = run(session, settings, mapper, [ev], NOW)
    row = session.scalar(select(BookmakerMarket))
    assert report.markets_mapped == 0 and row.is_mapped is False  # "1 X 2" in a BO3: regulation market
    assert all(not s.is_mapped for s in row.selections)
    linked_match.format = MatchFormat.BO2
    session.flush()
    report = run(session, settings, mapper, [ev], NOW + timedelta(minutes=3))
    session.refresh(row)
    assert row.spec_key == "match_winner"
    assert {s.selection_key for s in row.selections} == {"team1", "draw", "team2"}
    # format corrected to BO3 while only part of the market is visible: nothing keeps a stale key
    linked_match.format = MatchFormat.BO3
    session.flush()
    part = event("1001", starts_at=T0, markets=[market("Wynik meczu", *three_way.selections[:2], ext="W")])
    run(session, settings, mapper, [part], NOW + timedelta(minutes=6), partial_event_ids={"1001"})
    session.refresh(row)
    assert row.is_mapped is False
    assert all(s.selection_key is None and not s.is_mapped for s in row.selections)
    # remap_event applies the same rule
    assert remap_event(session, session.scalar(select(BookmakerEvent)), mapper) == 0


def test_sport_label_is_stored_and_other_esports_are_never_linked(session, settings, mapper) -> None:
    g2, fnatic = make_team(session, "G2"), make_team(session, "Fnatic")
    match = make_match(session, g2, fnatic, T0)
    winner = market("Zwycięzca meczu", sel("G2", "1.5", "g1"), sel("Fnatic", "2.6", "g2"), ext="G")
    lol = event("555", "G2", "Fnatic", starts_at=T0, markets=[winner], competition="LEC Winter")
    lol.sport_raw = "League of Legends"
    cs = event("556", "G2", "Fnatic", starts_at=T0, markets=[winner], competition="IEM Chengdu")
    cs.sport_raw = "Counter-Strike 2"
    report = run(session, settings, mapper, [lol, cs], NOW)
    rows = {e.external_id: e for e in session.scalars(select(BookmakerEvent))}
    assert rows["555"].extra["sport_raw"] == "League of Legends"
    assert rows["555"].link_status is LinkStatus.UNLINKED and rows["555"].match_id is None
    assert rows["556"].link_status is LinkStatus.AUTO and rows["556"].match_id == match.id
    assert report.link_status_counts == {"unlinked": 1, "auto": 1}


def test_failed_event_leaves_no_partial_rows_and_later_events_are_stored(
    session, settings, mapper, linked_match
) -> None:
    """Each event runs in a SAVEPOINT: a failure in the middle of an event rolls back that event only."""

    bad_market = market("Zwycięzca meczu", sel("Alpha", "1.5", "b1"), ext="B")
    bad = event("2002", "Alpha", "Bravo", starts_at=T0, markets=[bad_market])
    original = BookmakerSelection.__init__

    def failing_init(self, *args, **kwargs):
        if kwargs.get("external_id") == "b1":
            raise RuntimeError("db exploded")
        original(self, *args, **kwargs)

    BookmakerSelection.__init__ = failing_init  # type: ignore[method-assign]
    try:
        report = run(session, settings, mapper, [bad, standard_event()], NOW)
    finally:
        BookmakerSelection.__init__ = original  # type: ignore[method-assign]
    assert report.events_failed == 1 and "2002" in report.errors[0]
    assert [e.external_id for e in session.scalars(select(BookmakerEvent))] == ["1001"]
    assert count(session, BookmakerMarket) == 3 and count(session, BookmakerSelection) == 6


def test_page_name_and_group_of_prefixed_markets_are_stored(session, settings, mapper, linked_match) -> None:
    """ "Mapa 1 - Liczba rund" is built by the parser; the page only shows "Liczba rund" under "Mapa 1"."""
    rounds = market(
        "Mapa 1 - Liczba rund", sel("Powyżej 21,5", "1.85", "a"), sel("Poniżej 21,5", "1.95", "b"), ext="2"
    )

    class Source(StaticOddsSource):
        def last_fetch_info(self) -> FetchInfo:
            meta = {("Mapa 1 - Liczba rund", "2"): {"page_name": "Liczba rund", "group": "Mapa 1"}}
            return FetchInfo(market_meta_by_event={"1001": meta})

    ev = event("1001", starts_at=T0, markets=[rounds])
    collect_once(session, Source([ev]), mapper, settings=settings, now=NOW)
    row = session.scalar(select(BookmakerMarket))
    assert row.spec_key == "total_rounds|map=1|line=21.5"
    assert row.extra == {
        "betclic_market_id": "2",
        "rule": "total_rounds",
        "page_name": "Liczba rund",
        "group": "Mapa 1",
    }


def test_duplicate_events_keep_the_latest_capture_time_and_sport(
    session, settings, mapper, linked_match
) -> None:
    older, newer = datetime.now(UTC) - timedelta(minutes=9), datetime.now(UTC) - timedelta(minutes=1)
    a = event("1001", starts_at=T0, markets=[market("Zwycięzca meczu", sel("Natus Vincere", "1.6", "s1"))])
    b = event("1001", starts_at=T0, markets=[market("Zwycięzca meczu", sel("FaZe Clan", "2.3", "s2"))])
    a.captured_at, b.captured_at, b.sport_raw = older, newer, "Counter-Strike 2"
    collect_once(session, StaticOddsSource([a, b]), mapper, settings=settings)
    row = session.scalar(select(BookmakerEvent))
    assert row.last_seen_at == newer and row.extra["sport_raw"] == "Counter-Strike 2"


def test_malformed_selections_are_dropped_without_failing_the_event(
    session, settings, mapper, linked_match
) -> None:
    ev = standard_event()
    ev.markets[0].selections += [
        sel("Remis", "0", "bad1"),
        sel("Ktoś", "1.00", "bad2"),  # not a price
        sel("Inny", "250000", "bad3"),  # would overflow Numeric(8, 3)
        sel("  ", "1.50", "bad4"),
    ]
    ev.markets.append(market("   ", sel("Tak", "1.5", "bad5"), ext="E"))
    ev.markets.append(market("Pusty rynek", ext="E2"))
    report = run(session, settings, mapper, [ev], NOW)
    assert report.errors == [] and report.events_failed == 0
    assert report.selections_invalid == 4 and report.selections_new == 6 and report.markets_new == 3
    winner = session.scalar(select(BookmakerMarket).where(BookmakerMarket.raw_name == "Zwycięzca meczu"))
    assert winner.is_mapped and {s.external_id for s in winner.selections} == {"s1", "s2"}
