"""Opt-in validation of ``parse_demo`` against REAL demos and their published scoreboards.

Skipped unless ``CS2BOT_REAL_DEMO_DIR`` points at a directory holding the demo files below. They are
the public fixtures of the awpy project (https://github.com/pnxenopoulos/awpy-fixtures, 84-640 MB
each, far too big for this repository); the expected numbers are the scoreboards recorded in that
project's ``manifest.json`` (csstats.gg / FACEIT / ESEA / HLTV). Download them once with::

    for n in valve-de_mirage-472514809 faceit-de_mirage-6de46e85 esea-de_dust2-2c5bdd12 \\
             hltv-de_inferno-2395492 hltv-de_mirage-2394170 hltv-de_nuke-221001; do
      curl -L -o "$CS2BOT_REAL_DEMO_DIR/$n.dem" \\
        "https://github.com/pnxenopoulos/awpy-fixtures/releases/download/$n/$n.dem"
    done
    CS2BOT_REAL_DEMO_DIR=... .venv/bin/python -m pytest tests/ingest/demos/test_demos_real_fixtures.py

A demo that is missing from the directory skips its own test. The whole module passed on 2026-11-23
with demoparser2 0.42.2 (the only deviation from a published number is documented at ``bargota22``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

from cs2bot.config import DemoSettings
from cs2bot.ingest.demos.archive import read_demo_map_name
from cs2bot.ingest.demos.parse import ParsedDemo, parse_demo

# Read at import time: the autouse fixture of tests/conftest.py removes CS2BOT_* variables per test.
REAL_DEMO_DIR = os.environ.get("CS2BOT_REAL_DEMO_DIR")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not REAL_DEMO_DIR, reason="CS2BOT_REAL_DEMO_DIR is not set (see the module docstring)"
    ),
]


@dataclass(frozen=True)
class Expected:
    """Published facts about one real demo. Player stats: name -> {stat: value}."""

    name: str
    map_name: str
    score: tuple[int, int]  # winner first
    rounds: int
    knife_rounds: int = 0
    replayed: int = 0
    players: dict[str, dict[str, float]] = field(default_factory=dict)


FIXTURES = [
    Expected(
        "valve-de_mirage-472514809",
        "mirage",
        (13, 0),
        13,
        players={
            "organner.pl free trial": {
                "kills": 27,
                "deaths": 1,  # a suicide / world death, which csstats omits and the game counts
                "adr": 183.2,
                "headshots": 11,
                "enemies_flashed": 6,
            },
            # csstats shows ADR 34.7 for this player; the game events give 37.7 (awpy documents the same
            # ~3 HP/round residual for this one player and allows it), so ADR is not asserted for him.
            "bargota22": {"kills": 4, "deaths": 13, "kast": 23.1},
        },
    ),
    Expected(
        "faceit-de_mirage-6de46e85",
        "mirage",
        (16, 14),  # overtime
        30,
        knife_rounds=1,
        players={
            "babaya0310": {"kills": 21, "deaths": 25, "adr": 82.8, "headshots": 7, "kast": 70.0},
            "cntoz": {"kills": 19, "deaths": 19, "adr": 63.9, "headshots": 8, "multikill_rounds": 5},
        },
    ),
    Expected(
        "esea-de_dust2-2c5bdd12",
        "dust2",
        (13, 6),
        19,
        knife_rounds=1,
        players={
            "jared": {
                "kills": 10,
                "deaths": 11,
                "adr": 42.9,
                "headshots": 5,
                "multikill_rounds": 2,
                "kast": 73.7,
            },
            "1umar": {"kills": 16, "deaths": 10, "adr": 83.6, "headshots": 2, "multikill_rounds": 3},
        },
    ),
    Expected(
        "hltv-de_inferno-2395492",  # 9z vs PARIVISION
        "inferno",
        (13, 10),
        23,
        players={
            "dgt": {"kills": 20, "deaths": 7, "adr": 84.7, "kast": 95.7},
            # HLTV's KAST: a pure flash assist is not an "A" (with it Jame would be at 78.3)
            "Jame": {"kills": 12, "deaths": 12, "adr": 52.7, "kast": 73.9},
        },
    ),
    Expected("hltv-de_mirage-2394170", "mirage", (13, 5), 18),  # B8 vs Vitality
    # 9z vs RED Canids: round 16 was replayed after a backup restore and must be counted once
    Expected("hltv-de_nuke-221001", "nuke", (13, 11), 24, replayed=1),
]


def demo_path(name: str) -> Path:
    path = Path(REAL_DEMO_DIR or ".") / f"{name}.dem"
    if not path.is_file():
        pytest.skip(f"{path.name} is not in CS2BOT_REAL_DEMO_DIR")
    return path


@cache
def parsed_demo(name: str) -> ParsedDemo:
    return parse_demo(demo_path(name), DemoSettings())


def player_stats(parsed: ParsedDemo, name: str) -> dict[str, float]:
    rows = parsed.player_rounds[parsed.player_rounds["name"] == name]
    assert not rows.empty, f"player {name!r} not found"
    steamid = rows["steamid"].iloc[0]
    kills = parsed.kills[(parsed.kills["attacker_steamid"] == steamid) & parsed.kills["is_enemy_kill"]]
    return {
        "kills": int(rows["kills"].sum()),
        "deaths": int(rows["deaths"].sum()),
        "adr": round(float(rows["damage"].sum()) / parsed.n_rounds, 1),
        "kast": round(100.0 * float(rows["kast"].mean()), 1),
        "headshots": int(kills["headshot"].sum()),
        "multikill_rounds": int((rows["kills"] >= 2).sum()),
        "enemies_flashed": int(rows["enemies_flashed"].sum()),
    }


@pytest.mark.parametrize("expected", FIXTURES, ids=lambda e: e.name)
def test_real_demo_rounds_and_score(expected: Expected) -> None:
    path = demo_path(expected.name)
    assert read_demo_map_name(path) == expected.map_name
    parsed = parsed_demo(expected.name)

    assert parsed.map_name == expected.map_name
    assert parsed.n_rounds == expected.rounds
    assert parsed.rounds["round_number"].tolist() == list(range(1, expected.rounds + 1))
    assert tuple(sorted(parsed.roster_scores.values(), reverse=True)) == expected.score
    assert parsed.dropped_rounds["knife"] == expected.knife_rounds
    assert parsed.dropped_rounds["replayed"] == expected.replayed
    assert parsed.warnings == []
    # structure: ten players, five a side, every round; pistol rounds
    assert len(parsed.players) == 10
    assert (parsed.rounds["ct_players"] == 5).all() and (parsed.rounds["t_players"] == 5).all()
    assert len(parsed.player_rounds) == 10 * expected.rounds
    # at most one opening kill per round (a round can end without a kill: the bomb goes off on a full save)
    openings = parsed.kills[parsed.kills["is_opening"]]["round_number"]
    assert openings.is_unique and expected.rounds - 2 <= len(openings) <= expected.rounds
    pistols = parsed.rounds[parsed.rounds["ct_buy_type"] == "pistol"]["round_number"].tolist()
    assert pistols == [1, 13]
    first = parsed.rounds.iloc[0]
    assert (first["ct_money_start"], first["t_money_start"]) == (4000, 4000)
    assert parsed.rounds["ct_buy_type"].notna().all() and parsed.rounds["t_buy_type"].notna().all()
    # the scoreboard adds up: deaths == kill events, kills == enemy kills
    assert int(parsed.player_rounds["deaths"].sum()) == len(parsed.kills)
    assert int(parsed.player_rounds["kills"].sum()) == int(parsed.kills["is_enemy_kill"].sum())
    # every planted bomb has a site, and positions were sampled with place names
    planted = parsed.rounds[parsed.rounds["bomb_planted"]]
    assert planted["bomb_site"].isin(["A", "B"]).all()
    assert len(parsed.positions) > 5 * expected.rounds
    assert parsed.positions["place"].notna().mean() > 0.95
    assert parsed.kills["distance"].notna().mean() > 0.9


@pytest.mark.parametrize(
    ("fixture", "player"),
    [(e.name, player) for e in FIXTURES for player in e.players],
)
def test_real_demo_player_stats_match_the_published_scoreboard(fixture: str, player: str) -> None:
    expected = next(e for e in FIXTURES if e.name == fixture).players[player]
    actual = player_stats(parsed_demo(fixture), player)
    assert {stat: actual[stat] for stat in expected} == expected
