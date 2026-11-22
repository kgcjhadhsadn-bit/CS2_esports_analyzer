"""Parser for map stats pages: scoreboards (incl. per-side), side split, round history."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cs2bot.domain.enums import RoundEndReason, Side, TeamSlot
from cs2bot.ingest.hltv import HLTVParseError, parse_mapstats_page
from tests.ingest.hltv.conftest import load_fixture

URL = "https://www.hltv.org/stats/matches/mapstatsid/{id}/x"


def _parse(mapstats_id: int, html: str | None = None):
    return parse_mapstats_page(
        html or load_fixture(f"mapstats_{mapstats_id}.html"), URL.format(id=mapstats_id)
    )


def test_regulation_map_header_and_side_split() -> None:
    page = _parse(210001)
    assert page.mapstats_id == 210001 and page.hltv_match_id == 2378001
    assert page.map_name == "mirage" and page.raw_map_name == "Mirage"
    assert page.played_at == datetime(2026, 11, 21, 16, 30, tzinfo=UTC)
    assert page.event is not None and (page.event.name, page.event.hltv_id) == ("IEM Cologne 2026", 8001)
    assert page.rating_version == "3.0"
    t1, t2 = page.team1, page.team2
    assert (t1.team.name, t1.team.hltv_id, t1.rounds, t1.won) == ("Vitality", 9565, 11, False)
    assert (t2.team.name, t2.team.hltv_id, t2.rounds, t2.won) == ("Natus Vincere", 4608, 13, True)
    assert t1.start_side is Side.CT and t2.start_side is Side.T
    assert (t1.ct_rounds, t1.t_rounds, t2.ct_rounds, t2.t_rounds) == (5, 6, 6, 7)
    assert page.side_split_complete and page.rounds_complete and page.warnings == []
    assert page.winner is TeamSlot.TEAM2 and page.total_rounds == 24 and page.overtime_rounds == 0
    assert t1.team_rating is not None and t1.first_kills is not None and t1.clutches_won is not None


def test_scoreboard_uses_traditional_columns_and_side_tables() -> None:
    page = _parse(210001)
    assert len(page.team1.players) == 5 and len(page.team2.players) == 5
    apex = page.team1.players[0]
    assert (apex.nickname, apex.hltv_id, apex.country) == ("apEX", 7322, "France")
    # traditional values, never the hidden eco-adjusted twins (fixture eco kills = kills + 3.4)
    assert (apex.kills, apex.headshots, apex.assists, apex.flash_assists) == (19, 9, 6, 1)
    assert (apex.deaths, apex.traded_deaths) == (17, 3)
    assert apex.kast == pytest.approx(67.6) and apex.adr == pytest.approx(104.9)
    assert apex.rating == pytest.approx(1.61) and apex.round_swing == pytest.approx(3.37)
    assert (apex.first_kills, apex.first_deaths, apex.fk_diff, apex.kd_diff) == (2, 1, 1, 2)
    assert apex.multi_kill_rounds == 1 and apex.clutches_won == 2
    ct = {p.hltv_id: p for p in page.team1.ct_players}
    t = {p.hltv_id: p for p in page.team1.t_players}
    for line in page.team1.players:
        assert ct[line.hltv_id].kills + t[line.hltv_id].kills == line.kills
        assert ct[line.hltv_id].deaths + t[line.hltv_id].deaths == line.deaths


def test_round_history_regulation() -> None:
    page = _parse(210001)
    assert len(page.rounds) == 24
    assert [r.round_number for r in page.rounds] == list(range(1, 25))
    assert sum(r.winner is TeamSlot.TEAM1 for r in page.rounds) == 11
    first = page.rounds[0]
    assert first.winner is TeamSlot.TEAM2 and first.winner_side is Side.T and first.team1_side is Side.CT
    assert first.end_reason is RoundEndReason.BOMB_EXPLODED and (first.team1_score, first.team2_score) == (
        0,
        1,
    )
    assert all(r.team1_side is Side.CT for r in page.rounds[:12])
    assert all(r.team1_side is Side.T for r in page.rounds[12:])
    assert {r.end_reason for r in page.rounds} <= {
        RoundEndReason.ELIMINATION,
        RoundEndReason.BOMB_EXPLODED,
        RoundEndReason.BOMB_DEFUSED,
        RoundEndReason.TIME_EXPIRED,
    }
    last = page.rounds[-1]
    assert (last.team1_score, last.team2_score) == (11, 13) and not last.is_overtime


def test_left_team_can_be_either_side_of_the_match() -> None:
    page = _parse(210002)  # rendered with Natus Vincere on the left
    assert page.team1.team.name == "Natus Vincere" and page.team2.team.name == "Vitality"
    assert (page.team1.rounds, page.team2.rounds) == (7, 13)
    assert page.team2.start_side is Side.T and (page.team2.t_rounds, page.team2.ct_rounds) == (8, 5)


def test_extended_overtime_two_containers() -> None:
    page = _parse(210003)
    assert len(page.rounds) == 36 and page.rounds_complete
    assert sum(r.is_overtime for r in page.rounds) == 12
    t1, t2 = page.team1, page.team2
    assert (t1.overtime_rounds, t2.overtime_rounds) == (7, 5)
    # side split from the round icons includes the 12 overtime rounds
    assert page.side_split_complete
    assert (t1.ct_rounds, t1.t_rounds, t2.ct_rounds, t2.t_rounds) == (10, 9, 9, 8)
    assert t1.ct_rounds + t1.t_rounds == 19 and t2.ct_rounds + t2.t_rounds == 17
    # teams keep their side into overtime (rounds 25-27 like 13-24), then switch every 3 rounds
    assert page.rounds[24].team1_side is page.rounds[23].team1_side
    assert page.rounds[27].team1_side is not page.rounds[26].team1_side


def test_single_overtime_inline_and_old_scoreboard_layout() -> None:
    page = _parse(210010)
    assert page.map_name == "nuke" and page.rating_version == "2.0"
    assert len(page.rounds) == 30 and page.rounds_complete and page.rounds[-1].is_overtime
    assert (page.team1.overtime_rounds, page.team2.overtime_rounds) == (4, 2)
    assert page.team1.start_side is Side.T
    brollan = page.team1.players[0]
    assert (brollan.kills, brollan.headshots, brollan.deaths) == (27, 15, 19)
    assert brollan.kast == pytest.approx(75.8)  # old layout: KAST in the st-kdratio column
    assert brollan.kd_diff == 8 and brollan.fk_diff == -2 and brollan.round_swing is None
    assert page.team1.ct_players == [] and page.team1.t_players == []


def test_missing_round_history_falls_back_to_breakdown() -> None:
    html = load_fixture("mapstats_210001.html").replace("round-history-con", "round-history-gone")
    page = _parse(210001, html)
    assert page.rounds == [] and not page.rounds_complete
    assert page.team1.start_side is Side.CT and (page.team1.ct_rounds, page.team1.t_rounds) == (5, 6)
    assert page.side_split_complete  # no overtime: the regulation breakdown covers every round


def test_overtime_without_round_history_is_not_a_complete_split() -> None:
    html = load_fixture("mapstats_210003.html").replace("round-history-con", "round-history-gone")
    page = _parse(210003, html)
    assert not page.side_split_complete
    assert (page.team1.ct_rounds, page.team1.t_rounds) == (7, 5)  # regulation only
    assert (page.team1.overtime_rounds, page.team2.overtime_rounds) == (7, 5)


def test_inconsistent_round_history_is_flagged() -> None:
    html = load_fixture("mapstats_210001.html").replace(
        '<div class="bold won">13</div>', '<div class="bold won">14</div>'
    )
    page = _parse(210001, html)
    assert not page.rounds_complete
    assert any("does not add up" in w for w in page.warnings)


def test_broken_scoreboard_keeps_the_rest() -> None:
    html = load_fixture("mapstats_210001.html").replace("stats-table", "stats-tab1e")
    page = _parse(210001, html)
    assert page.team1.players == [] and page.rounds_complete and page.team1.rounds == 11


def test_not_a_mapstats_page_raises() -> None:
    with pytest.raises(HLTVParseError):
        _parse(210001, load_fixture("match_bo3_finished.html"))
    with pytest.raises(HLTVParseError):
        parse_mapstats_page("<html></html>", "https://www.hltv.org/stats")
