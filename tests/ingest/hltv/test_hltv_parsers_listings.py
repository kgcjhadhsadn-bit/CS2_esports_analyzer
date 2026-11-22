"""Parsers for /results and /matches."""

from __future__ import annotations

from datetime import UTC, datetime

from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.ingest.hltv import parse_matches_page, parse_results_page, parse_results_pagination
from tests.ingest.hltv.conftest import load_fixture


def test_results_page_rows_skip_featured_duplicates() -> None:
    rows = parse_results_page(load_fixture("results_offset0.html"))
    ids = [r.hltv_match_id for r in rows]
    # the "big results" block on page 1 duplicates two regular rows; each match appears once
    assert ids == [2378001, 2378002, 2378100, 2378030, 2378101, 2378040, 2378102]
    bo3 = rows[0]
    assert bo3.team1.name == "Vitality" and bo3.team2.name == "Natus Vincere"
    assert bo3.team1.hltv_id is None  # the results page does not link teams
    assert (bo3.team1_score, bo3.team2_score, bo3.winner) == (2, 1, TeamSlot.TEAM1)
    assert bo3.format is MatchFormat.BO3 and bo3.map_name is None and not bo3.is_forfeit
    assert bo3.stars == 3
    assert bo3.event is not None and bo3.event.name == "IEM Cologne 2026"
    assert bo3.played_at == datetime.fromtimestamp(1795290000, tz=UTC)
    assert bo3.url == "https://www.hltv.org/matches/2378001/vitality-vs-natus-vincere-iem-cologne-2026"


def test_results_page_bo1_forfeit_and_bo5_hints() -> None:
    rows = {r.hltv_match_id: r for r in parse_results_page(load_fixture("results_offset0.html"))}
    bo1 = rows[2378002]
    assert bo1.format is MatchFormat.BO1 and bo1.map_name == "nuke" and bo1.format_hint == "nuke"
    anubis = rows[2378101]
    assert anubis.map_name == "anubis"  # "anb" slug
    forfeit = rows[2378030]
    assert forfeit.is_forfeit and forfeit.format is None and forfeit.winner is TeamSlot.TEAM2
    assert rows[2378040].format is MatchFormat.BO5 and rows[2378040].stars == 1
    assert rows[2378100].stars == 0


def test_results_pagination() -> None:
    page1 = parse_results_pagination(load_fixture("results_offset0.html"))
    assert page1 is not None
    assert (page1.start, page1.end, page1.total) == (1, 100, 115045)
    assert page1.has_next and page1.next_url == "https://www.hltv.org/results?offset=100"
    assert parse_results_pagination("<html><body>nothing</body></html>") is None


def test_results_malformed_row_is_skipped_not_fatal() -> None:
    html = load_fixture("results_offset0.html")
    broken = html.replace('<div class="team team-won">Kubix</div>', "", 1)  # row without team1
    broken = broken.replace('href="/matches/2378101/', 'href="/matches/not-a-number/', 1)
    ids = [r.hltv_match_id for r in parse_results_page(broken)]
    assert 2378100 not in ids and 2378101 not in ids
    assert {2378001, 2378002, 2378030, 2378040, 2378102} <= set(ids)


def test_results_date_falls_back_to_day_headline() -> None:
    html = load_fixture("results_offset0.html").replace('data-zonedgrouping-entry-unix="1795125000000"', "")
    row = next(r for r in parse_results_page(html) if r.hltv_match_id == 2378040)
    assert row.played_at == datetime(2026, 11, 19, tzinfo=UTC)


def test_matches_page_2025_layout() -> None:
    rows = {r.hltv_match_id: r for r in parse_matches_page(load_fixture("matches_upcoming.html"))}
    assert set(rows) == {2378050, 2378011, 2378061, 2378010, 2378062, 2378063, 2378020}
    live = rows[2378050]
    assert live.is_live and live.scheduled_at is None and live.stars == 3
    g2 = rows[2378010]
    assert g2.team1 is not None and g2.team1.name == "G2" and g2.team1.hltv_id == 5995
    assert g2.team2 is not None and g2.team2.name == "Spirit" and g2.team2.hltv_id == 7020
    assert g2.scheduled_at == datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
    assert g2.format is MatchFormat.BO3 and g2.stars == 2 and g2.is_lan is True and not g2.is_live
    assert (
        g2.event is not None
        and g2.event.hltv_id == 8010
        and g2.event.name == "StarLadder Budapest Major 2026"
    )
    assert g2.url == "https://www.hltv.org/matches/2378010/g2-vs-spirit-starladder-budapest-major-2026"
    tbd = rows[2378061]
    assert not tbd.has_teams and tbd.title == "CCT Season 3 Europe Series 12 - Lower bracket"
    half = rows[2378063]
    assert half.team1 is not None and half.team2 is None and not half.has_teams  # "TBD" placeholder
    assert rows[2378062].is_lan is False


def test_matches_page_legacy_layout() -> None:
    rows = {r.hltv_match_id: r for r in parse_matches_page(load_fixture("matches_legacy.html"))}
    assert set(rows) == {2378050, 2378010, 2378064}
    live = rows[2378050]
    assert live.is_live and live.stars == 3
    assert live.team1 is not None and live.team1.hltv_id == 9565 and live.team1.name == "Vitality"
    g2 = rows[2378010]
    assert g2.stars == 2 and g2.scheduled_at == datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
    assert g2.team2 is not None and g2.team2.name == "Spirit" and g2.team2.hltv_id == 7020
    assert g2.format is MatchFormat.BO3
    empty = rows[2378064]
    assert not empty.has_teams and empty.title == "BLAST Premier World Final 2026 - Grand final"
    assert empty.format is MatchFormat.BO5


def test_matches_page_ignores_garbage() -> None:
    assert parse_matches_page("<html><body><div class='match-wrapper'>no id</div></body></html>") == []
