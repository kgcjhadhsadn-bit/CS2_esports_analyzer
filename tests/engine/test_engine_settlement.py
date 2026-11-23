"""Settlement: every market type on hand-built results, refusal to guess, and consistency with pricing."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy.orm import Session

from cs2bot.config import BetclicSettings
from cs2bot.domain.enums import BetStatus, MatchFormat, TeamSlot
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.predictions import MatchPrediction
from cs2bot.domain.results import MapActual, MatchActuals, SettlementOutcome
from cs2bot.engine import pricing
from cs2bot.engine.settlement import (
    Resolution,
    bet_status_for,
    explain_outcome,
    resolve_bet,
    resolve_outcome,
    spec_from_key,
)
from tests.engine.engine_factories import add_bet, add_match
from tests.engine.engine_series import (
    PLAYER_TEAMS,
    RACE_TARGETS,
    TEAM1_IDS,
    TEAM2_IDS,
    empirical_prediction,
    simulate_series,
)

MT = MarketType
WON, LOST, VOID, PUSH, UNKNOWN = (
    SettlementOutcome.WON,
    SettlementOutcome.LOST,
    SettlementOutcome.VOID,
    SettlementOutcome.PUSH,
    SettlementOutcome.UNKNOWN,
)
T1, T2 = TeamSlot.TEAM1, TeamSlot.TEAM2
RULES = {"include_overtime_in_totals": True, "h2h_tie": "push"}


def history(*chunks: tuple[TeamSlot, int]) -> tuple[TeamSlot, ...]:
    out: list[TeamSlot] = []
    for team, count in chunks:
        out.extend([team] * count)
    return tuple(out)


def kills(first: int, **overrides: int) -> dict[int, int]:
    """Kills for all ten players: team1 gets first, first-1, ...; team2 two fewer each."""
    table = {pid: first - i for i, pid in enumerate(TEAM1_IDS)}
    table.update({pid: first - 2 - i for i, pid in enumerate(TEAM2_IDS)})
    for key, value in overrides.items():
        table[int(key.removeprefix("p"))] = value
    return table


# map 1: 13-9, team1 wins both pistols, halves 8-4 and 5-5 (a level second half)
MAP1 = MapActual(
    map_number=1,
    map_name="mirage",
    team1_rounds=13,
    team2_rounds=9,
    round_winners=history((T1, 8), (T2, 4), (T1, 2), (T2, 5), (T1, 3)),
    player_kills=kills(22),
)
# map 2: 14-16 after overtime; team2 wins round 1, team1 round 13; halves 5-7 and 7-5
MAP2 = MapActual(
    map_number=2,
    map_name="inferno",
    team1_rounds=14,
    team2_rounds=16,
    round_winners=history((T2, 7), (T1, 5), (T1, 7), (T2, 5), (T1, 2), (T2, 4)),
    player_kills=kills(25, p201=31),
)
# map 3: 13-11 without round history or player statistics
MAP3 = MapActual(map_number=3, map_name="nuke", team1_rounds=13, team2_rounds=11)


def series(fmt: MatchFormat, *maps: MapActual, **kw: bool) -> MatchActuals:
    return MatchActuals(match_id=7, format=fmt, team1_id=1, team2_id=2, maps=maps, **kw)


BO3_FULL = series(MatchFormat.BO3, MAP1, MAP2, MAP3)  # 2-1 for team1
BO3_SWEEP = series(
    MatchFormat.BO3,
    MAP1,
    MAP2.model_copy(update={"team1_rounds": 16, "team2_rounds": 14, "round_winners": None}),
)  # 2-0 for team1, map 3 not played


def spec(market_type: MarketType, **kw: object) -> MarketSpec:
    return MarketSpec(market_type=market_type, **kw)


def settle(market: MarketSpec, selection: str, actuals: MatchActuals, **kw: object) -> SettlementOutcome:
    return resolve_outcome(market, selection, actuals, **{**RULES, **kw})


# ----------------------------------------------------------------------------------------------
# every market type, hand-computed
# ----------------------------------------------------------------------------------------------

CASES: list[tuple[MarketSpec, str, MatchActuals, SettlementOutcome]] = [
    # match level (BO3_FULL: 2-1, three maps)
    (spec(MT.MATCH_WINNER), "team1", BO3_FULL, WON),
    (spec(MT.MATCH_WINNER), "team2", BO3_FULL, LOST),
    (spec(MT.CORRECT_MAP_SCORE), "2-1", BO3_FULL, WON),
    (spec(MT.CORRECT_MAP_SCORE), "1-2", BO3_FULL, LOST),
    (spec(MT.CORRECT_MAP_SCORE), "2-0", BO3_SWEEP, WON),
    (spec(MT.MAP_HANDICAP, line=-1.5), "team1", BO3_FULL, LOST),  # margin +1
    (spec(MT.MAP_HANDICAP, line=-1.5), "team2", BO3_FULL, WON),  # team2 +1.5
    (spec(MT.MAP_HANDICAP, line=-1.5), "team1", BO3_SWEEP, WON),  # margin +2
    (spec(MT.MAP_HANDICAP, line=1.5), "team2", BO3_SWEEP, LOST),
    (spec(MT.MAP_HANDICAP, line=-1.0), "team1", BO3_FULL, PUSH),
    (spec(MT.MAP_HANDICAP, line=-1.0), "team2", BO3_FULL, PUSH),
    (spec(MT.MAP_HANDICAP, line=-2.0), "team1", BO3_SWEEP, PUSH),
    (spec(MT.TOTAL_MAPS, line=2.5), "over", BO3_FULL, WON),
    (spec(MT.TOTAL_MAPS, line=2.5), "under", BO3_FULL, LOST),
    (spec(MT.TOTAL_MAPS, line=2.5), "under", BO3_SWEEP, WON),
    (spec(MT.TOTAL_MAPS, line=3.0), "over", BO3_FULL, PUSH),
    (spec(MT.BOTH_TEAMS_WIN_A_MAP), "yes", BO3_FULL, WON),
    (spec(MT.BOTH_TEAMS_WIN_A_MAP), "no", BO3_FULL, LOST),
    (spec(MT.BOTH_TEAMS_WIN_A_MAP), "no", BO3_SWEEP, WON),
    # map winner; map 3 of a sweep is void
    (spec(MT.MAP_WINNER, map_number=1), "team1", BO3_FULL, WON),
    (spec(MT.MAP_WINNER, map_number=2), "team1", BO3_FULL, LOST),
    (spec(MT.MAP_WINNER, map_number=2), "team2", BO3_FULL, WON),
    (spec(MT.MAP_WINNER, map_number=3), "team2", BO3_FULL, LOST),
    (spec(MT.MAP_WINNER, map_number=3), "team1", BO3_SWEEP, VOID),
    (spec(MT.MAP_WINNER, map_number=3), "team2", BO3_SWEEP, VOID),
    # round handicap (team1 perspective): map 1 margin +4, map 2 margin -2
    (spec(MT.ROUND_HANDICAP, map_number=1, line=-3.5), "team1", BO3_FULL, WON),
    (spec(MT.ROUND_HANDICAP, map_number=1, line=-4.5), "team1", BO3_FULL, LOST),
    (spec(MT.ROUND_HANDICAP, map_number=1, line=-4.5), "team2", BO3_FULL, WON),
    (spec(MT.ROUND_HANDICAP, map_number=1, line=-4.0), "team1", BO3_FULL, PUSH),
    (spec(MT.ROUND_HANDICAP, map_number=1, line=-4.0), "team2", BO3_FULL, PUSH),
    (spec(MT.ROUND_HANDICAP, map_number=2, line=2.5), "team1", BO3_FULL, WON),
    (spec(MT.ROUND_HANDICAP, map_number=2, line=1.5), "team1", BO3_FULL, LOST),
    (spec(MT.ROUND_HANDICAP, map_number=2, line=2.0), "team2", BO3_FULL, PUSH),
    (spec(MT.ROUND_HANDICAP, map_number=3, line=-1.5), "team1", BO3_SWEEP, VOID),
    # total rounds: map 1 = 22, map 2 = 30 (overtime)
    (spec(MT.TOTAL_ROUNDS, map_number=1, line=21.5), "over", BO3_FULL, WON),
    (spec(MT.TOTAL_ROUNDS, map_number=1, line=22.5), "over", BO3_FULL, LOST),
    (spec(MT.TOTAL_ROUNDS, map_number=1, line=22.0), "under", BO3_FULL, PUSH),
    (spec(MT.TOTAL_ROUNDS, map_number=2, line=26.5), "over", BO3_FULL, WON),
    (spec(MT.TOTAL_ROUNDS, map_number=3, line=26.5), "over", BO3_SWEEP, VOID),
    # team totals: map 2 team1 = 14, team2 = 16
    (spec(MT.TEAM_TOTAL_ROUNDS, map_number=2, team="team1", line=13.5), "over", BO3_FULL, WON),
    (spec(MT.TEAM_TOTAL_ROUNDS, map_number=2, team="team2", line=16.5), "over", BO3_FULL, LOST),
    (spec(MT.TEAM_TOTAL_ROUNDS, map_number=2, team="team2", line=16.0), "under", BO3_FULL, PUSH),
    (spec(MT.TEAM_TOTAL_ROUNDS, map_number=1, team="team2", line=9.5), "under", BO3_FULL, WON),
    # odd / even
    (spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=1), "even", BO3_FULL, WON),
    (spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=1), "odd", BO3_FULL, LOST),
    (spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=3), "odd", BO3_SWEEP, VOID),
    # map correct score / overtime
    (spec(MT.MAP_CORRECT_SCORE, map_number=1), "13-9", BO3_FULL, WON),
    (spec(MT.MAP_CORRECT_SCORE, map_number=1), "9-13", BO3_FULL, LOST),
    (spec(MT.MAP_CORRECT_SCORE, map_number=2), "14-16", BO3_FULL, WON),
    (spec(MT.MAP_CORRECT_SCORE, map_number=3), "13-11", BO3_SWEEP, VOID),
    (spec(MT.MAP_OVERTIME, map_number=1), "no", BO3_FULL, WON),
    (spec(MT.MAP_OVERTIME, map_number=2), "yes", BO3_FULL, WON),
    (spec(MT.MAP_OVERTIME, map_number=2), "no", BO3_FULL, LOST),
    (spec(MT.MAP_OVERTIME, map_number=3), "yes", BO3_SWEEP, VOID),
    # pistol rounds
    (spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=1), "team1", BO3_FULL, WON),
    (spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=13), "team2", BO3_FULL, LOST),
    (spec(MT.PISTOL_ROUND_WINNER, map_number=2, round_number=1), "team1", BO3_FULL, LOST),
    (spec(MT.PISTOL_ROUND_WINNER, map_number=2, round_number=13), "team1", BO3_FULL, WON),
    (spec(MT.PISTOL_ROUND_WINNER, map_number=3, round_number=1), "team1", BO3_SWEEP, VOID),
    # race to N: map 1 team1 is first to 5 and to 13; map 2 team2 is first to 5 and 16, team1 to 12
    (spec(MT.RACE_TO_ROUNDS, map_number=1, line=5.0), "team1", BO3_FULL, WON),
    (spec(MT.RACE_TO_ROUNDS, map_number=1, line=5.0), "team2", BO3_FULL, LOST),
    (spec(MT.RACE_TO_ROUNDS, map_number=1, line=13.0), "team1", BO3_FULL, WON),
    (spec(MT.RACE_TO_ROUNDS, map_number=1, line=14.0), "team1", BO3_FULL, VOID),
    (spec(MT.RACE_TO_ROUNDS, map_number=2, line=5.0), "team2", BO3_FULL, WON),
    (spec(MT.RACE_TO_ROUNDS, map_number=2, line=12.0), "team1", BO3_FULL, WON),
    (spec(MT.RACE_TO_ROUNDS, map_number=2, line=16.0), "team2", BO3_FULL, WON),
    (spec(MT.RACE_TO_ROUNDS, map_number=2, line=17.0), "team2", BO3_FULL, VOID),
    (spec(MT.RACE_TO_ROUNDS, map_number=3, line=13.0), "team1", BO3_FULL, WON),  # decided by the score
    (spec(MT.RACE_TO_ROUNDS, map_number=3, line=12.0), "team1", BO3_FULL, WON),  # only team1 got to 12
    (spec(MT.RACE_TO_ROUNDS, map_number=3, line=11.0), "team1", BO3_FULL, UNKNOWN),  # needs the history
    (spec(MT.RACE_TO_ROUNDS, map_number=3, line=5.0), "team1", BO3_SWEEP, VOID),
    # halves: map 1 8-4 then 5-5; map 2 5-7 then 7-5 (overtime rounds do not belong to a half)
    (spec(MT.HALF_WINNER, map_number=1, half=1), "team1", BO3_FULL, WON),
    (spec(MT.HALF_WINNER, map_number=1, half=1), "draw", BO3_FULL, LOST),
    (spec(MT.HALF_WINNER, map_number=1, half=2), "draw", BO3_FULL, WON),
    (spec(MT.HALF_WINNER, map_number=1, half=2), "team1", BO3_FULL, LOST),
    (spec(MT.HALF_WINNER, map_number=1, half=2), "team2", BO3_FULL, LOST),
    (spec(MT.HALF_WINNER, map_number=2, half=1), "team2", BO3_FULL, WON),
    (spec(MT.HALF_WINNER, map_number=2, half=2), "team1", BO3_FULL, WON),
    (spec(MT.HALF_WINNER, map_number=3, half=1), "team1", BO3_FULL, UNKNOWN),
    (spec(MT.HALF_WINNER, map_number=3, half=1), "team1", BO3_SWEEP, VOID),
    # player kills: 101 has 22 on map 1 and 25 on map 2; 201 has 20 and 31
    (spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=21.5), "over", BO3_FULL, WON),
    (spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=22.5), "over", BO3_FULL, LOST),
    (spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=22.0), "over", BO3_FULL, PUSH),
    (spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=22.0), "under", BO3_FULL, PUSH),
    (spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 2), line=46.5), "over", BO3_FULL, WON),
    (spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 2), line=47.5), "over", BO3_FULL, LOST),
    (spec(MT.PLAYER_KILLS, player_id=101, map_number=3, line=15.5), "over", BO3_SWEEP, VOID),
    (spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 2, 3), line=60.5), "over", BO3_SWEEP, VOID),
    (spec(MT.PLAYER_KILLS, player_id=101, line=46.5), "over", BO3_SWEEP, WON),  # match = maps played
    (spec(MT.PLAYER_KILLS, player_id=101, line=60.5), "over", BO3_FULL, UNKNOWN),  # no stats for map 3
    (spec(MT.PLAYER_KILLS, player_id=999, map_number=1, line=15.5), "over", BO3_FULL, UNKNOWN),
    # head to head on map 2: 101 has 25, 201 has 31
    (
        spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=2),
        "player:201",
        BO3_FULL,
        WON,
    ),
    (
        spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=2),
        "player:101",
        BO3_FULL,
        LOST,
    ),
    (
        spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=2, line=6.5),
        "player:101",
        BO3_FULL,
        WON,
    ),  # 25 + 6.5 > 31
    (
        spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=2, line=6.0),
        "player:101",
        BO3_FULL,
        PUSH,
    ),
    (
        spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=3),
        "player:101",
        BO3_SWEEP,
        VOID,
    ),
    # top fragger: map 1 -> 101 (22); map 2 -> 201 (31); maps 1+2 -> 201 (51) ahead of 101 (47)
    (spec(MT.TOP_FRAGGER, map_number=1), "player:101", BO3_FULL, WON),
    (spec(MT.TOP_FRAGGER, map_number=1), "player:201", BO3_FULL, LOST),
    (spec(MT.TOP_FRAGGER, map_number=2), "player:201", BO3_FULL, WON),
    (spec(MT.TOP_FRAGGER, maps_scope=(1, 2)), "player:201", BO3_FULL, WON),
    (spec(MT.TOP_FRAGGER, maps_scope=(1, 2)), "player:101", BO3_FULL, LOST),
    (spec(MT.TOP_FRAGGER, map_number=3), "player:101", BO3_SWEEP, VOID),
    (spec(MT.TOP_FRAGGER, map_number=3), "player:101", BO3_FULL, UNKNOWN),  # no stats for map 3
    (spec(MT.TOP_FRAGGER), "player:101", BO3_FULL, UNKNOWN),  # match scope includes map 3
]


@pytest.mark.parametrize(("market", "selection", "actuals", "expected"), CASES, ids=lambda x: str(x)[:60])
def test_resolve_outcome(
    market: MarketSpec, selection: str, actuals: MatchActuals, expected: SettlementOutcome
) -> None:
    assert settle(market, selection, actuals) is expected


def test_team_restricted_top_fragger_needs_the_player_teams() -> None:
    market = spec(MT.TOP_FRAGGER, map_number=2, team="team1")
    assert settle(market, "player:101", BO3_FULL) is UNKNOWN  # MapActual does not say who plays where
    assert settle(market, "player:101", BO3_FULL, player_teams=PLAYER_TEAMS) is WON  # 25, best of team1
    assert settle(market, "player:102", BO3_FULL, player_teams=PLAYER_TEAMS) is LOST
    # 201 has more kills overall but is not on team1: asking for him in a team1 market is undecidable
    assert settle(market, "player:201", BO3_FULL, player_teams=PLAYER_TEAMS) is UNKNOWN
    team2_market = spec(MT.TOP_FRAGGER, map_number=2, team="team2")
    assert settle(team2_market, "player:201", BO3_FULL, player_teams=PLAYER_TEAMS) is WON


def test_top_fragger_ties_and_partial_statistics_are_never_guessed() -> None:
    tied = MAP1.model_copy(update={"player_kills": kills(22, p201=22)})
    actuals = series(MatchFormat.BO3, tied, MAP2, MAP3)
    market = spec(MT.TOP_FRAGGER, map_number=1)
    result = explain_outcome(market, "player:101", actuals, **RULES)
    assert result.outcome is UNKNOWN and "dead-heat" in result.reason
    assert settle(market, "player:102", actuals) is LOST  # beaten outright: a tie above does not matter
    # only three stat lines: the leader cannot be confirmed (somebody unlisted may have more kills)
    partial = MAP1.model_copy(update={"player_kills": {101: 22, 102: 20, 201: 18}})
    actuals = series(MatchFormat.BO3, partial, MAP2, MAP3)
    assert settle(market, "player:101", actuals) is UNKNOWN
    assert settle(market, "player:102", actuals) is LOST  # ...but a beaten player has lost for sure


def test_overtime_can_be_excluded_from_round_totals_and_handicaps() -> None:
    """Regulation-only rules: the 14-16 overtime map counts as 12-12 (24 rounds, margin 0)."""
    reg = {"include_overtime_in_totals": False}
    assert settle(spec(MT.TOTAL_ROUNDS, map_number=2, line=26.5), "over", BO3_FULL, **reg) is LOST
    assert settle(spec(MT.TOTAL_ROUNDS, map_number=2, line=24.0), "over", BO3_FULL, **reg) is PUSH
    assert settle(spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=2), "even", BO3_FULL, **reg) is WON
    team2_total = spec(MT.TEAM_TOTAL_ROUNDS, map_number=2, team="team2", line=12.5)
    assert settle(team2_total, "over", BO3_FULL, **reg) is LOST
    assert settle(team2_total, "over", BO3_FULL) is WON
    handicap = spec(MT.ROUND_HANDICAP, map_number=2, line=1.5)
    assert settle(handicap, "team2", BO3_FULL, **reg) is LOST  # 12-12 +1.5 for team1
    assert settle(handicap, "team2", BO3_FULL) is WON  # 14-16 +1.5 is still a team2 win
    assert settle(spec(MT.ROUND_HANDICAP, map_number=2, line=0.0), "team1", BO3_FULL, **reg) is PUSH
    # markets on the final score are not affected
    assert settle(spec(MT.MAP_WINNER, map_number=2), "team2", BO3_FULL, **reg) is WON
    assert settle(spec(MT.MAP_CORRECT_SCORE, map_number=2), "14-16", BO3_FULL, **reg) is WON
    assert settle(spec(MT.TOTAL_ROUNDS, map_number=1, line=21.5), "over", BO3_FULL, **reg) is WON


def test_head_to_head_tie_rule() -> None:
    level = MAP1.model_copy(update={"player_kills": kills(22, p201=22)})
    actuals = series(MatchFormat.BO3, level, MAP2, MAP3)
    market = spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=1)
    for selection in ("player:101", "player:201"):
        assert settle(market, selection, actuals, h2h_tie="push") is PUSH
        assert settle(market, selection, actuals, h2h_tie="lose") is LOST
    with pytest.raises(ValueError, match="h2h_tie"):
        resolve_outcome(market, "player:101", actuals, h2h_tie="win")


def test_rules_default_to_the_betclic_settings() -> None:
    market = spec(MT.TOTAL_ROUNDS, map_number=2, line=26.5)
    assert resolve_outcome(market, "over", BO3_FULL) is WON  # default settings: overtime counts
    regulation = BetclicSettings(totals_include_overtime=False, h2h_tie_rule="lose")
    assert resolve_outcome(market, "over", BO3_FULL, betclic=regulation) is LOST
    assert (
        resolve_outcome(market, "over", BO3_FULL, betclic=regulation, include_overtime_in_totals=True) is WON
    )
    level = MAP1.model_copy(update={"player_kills": kills(22, p201=22)})
    h2h = spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=1)
    actuals = series(MatchFormat.BO3, level, MAP2, MAP3)
    assert resolve_outcome(h2h, "player:101", actuals) is PUSH
    assert resolve_outcome(h2h, "player:101", actuals, betclic=regulation) is LOST


def test_bo2_draw_and_other_formats() -> None:
    lost_map = MAP2.model_copy(update={"round_winners": None})
    drawn = series(MatchFormat.BO2, MAP1, lost_map)
    assert settle(spec(MT.MATCH_WINNER), "draw", drawn) is WON
    assert settle(spec(MT.MATCH_WINNER), "team1", drawn) is LOST
    assert settle(spec(MT.MATCH_WINNER), "team2", drawn) is LOST
    assert settle(spec(MT.CORRECT_MAP_SCORE), "1-1", drawn) is WON
    assert settle(spec(MT.MAP_HANDICAP, line=0.0), "team1", drawn) is PUSH
    assert settle(spec(MT.MAP_HANDICAP, line=0.5), "team1", drawn) is WON
    assert settle(spec(MT.BOTH_TEAMS_WIN_A_MAP), "yes", drawn) is WON
    assert settle(spec(MT.TOTAL_MAPS, line=1.5), "over", drawn) is WON
    assert settle(spec(MT.MATCH_WINNER), "draw", BO3_FULL) is UNKNOWN  # no draw outside BO2
    bo1 = series(MatchFormat.BO1, MAP1)
    assert settle(spec(MT.MATCH_WINNER), "team1", bo1) is WON
    assert settle(spec(MT.CORRECT_MAP_SCORE), "1-0", bo1) is WON
    assert settle(spec(MT.MAP_WINNER, map_number=2), "team1", bo1) is UNKNOWN  # a BO1 has no map 2
    win = MAP1
    bo5 = series(
        MatchFormat.BO5,
        win,
        lost_map,
        win.model_copy(update={"map_number": 3}),
        win.model_copy(update={"map_number": 4}),
    )
    assert settle(spec(MT.MATCH_WINNER), "team1", bo5) is WON
    assert settle(spec(MT.CORRECT_MAP_SCORE), "3-1", bo5) is WON
    assert settle(spec(MT.MAP_HANDICAP, line=-1.5), "team1", bo5) is WON
    assert settle(spec(MT.MAP_HANDICAP, line=-2.5), "team1", bo5) is LOST
    assert settle(spec(MT.TOTAL_MAPS, line=4.5), "under", bo5) is WON
    assert settle(spec(MT.MAP_WINNER, map_number=5), "team1", bo5) is VOID
    assert settle(spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 2, 3), line=60.5), "over", bo5) is WON


def test_cancelled_and_forfeited_matches_void_everything() -> None:
    for flags in ({"cancelled": True}, {"forfeited": True}):
        for actuals in (series(MatchFormat.BO3, **flags), series(MatchFormat.BO3, MAP1, **flags)):
            assert settle(spec(MT.MATCH_WINNER), "team1", actuals) is VOID
            assert settle(spec(MT.TOTAL_ROUNDS, map_number=1, line=21.5), "over", actuals) is VOID
            assert settle(spec(MT.PLAYER_KILLS, player_id=101, line=40.5), "under", actuals) is VOID
    reason = explain_outcome(spec(MT.MATCH_WINNER), "team1", series(MatchFormat.BO3, forfeited=True)).reason
    assert "forfeit" in reason


@pytest.mark.parametrize(
    "actuals",
    [
        series(MatchFormat.BO3, MAP1, complete=False),  # still running
        series(MatchFormat.BO3, MAP1),  # 1-0 is not a finished BO3
        series(MatchFormat.BO3, MAP1, MAP3),  # map numbers 1 and 3
        series(MatchFormat.BO3, MAP1, MAP1),  # the same map twice
        series(MatchFormat.BO3, MAP1, MAP2.model_copy(update={"team1_rounds": 13, "team2_rounds": 12})),
        series(MatchFormat.BO3, MAP1, MAP2.model_copy(update={"team1_rounds": 1, "team2_rounds": 0})),
        series(MatchFormat.BO2, MAP1),  # a BO2 has two maps
        series(  # a third map after a 2-0
            MatchFormat.BO3,
            MAP1,
            MAP1.model_copy(update={"map_number": 2}),
            MAP1.model_copy(update={"map_number": 3}),
        ),
        series(MatchFormat.BO1, MAP1, MAP2),
    ],
    ids=[
        "incomplete",
        "undecided",
        "gap",
        "duplicate map",
        "13-12",
        "1-0 (default win)",
        "short bo2",
        "extra map",
        "bo1 with two maps",
    ],
)
def test_inconsistent_series_are_unknown_never_a_guess(actuals: MatchActuals) -> None:
    for market, selection in (
        (spec(MT.MATCH_WINNER), "team1"),
        (spec(MT.MAP_WINNER, map_number=1), "team1"),
        (spec(MT.TOTAL_ROUNDS, map_number=1, line=21.5), "over"),
        (spec(MT.MAP_WINNER, map_number=3), "team1"),
    ):
        result = explain_outcome(market, selection, actuals, **RULES)
        assert result.outcome is UNKNOWN and result.reason


def test_round_history_must_match_the_final_score() -> None:
    pistol = spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=1)
    short = MAP1.model_copy(update={"round_winners": MAP1.round_winners[:-1]})
    wrong = MAP1.model_copy(update={"round_winners": history((T2, 13), (T1, 9))})
    # 13 team1 wins in the first 13 rounds: the map would have ended there
    overrun = MAP1.model_copy(update={"round_winners": history((T1, 13), (T2, 9))})
    for broken in (short, wrong, overrun):
        actuals = series(MatchFormat.BO3, broken, MAP2, MAP3)
        assert settle(pistol, "team1", actuals) is UNKNOWN
        assert settle(spec(MT.HALF_WINNER, map_number=1, half=1), "team1", actuals) is UNKNOWN
        # markets that only need the final score still settle
        assert settle(spec(MT.MAP_WINNER, map_number=1), "team1", actuals) is WON


@pytest.mark.parametrize(
    ("market", "selection"),
    [
        (spec(MT.OTHER), "team1"),
        (spec(MT.MATCH_WINNER), "over"),
        (spec(MT.MAP_WINNER), "team1"),  # no map number
        (spec(MT.MAP_WINNER, map_number=4), "team1"),  # a BO3 has no map 4
        (spec(MT.MAP_WINNER, map_number=1), "draw"),
        (spec(MT.TOTAL_ROUNDS, map_number=1), "over"),  # no line
        (spec(MT.TOTAL_ROUNDS, map_number=1, line=float("nan")), "over"),
        (spec(MT.TOTAL_ROUNDS, map_number=1, line=21.5), "yes"),
        (spec(MT.TEAM_TOTAL_ROUNDS, map_number=1, line=9.5), "over"),  # no team
        (spec(MT.CORRECT_MAP_SCORE), "3-0"),  # impossible in a BO3
        (spec(MT.MAP_CORRECT_SCORE, map_number=1), "13-12"),
        (spec(MT.MAP_CORRECT_SCORE, map_number=1), "thirteen"),
        (spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=2), "team1"),
        (spec(MT.RACE_TO_ROUNDS, map_number=1, line=5.5), "team1"),
        (spec(MT.RACE_TO_ROUNDS, map_number=1, line=0.0), "team1"),
        (spec(MT.HALF_WINNER, map_number=1, half=3), "team1"),
        (spec(MT.PLAYER_KILLS, map_number=1, line=15.5), "over"),  # no player
        (spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 7), line=15.5), "over"),
        (spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=101, map_number=1), "player:101"),
        (spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=1), "player:102"),
        (spec(MT.TOP_FRAGGER, map_number=1), "team1"),
        (spec(MT.TOP_FRAGGER, map_number=1, player_id=102), "player:101"),
    ],
    ids=lambda x: str(x)[:50],
)
def test_malformed_markets_are_unknown(market: MarketSpec, selection: str) -> None:
    """Whatever pricing refuses to price (UnpriceableMarket) settlement refuses to settle."""
    assert settle(market, selection, BO3_FULL) is UNKNOWN
    assert settle(market, selection, BO3_SWEEP) is UNKNOWN  # never VOID for a market that makes no sense


def test_negative_kills_are_not_trusted() -> None:
    broken = MAP1.model_copy(update={"player_kills": kills(22, p101=-1)})
    actuals = series(MatchFormat.BO3, broken, MAP2, MAP3)
    assert settle(spec(MT.PLAYER_KILLS, player_id=101, map_number=1, line=5.5), "under", actuals) is UNKNOWN
    assert settle(spec(MT.TOP_FRAGGER, map_number=1), "player:102", actuals) is UNKNOWN


# ----------------------------------------------------------------------------------------------
# spec keys and bets
# ----------------------------------------------------------------------------------------------

ROUND_TRIP_SPECS = [
    spec(MT.MATCH_WINNER),
    spec(MT.MAP_HANDICAP, line=-1.5),
    spec(MT.TOTAL_MAPS, line=2.5),
    spec(MT.ROUND_HANDICAP, map_number=2, line=3.0),
    spec(MT.TOTAL_ROUNDS, map_number=3, line=26.5),
    spec(MT.TEAM_TOTAL_ROUNDS, map_number=1, team="team2", line=10.5),
    spec(MT.PISTOL_ROUND_WINNER, map_number=1, round_number=13),
    spec(MT.RACE_TO_ROUNDS, map_number=1, line=5.0),
    spec(MT.HALF_WINNER, map_number=2, half=2),
    spec(MT.PLAYER_KILLS, player_id=101, maps_scope=(1, 2), line=33.5),
    spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=201, map_number=1, line=-2.5),
    spec(MT.TOP_FRAGGER, team="team1"),
    spec(MT.OTHER),
]


@pytest.mark.parametrize("market", ROUND_TRIP_SPECS, ids=lambda s: s.key())
def test_spec_from_key_round_trip(market: MarketSpec) -> None:
    assert spec_from_key(market.key()) == market
    assert spec_from_key(market.key()).key() == market.key()


def test_spec_from_key_ignores_the_player_name_and_rejects_garbage() -> None:
    named = spec(MT.PLAYER_KILLS, player_id=101, player_name="ZywOo", map_number=1, line=17.5)
    assert spec_from_key(named.key()) == named.model_copy(update={"player_name": None})
    for bad in ("", "nonsense", "match_winner|map", "total_rounds|map=x", "total_rounds|what=1", "x|map=1"):
        with pytest.raises(ValueError):
            spec_from_key(bad)
    with pytest.raises(ValueError):
        spec_from_key("team_total_rounds|map=1|team=team3|line=9.5")  # rejected by MarketSpec itself


def test_resolve_bet(session: Session) -> None:
    add_match(session, match_id=7)
    add_match(session, match_id=8)
    bet = add_bet(session, match_id=7, spec_key="total_rounds|map=2|line=26.5", selection_key="over")
    assert resolve_bet(bet, BO3_FULL, **RULES) == Resolution(WON)
    void = add_bet(session, match_id=7, spec_key="map_winner|map=3", selection_key="team1")
    result = resolve_bet(void, BO3_SWEEP, **RULES)
    assert result.outcome is VOID and "map 3" in result.reason
    other_match = add_bet(session, match_id=8, spec_key="match_winner", selection_key="team1")
    assert resolve_bet(other_match, BO3_FULL).outcome is UNKNOWN
    unreadable = add_bet(session, match_id=7, spec_key="match_winner|oops", selection_key="team1")
    assert resolve_bet(unreadable, BO3_FULL).outcome is UNKNOWN


def test_bet_status_for() -> None:
    assert bet_status_for(WON) is BetStatus.WON
    assert bet_status_for(LOST) is BetStatus.LOST
    assert bet_status_for(VOID) is BetStatus.VOID
    assert bet_status_for(PUSH) is BetStatus.VOID
    assert bet_status_for(UNKNOWN) is None


# ----------------------------------------------------------------------------------------------
# consistency with pricing
# ----------------------------------------------------------------------------------------------

N_SERIES = 700
FORMAT_ROUND_PROBS: dict[MatchFormat, list[float]] = {
    MatchFormat.BO1: [0.5],
    MatchFormat.BO2: [0.52, 0.47],
    MatchFormat.BO3: [0.53, 0.47, 0.5],
    MatchFormat.BO5: [0.52, 0.5, 0.48, 0.5, 0.51],
}


def consistency_markets(fmt: MatchFormat) -> list[tuple[MarketSpec, tuple[str, ...]]]:
    """Every market type with several lines; each entry lists all selections of that market."""
    teams = ("team1", "team2")
    over_under = ("over", "under")
    out: list[tuple[MarketSpec, tuple[str, ...]]] = [
        (spec(MT.MATCH_WINNER), (*teams, "draw") if fmt is MatchFormat.BO2 else teams),
        (spec(MT.CORRECT_MAP_SCORE), tuple(f"{a}-{b}" for a, b in sorted(pricing.final_map_scores(fmt)))),
        (spec(MT.BOTH_TEAMS_WIN_A_MAP), ("yes", "no")),
    ]
    out += [(spec(MT.MAP_HANDICAP, line=line), teams) for line in (-2.5, -1.5, -1.0, 0.0, 0.5, 1.0, 1.5)]
    out += [(spec(MT.TOTAL_MAPS, line=line), over_under) for line in (1.5, 2.0, 2.5, 3.0, 3.5, 4.5)]
    for k in range(1, fmt.n_maps + 1):
        out.append((spec(MT.MAP_WINNER, map_number=k), teams))
        out += [
            (spec(MT.ROUND_HANDICAP, map_number=k, line=line), teams)
            for line in (-5.5, -3.0, -2.5, -2.0, 0.0, 1.5, 2.0, 4.0, 4.5)
        ]
        out += [
            (spec(MT.TOTAL_ROUNDS, map_number=k, line=line), over_under)
            for line in (18.5, 20.0, 21.5, 23.5, 24.0, 24.5, 26.5, 30.0)
        ]
        out += [
            (spec(MT.TEAM_TOTAL_ROUNDS, map_number=k, team=team, line=line), over_under)
            for team in teams
            for line in (8.5, 10.0, 12.0, 12.5, 13.0, 13.5, 16.0)
        ]
        out.append((spec(MT.TOTAL_ROUNDS_ODD_EVEN, map_number=k), ("odd", "even")))
        out.append(
            (
                spec(MT.MAP_CORRECT_SCORE, map_number=k),
                ("13-0", "13-7", "13-11", "11-13", "16-14", "14-16", "13-16", "19-17", "22-20"),
            )
        )
        out.append((spec(MT.MAP_OVERTIME, map_number=k), ("yes", "no")))
        out += [(spec(MT.PISTOL_ROUND_WINNER, map_number=k, round_number=r), teams) for r in (1, 13)]
        out += [(spec(MT.RACE_TO_ROUNDS, map_number=k, line=float(t)), teams) for t in RACE_TARGETS]
        out += [(spec(MT.HALF_WINNER, map_number=k, half=h), (*teams, "draw")) for h in (1, 2)]
        out += [
            (spec(MT.PLAYER_KILLS, player_id=pid, map_number=k, line=line), over_under)
            for pid in (101, 203)
            for line in (12.5, 17.0, 17.5, 22.0)
        ]
    scopes = [tuple(range(1, last + 1)) for last in range(2, fmt.n_maps + 1)]
    for scope in scopes:
        mid = 17.5 * len(scope)
        out += [
            (spec(MT.PLAYER_KILLS, player_id=102, maps_scope=scope, line=line), over_under)
            for line in (mid - 4.5, mid - 0.5, mid + 3.5)
        ]
    out += [
        (spec(MT.PLAYER_KILLS, player_id=204, line=line), over_under)
        for line in (20.5, 35.0, 35.5, 47.5, 60.0)
    ]
    return out


def frequencies(
    market: MarketSpec, selection: str, population: list[MatchActuals], **rules: object
) -> Counter[SettlementOutcome]:
    return Counter(
        resolve_outcome(market, selection, s, player_teams=PLAYER_TEAMS, **rules) for s in population
    )


@pytest.fixture(scope="module", params=list(MatchFormat), ids=lambda f: f.value)
def population(request: pytest.FixtureRequest) -> tuple[MatchFormat, list[MatchActuals], MatchPrediction]:
    fmt: MatchFormat = request.param
    rng = np.random.default_rng(20_260 + fmt.n_maps)
    sample = [simulate_series(rng, fmt, FORMAT_ROUND_PROBS[fmt]) for _ in range(N_SERIES)]
    return fmt, sample, empirical_prediction(sample, fmt)


@pytest.mark.parametrize("include_overtime", [True, False], ids=["with overtime", "regulation only"])
def test_pricing_and_settlement_agree_on_every_market(
    population: tuple[MatchFormat, list[MatchActuals], MatchPrediction], include_overtime: bool
) -> None:
    """The prediction describes the sample exactly, so the priced probabilities must equal the
    settled frequencies: P(win), P(push) and P(void) of every selection, to float precision."""
    fmt, sample, prediction = population
    rules = {"include_overtime_in_totals": include_overtime, "h2h_tie": "push"}
    n = len(sample)
    checked = 0
    for market, selections in consistency_markets(fmt):
        settled_mass = []
        for selection in selections:
            try:
                priced = pricing.price_outcome(market, selection, prediction, sigma_floor=0.01, **rules)
            except pricing.UnpriceableMarket:
                # the only acceptable disagreement: a slot the sample never reached
                assert market.map_number is not None or market.maps_scope is not None
                assert all(
                    resolve_outcome(market, selection, s, **rules) in (VOID, UNKNOWN) for s in sample
                ), f"{market.key()}#{selection} is unpriceable but settles"
                continue
            freq = frequencies(market, selection, sample, **rules)
            label = f"{fmt.value} {market.key()}#{selection}"
            assert freq[UNKNOWN] == 0, label
            assert freq[WON] / n == pytest.approx(priced.p_win, abs=1e-9), label
            assert freq[PUSH] / n == pytest.approx(priced.p_push, abs=1e-9), label
            assert freq[VOID] / n == pytest.approx(priced.p_void, abs=1e-9), label
            assert freq[LOST] / n == pytest.approx(priced.p_lose, abs=1e-9), label
            settled_mass.append((priced.p_win, priced.p_push, priced.p_void))
            checked += 1
        # exhaustive selection lists: exactly one selection wins whenever the market settles
        if settled_mass and market.market_type not in (MT.MAP_CORRECT_SCORE,):
            p_push, p_void = settled_mass[0][1], settled_mass[0][2]
            assert sum(p for p, _, _ in settled_mass) == pytest.approx(1.0 - p_push - p_void, abs=1e-9)
    assert checked > 100 * fmt.n_maps


def test_overtime_population_is_not_trivial(
    population: tuple[MatchFormat, list[MatchActuals], MatchPrediction],
) -> None:
    """Guard for the test above: the sample really contains overtimes, double overtimes, pushes and
    unplayed maps, so the agreement is not vacuous."""
    fmt, sample, prediction = population
    maps = [m for s in sample for m in s.maps]
    assert sum(1 for m in maps if m.went_to_overtime) > 20
    assert any(max(m.team1_rounds, m.team2_rounds) >= 19 for m in maps)
    if fmt.maps_to_win is not None and fmt.n_maps > 1:
        assert 0.05 < prediction.central.slots[-1].p_played < 0.95
    first = prediction.central.slots[0]
    assert set(first.p_neither_race_to) == {"14", "16", "17", "19"}
    assert 0.5 < first.p_neither_race_to["14"] < 1.0  # regulation finishes never reach 14


def test_race_to_is_priced_the_same_without_the_published_neither_probability(
    population: tuple[MatchFormat, list[MatchActuals], MatchPrediction],
) -> None:
    """P(neither team reaches N) derived from the score pmf equals the simulator's own number."""
    fmt, sample, prediction = population
    bare = empirical_prediction(sample, fmt, publish_neither=False)
    assert not bare.central.slots[0].p_neither_race_to
    for target in (14, 16, 17, 19):
        market = spec(MT.RACE_TO_ROUNDS, map_number=1, line=float(target))
        for selection in ("team1", "team2"):
            with_neither = pricing.price_outcome(market, selection, prediction, sigma_floor=0.0, **RULES)
            derived = pricing.price_outcome(market, selection, bare, sigma_floor=0.0, **RULES)
            assert derived.p_win == pytest.approx(with_neither.p_win, abs=1e-9)
            assert derived.p_void == pytest.approx(with_neither.p_void, abs=1e-9)
            assert with_neither.p_void > 0.5


@pytest.mark.parametrize("tie_rule", ["push", "lose"])
def test_head_to_head_agrees_statistically(
    population: tuple[MatchFormat, list[MatchActuals], MatchPrediction], tie_rule: str
) -> None:
    """Pricing multiplies the two players' marginal pmfs; kills in the sample are drawn independently,
    so the settled frequencies agree up to sampling noise - and the void share agrees exactly."""
    fmt, sample, prediction = population
    rules = {"include_overtime_in_totals": True, "h2h_tie": tie_rule}
    n = len(sample)
    markets = [spec(MT.PLAYER_KILLS_H2H, player_id=101, opponent_player_id=202)]
    markets += [
        spec(MT.PLAYER_KILLS_H2H, player_id=103, opponent_player_id=205, map_number=k, line=line)
        for k in range(1, fmt.n_maps + 1)
        for line in (None, -2.5, 3.0)
    ]
    for market in markets:
        a, b = market.player_id, market.opponent_player_id
        total_win = 0.0
        for selection in (f"player:{a}", f"player:{b}"):
            priced = pricing.price_outcome(market, selection, prediction, sigma_floor=0.01, **rules)
            freq = frequencies(market, selection, sample, **rules)
            label = f"{fmt.value} {market.key()}#{selection}"
            assert freq[UNKNOWN] == 0, label
            assert freq[VOID] / n == pytest.approx(priced.p_void, abs=1e-9), label
            assert freq[WON] / n == pytest.approx(priced.p_win, abs=0.07), label
            assert freq[PUSH] / n == pytest.approx(priced.p_push, abs=0.04), label
            if tie_rule == "lose":
                assert freq[PUSH] == 0 and priced.p_push == 0.0
            total_win += priced.p_win
            push, void = priced.p_push, priced.p_void
        # under "lose" the tie mass (the push probability of the "push" rule) is lost by both sides
        tie_mass = pricing.price_outcome(
            market, f"player:{a}", prediction, sigma_floor=0.01, h2h_tie="push"
        ).p_push
        assert (tie_mass > 0) == (market.line is None or float(market.line).is_integer())
        assert push == (tie_mass if tie_rule == "push" else 0.0)
        assert total_win == pytest.approx(1.0 - tie_mass - void, abs=1e-9)


def test_top_fragger_agrees_up_to_dead_heats(
    population: tuple[MatchFormat, list[MatchActuals], MatchPrediction],
) -> None:
    """Pricing uses the dead-heat adjusted probability; settlement declares outright winners and
    leaves ties to the bookmaker: ``won <= p_win <= won + ties`` and the void share is exact."""
    fmt, sample, prediction = population
    n = len(sample)
    markets = [spec(MT.TOP_FRAGGER), spec(MT.TOP_FRAGGER, team="team1"), spec(MT.TOP_FRAGGER, team="team2")]
    for k in range(1, fmt.n_maps + 1):
        markets += [spec(MT.TOP_FRAGGER, map_number=k), spec(MT.TOP_FRAGGER, map_number=k, team="team2")]
    if fmt.n_maps >= 2:
        markets.append(spec(MT.TOP_FRAGGER, maps_scope=(1, 2)))
    for market in markets:
        candidates = TEAM1_IDS if market.team == "team1" else TEAM2_IDS if market.team == "team2" else None
        ids = candidates or (*TEAM1_IDS, *TEAM2_IDS)
        won_total = p_total = 0.0
        for pid in ids:
            selection = f"player:{pid}"
            try:
                priced = pricing.price_outcome(market, selection, prediction, sigma_floor=0.01, **RULES)
            except pricing.UnpriceableMarket:
                assert market.map_number is not None  # a slot the sample never reached
                continue
            freq = frequencies(market, selection, sample, **RULES)
            label = f"{fmt.value} {market.key()}#{selection}"
            assert freq[PUSH] == 0, label
            assert freq[VOID] / n == pytest.approx(priced.p_void, abs=1e-9), label
            assert freq[WON] / n <= priced.p_win + 1e-9, label
            assert priced.p_win <= (freq[WON] + freq[UNKNOWN]) / n + 1e-9, label
            won_total += freq[WON] / n
            p_total += priced.p_win
        if p_total:
            settle_mass = 1.0 - priced.p_void
            assert p_total == pytest.approx(settle_mass, abs=1e-9)  # dead heats split the whole mass
            assert won_total <= settle_mass + 1e-9
            assert won_total > 0.7 * settle_mass  # ties are the exception, not the rule


def test_stake_refund_rules_are_shared_with_the_ledger(session: Session) -> None:
    """A PUSH and a VOID are both refunds for the money side (``SettlementOutcome.is_refund``)."""
    from cs2bot.engine import ledger
    from cs2bot.engine.ev import TaxModel

    model = TaxModel(Decimal("0.12"), Decimal("2280"), Decimal("0.10"))
    add_match(session, match_id=7)
    bet = add_bet(session, match_id=7, stake="50.00", odds="1.80")
    for outcome in (PUSH, VOID):
        assert outcome.is_refund
        assert ledger.settle_amounts(bet, outcome, model) == (Decimal("50.00"), Decimal("0.00"))
    assert ledger.settle_amounts(bet, WON, model) == (Decimal("79.20"), Decimal("29.20"))
    assert ledger.settle_amounts(bet, LOST, model) == (Decimal("0.00"), Decimal("-50.00"))
    with pytest.raises(ValueError):
        ledger.settle_amounts(bet, UNKNOWN, model)
