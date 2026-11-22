"""Parser for the match overview page."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from cs2bot.domain.enums import MatchFormat, MatchStatus, Side, TeamSlot, VetoAction
from cs2bot.ingest.hltv import HLTVMatchStatus, HLTVParseError, MapState, parse_match_page
from cs2bot.ingest.hltv.parsers import match as match_module
from tests.ingest.hltv.conftest import load_fixture

URL = "https://www.hltv.org/matches/{id}/x"


def _parse(name: str, match_id: int):
    return parse_match_page(load_fixture(name), URL.format(id=match_id))


def test_finished_bo3_header_and_result() -> None:
    page = _parse("match_bo3_finished.html", 2378001)
    assert page.hltv_match_id == 2378001
    assert page.url == "https://www.hltv.org/matches/2378001/vitality-vs-natus-vincere-iem-cologne-2026"
    assert page.team1 is not None and (page.team1.name, page.team1.hltv_id) == ("Vitality", 9565)
    assert page.team2 is not None and (page.team2.name, page.team2.hltv_id) == ("Natus Vincere", 4608)
    assert page.scheduled_at == datetime(2026, 11, 21, 16, 30, tzinfo=UTC)
    assert page.status is HLTVMatchStatus.FINISHED and page.status.to_match_status() is MatchStatus.FINISHED
    assert page.best_of == 3 and page.format is MatchFormat.BO3 and page.is_lan is True
    assert page.event is not None and (page.event.name, page.event.hltv_id) == ("IEM Cologne 2026", 8001)
    assert page.stage == "Playoffs - Semi-final"
    assert (page.team1_score, page.team2_score, page.winner) == (2, 1, TeamSlot.TEAM1)
    assert (page.team1_rank, page.team2_rank, page.rank_date) == (1, 3, date(2026, 11, 16))
    assert not page.is_forfeit and page.warnings == []


def test_finished_bo3_maps_halves_and_picks() -> None:
    page = _parse("match_bo3_finished.html", 2378001)
    assert [m.map_name for m in page.maps] == ["mirage", "inferno", "anubis"]
    mirage, inferno, anubis = page.maps
    assert mirage.state is MapState.PLAYED and (mirage.team1_rounds, mirage.team2_rounds) == (11, 13)
    assert mirage.winner is TeamSlot.TEAM2 and mirage.team1_start_side is Side.CT
    assert mirage.mapstats_id == 210001 and mirage.picked_by is TeamSlot.TEAM2 and not mirage.is_decider
    assert mirage.regulation_side_rounds() == {
        (TeamSlot.TEAM1, Side.CT): 5,
        (TeamSlot.TEAM1, Side.T): 6,
        (TeamSlot.TEAM2, Side.CT): 6,
        (TeamSlot.TEAM2, Side.T): 7,
    }
    assert inferno.picked_by is TeamSlot.TEAM1 and inferno.team1_start_side is Side.T
    # decider with two overtimes: regulation halves carry sides, the OT group does not
    assert anubis.is_decider and anubis.picked_by is None
    assert (anubis.team1_rounds, anubis.team2_rounds, anubis.overtime_rounds) == (19, 17, 12)
    assert [(h.team1_rounds, h.team2_rounds, h.is_overtime) for h in anubis.halves] == [
        (7, 5, False),
        (5, 7, False),
        (7, 5, True),
    ]
    assert anubis.halves[2].team1_side is None


def test_finished_bo3_veto_lineups_demos() -> None:
    page = _parse("match_bo3_finished.html", 2378001)
    assert [(v.step, v.action, v.team, v.map_name) for v in page.vetoes] == [
        (1, VetoAction.BAN, TeamSlot.TEAM2, "nuke"),
        (2, VetoAction.BAN, TeamSlot.TEAM1, "ancient"),
        (3, VetoAction.PICK, TeamSlot.TEAM2, "mirage"),
        (4, VetoAction.PICK, TeamSlot.TEAM1, "inferno"),
        (5, VetoAction.BAN, TeamSlot.TEAM2, "dust2"),
        (6, VetoAction.BAN, TeamSlot.TEAM1, "train"),
        (7, VetoAction.LEFTOVER, None, "anubis"),
    ]
    assert [p.nickname for p in page.team1_lineup] == ["apEX", "ZywOo", "flameZ", "mezii", "ropz"]
    zywoo = page.team1_lineup[1]
    assert zywoo.hltv_id == 11893 and zywoo.real_name == "Mathieu Herbaut" and zywoo.country == "France"
    assert not any(p.is_standin for p in page.team1_lineup + page.team2_lineup)
    assert [p.hltv_id for p in page.team2_lineup] == [9816, 19230, 18987, 19206, 20424]
    assert [(d.hltv_demo_id, d.map_name) for d in page.demos] == [
        (96001, "mirage"),
        (96002, "inferno"),
        (96003, "anubis"),
    ]
    assert page.demo_link is not None and page.demo_link.url == "https://www.hltv.org/download/demo/96001"


def test_bo1_overtime_online() -> None:
    page = _parse("match_bo1_finished.html", 2378002)
    assert page.format is MatchFormat.BO1 and page.is_lan is False
    (nuke,) = page.maps
    assert nuke.map_name == "nuke" and nuke.is_decider and nuke.picked_by is None
    assert (nuke.team1_rounds, nuke.team2_rounds, nuke.overtime_rounds) == (16, 14, 6)
    assert nuke.team1_start_side is Side.T
    assert nuke.halves[-1].is_overtime and (nuke.halves[-1].team1_rounds, nuke.halves[-1].team2_rounds) == (
        4,
        2,
    )
    assert [v.action for v in page.vetoes].count(VetoAction.BAN) == 6
    assert [(d.hltv_demo_id, d.map_name) for d in page.demos] == [(96010, "nuke")]  # data-demo-link


def test_upcoming_without_veto() -> None:
    page = _parse("match_upcoming.html", 2378010)
    assert page.status is HLTVMatchStatus.SCHEDULED and page.status_text == "1 : 02 : 11 : 47"
    assert page.vetoes == [] and page.demos == []
    assert all(m.state is MapState.UNPLAYED and m.map_name == "tba" for m in page.maps)
    assert page.played_maps == []
    assert len(page.team1_lineup) == 5 and len(page.team2_lineup) == 5
    assert page.team1_score is None and page.winner is None


def test_upcoming_with_veto_and_standin() -> None:
    page = _parse("match_upcoming_veto.html", 2378011)
    assert len(page.vetoes) == 7
    assert [(m.map_name, m.state, m.picked_by, m.is_decider) for m in page.maps] == [
        ("inferno", MapState.UNPLAYED, TeamSlot.TEAM2, False),
        ("mirage", MapState.UNPLAYED, TeamSlot.TEAM1, False),
        ("dust2", MapState.UNPLAYED, None, True),
    ]
    standins = [p.nickname for p in page.team2_lineup if p.is_standin]
    assert standins == ["molodoy"]


def test_postponed() -> None:
    page = _parse("match_postponed.html", 2378020)
    assert page.status is HLTVMatchStatus.POSTPONED and page.status.to_match_status() is MatchStatus.POSTPONED
    assert page.played_maps == [] and page.vetoes == []


def test_full_forfeit() -> None:
    page = _parse("match_forfeit.html", 2378030)
    assert page.status is HLTVMatchStatus.DELETED
    assert page.status.to_match_status() is MatchStatus.CANCELED
    assert page.is_forfeit and page.forfeit_note is not None and "forfeit" in page.forfeit_note
    (default,) = page.maps
    assert default.state is MapState.FORFEIT and default.map_name == "default"
    assert page.winner is TeamSlot.TEAM2  # carried by the "Default" map (no won/lost header)
    assert page.team1_score is None and page.played_maps == []
    assert len(page.vetoes) == 7 and page.demos == []
    assert page.team2_rank is None  # unranked team


def test_bo5_partial_forfeit() -> None:
    page = _parse("match_bo5_partial_forfeit.html", 2378040)
    assert page.format is MatchFormat.BO5 and page.status is HLTVMatchStatus.FINISHED
    assert [m.state for m in page.maps] == [
        MapState.PLAYED,
        MapState.PLAYED,
        MapState.FORFEIT,
        MapState.UNPLAYED,
        MapState.UNPLAYED,
    ]
    assert (page.team1_score, page.team2_score, page.winner) == (3, 0, TeamSlot.TEAM1)
    assert page.is_forfeit and page.forfeit_note is not None and "3rd map" in page.forfeit_note
    assert page.maps[4].is_decider and page.maps[3].picked_by is TeamSlot.TEAM1


def test_veto_team_names_resolved_by_alias() -> None:
    html = load_fixture("match_bo3_finished.html").replace(
        "1. Natus Vincere removed Nuke", "1. NAVI removed Nuke"
    )
    page = parse_match_page(html, URL.format(id=2378001))
    assert page.vetoes[0].team is TeamSlot.TEAM2 and page.vetoes[0].team_name == "NAVI"


def test_unknown_veto_team_is_a_warning() -> None:
    html = load_fixture("match_bo3_finished.html").replace(
        "2. Vitality removed Ancient", "2. Somebody removed Ancient"
    )
    page = parse_match_page(html, URL.format(id=2378001))
    assert page.vetoes[1].team is None and any("veto step 2" in w for w in page.warnings)


def test_lineups_without_data_attributes_fall_back_to_links() -> None:
    html = load_fixture("match_bo3_finished.html").replace("data-player-id=", "data-unknown=")
    page = parse_match_page(html, URL.format(id=2378001))
    assert [p.hltv_id for p in page.team1_lineup] == [7322, 11893, 20113, 18462, 11816]
    assert page.team1_lineup[0].nickname == "apEX"


def test_broken_section_becomes_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("layout changed")

    monkeypatch.setattr(match_module, "_parse_lineups", boom)
    page = _parse("match_bo3_finished.html", 2378001)
    assert page.team1_lineup == [] and len(page.maps) == 3 and len(page.vetoes) == 7
    assert any("lineups section could not be parsed" in w for w in page.warnings)


def test_not_a_match_page_raises() -> None:
    with pytest.raises(HLTVParseError):
        parse_match_page(load_fixture("challenge_just_a_moment.html"), URL.format(id=1))
    with pytest.raises(HLTVParseError):
        parse_match_page("<html></html>", "https://www.hltv.org/results")


def test_half_score_text_fallback() -> None:
    html = load_fixture("match_bo1_finished.html")
    start = html.index('<div class="results-center-half-score">')
    end = html.index("</div>", start)
    html = html[:start] + '<div class="results-center-half-score">(5:7; 7:5) (4:2)' + html[end:]
    (nuke,) = parse_match_page(html, URL.format(id=2378002)).maps
    assert [(h.team1_rounds, h.team2_rounds, h.is_overtime) for h in nuke.halves] == [
        (5, 7, False),
        (7, 5, False),
        (4, 2, True),
    ]
    assert nuke.team1_start_side is None  # sides are unknown without the span classes
