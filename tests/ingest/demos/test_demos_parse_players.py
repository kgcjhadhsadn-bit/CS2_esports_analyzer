"""Kills, trades, damage, utility, KAST, clutches, rosters and position samples of ``parse_demo``."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import pytest

from cs2bot.config import DemoSettings
from cs2bot.ingest.demos.parse import (
    KILL_COLUMNS,
    PLAYER_COLUMNS,
    PLAYER_ROUND_COLUMNS,
    POSITION_COLUMNS,
    ParsedDemo,
    ParseOptions,
    parse_demo,
)
from tests.ingest.demos.demos_builders import (
    STEAMIDS,
    TEAM_A,
    TEAM_B,
    B,
    DemoBuilder,
    FakeParser,
    H,
    K,
    N,
    factory_for,
    sid,
    standard_match,
)


def parse(parser: FakeParser, settings: DemoSettings | None = None, **kwargs: Any) -> ParsedDemo:
    return parse_demo("demo.dem", settings or DemoSettings(), parser_factory=factory_for(parser), **kwargs)


def one_round(**kwargs: Any) -> ParsedDemo:
    """A 13-round map whose first round is scripted by ``kwargs``."""
    builder = DemoBuilder()
    builder.add_round(kwargs.pop("winner", "CT"), **kwargs)
    builder.add_rounds("C" * 12)
    return parse(builder.build())


def player_round(parsed: ParsedDemo, name: str, number: int = 1) -> dict[str, Any]:
    frame = parsed.player_rounds
    rows = frame[(frame["round_number"] == number) & (frame["steamid"] == sid(name))].to_dict("records")
    assert len(rows) == 1, f"{name} has {len(rows)} rows in round {number}"
    return rows[0]


def kills_of(parsed: ParsedDemo, number: int = 1) -> list[dict[str, Any]]:
    return parsed.kills[parsed.kills["round_number"] == number].to_dict("records")


# ---------------------------------------------------------------------------------------- kills


def test_kill_rows_carry_all_event_details() -> None:
    parsed = one_round(
        kills=[
            K(
                21.5,
                "a1",
                "b1",
                weapon="awp",
                assister="a2",
                headshot=True,
                penetrated=1,
                thrusmoke=True,
                noscope=True,
                attackerblind=True,
                assistedflash=True,
            )
        ],
        positions={"a1": ("Window", 0.0, 0.0, 0.0), "b1": ("Middle", 300.0, 400.0, 0.0)},
    )
    assert list(parsed.kills.columns) == list(KILL_COLUMNS)
    (kill,) = kills_of(parsed)
    assert kill["round_number"] == 1
    assert kill["seconds_into_round"] == pytest.approx(21.5)
    assert kill["tick"] == parsed.rounds.iloc[0]["freeze_end_tick"] + round(21.5 * 64)
    assert kill["attacker_steamid"] == sid("a1")
    assert kill["victim_steamid"] == sid("b1")
    assert kill["assister_steamid"] == sid("a2")
    assert (kill["attacker_side"], kill["victim_side"]) == ("ct", "t")
    assert kill["weapon"] == "awp"
    for flag in ("headshot", "penetrated", "through_smoke", "noscope", "attacker_blind", "flash_assisted"):
        assert kill[flag] is True or kill[flag] == True  # noqa: E712 - numpy bools
    assert (kill["attacker_place"], kill["victim_place"]) == ("Window", "Middle")
    assert (kill["victim_x"], kill["victim_y"], kill["victim_z"]) == (300.0, 400.0, 0.0)
    assert kill["distance"] == pytest.approx(500.0)
    assert kill["is_opening"] and kill["is_enemy_kill"]
    assert not kill["is_trade"] and not kill["was_traded"]


def test_steam_ids_keep_all_17_digits() -> None:
    """A float64 cannot hold these ids: they must come through as exact Python ints."""
    parsed = one_round(kills=[K(20, "a2", "b3", assister="a4")])
    (kill,) = kills_of(parsed)
    assert kill["attacker_steamid"] == 76561198000000003
    assert kill["victim_steamid"] == 76561198100000005
    assert kill["assister_steamid"] == 76561198000000007
    assert all(isinstance(kill[c], int) for c in ("attacker_steamid", "victim_steamid", "assister_steamid"))
    assert float(76561198000000003) != 76561198000000003  # the precision trap this guards against
    assert sorted(parsed.steamids) == sorted(STEAMIDS.values())
    assert set(parsed.player_rounds["steamid"]) == set(STEAMIDS.values())
    assert set(parsed.positions["steamid"]) == set(STEAMIDS.values())


def test_weapon_names_are_normalised() -> None:
    parsed = one_round(
        kills=[K(20, "a1", "b1", weapon="weapon_AK47"), K(22, "a1", "b2", weapon="m4a1_silencer")]
    )
    assert [k["weapon"] for k in kills_of(parsed)] == ["ak47", "m4a1_silencer"]


def test_opening_kill_is_the_first_enemy_kill() -> None:
    parsed = one_round(
        kills=[
            K(5, None, "a5", weapon="world"),  # fell to death: not a kill for anybody
            K(8, "b1", "b2", weapon="hegrenade"),  # team kill
            K(20, "b3", "a1"),
            K(30, "a2", "b3"),
        ]
    )
    rows = kills_of(parsed)
    assert [r["is_opening"] for r in rows] == [False, False, True, False]
    assert [r["is_enemy_kill"] for r in rows] == [False, False, True, True]
    assert pd.isna(rows[0]["attacker_steamid"]) and pd.isna(rows[0]["attacker_place"])
    assert player_round(parsed, "b3")["opening_kill"]
    assert player_round(parsed, "a1")["opening_death"]
    assert not player_round(parsed, "a5")["opening_death"]
    # the team killer gets no kill, the victims all get a death
    assert player_round(parsed, "b1")["kills"] == 0
    assert player_round(parsed, "b2")["deaths"] == 1
    assert player_round(parsed, "a5")["deaths"] == 1
    assert player_round(parsed, "b3")["kills"] == 1


def test_trade_window_boundary_is_inclusive() -> None:
    """5.0 s (320 ticks) is a trade, one tick more is not."""
    exact = one_round(kills=[K(20.0, "b1", "a1"), K(25.0, "a2", "b1")])
    first, second = kills_of(exact)
    assert first["was_traded"] and not first["is_trade"]
    assert second["is_trade"] and not second["was_traded"]

    late = one_round(kills=[K(20.0, "b1", "a1"), K(25.0 + 1 / 64, "a2", "b1")])
    first, second = kills_of(late)
    assert not first["was_traded"] and not second["is_trade"]


def test_trade_window_follows_the_settings() -> None:
    builder = DemoBuilder()
    builder.add_round("CT", kills=[K(20.0, "b1", "a1"), K(23.0, "a2", "b1")])
    builder.add_rounds("C" * 12)
    assert kills_of(parse(builder.build(), DemoSettings(trade_window_s=5.0)))[1]["is_trade"]
    assert not kills_of(parse(builder.build(), DemoSettings(trade_window_s=2.0)))[1]["is_trade"]


def test_trade_needs_the_killer_to_die_to_a_teammate_of_the_victim() -> None:
    parsed = one_round(
        kills=[
            K(20.0, "b1", "a1"),
            K(21.0, "a2", "b2"),  # kills somebody else: not a trade
            K(22.0, "b3", "b1"),  # b1 dies to his own team mate: not a trade
            K(23.0, "a3", "b4"),
        ]
    )
    assert [k["is_trade"] for k in kills_of(parsed)] == [False] * 4
    assert [k["was_traded"] for k in kills_of(parsed)] == [False] * 4


def test_multiple_deaths_can_be_avenged_by_one_trade_kill() -> None:
    parsed = one_round(
        kills=[
            K(20.0, "b1", "a1"),
            K(22.0, "b1", "a2"),
            K(24.0, "a3", "b1"),  # avenges a1 (4 s) and a2 (2 s)
            K(40.0, "b2", "a3"),
            K(50.0, "a4", "b2"),  # 10 s later: no trade
        ]
    )
    rows = kills_of(parsed)
    assert [r["was_traded"] for r in rows] == [True, True, False, False, False]
    assert [r["is_trade"] for r in rows] == [False, False, True, False, False]
    assert player_round(parsed, "a1")["kast"] and player_round(parsed, "a2")["kast"]


def test_kills_between_round_end_and_the_next_round_belong_to_the_round() -> None:
    builder = DemoBuilder()
    builder.add_round("T", duration=40.0, kills=[K(30, "b1", "a1"), K(44, "b1", "a2")])
    second = builder.add_round("CT", kills=[K(10, "a1", "b1")])
    builder.add_rounds("C" * 11)
    parsed = parse(builder.build())

    first_round = kills_of(parsed, 1)
    assert [k["seconds_into_round"] for k in first_round] == [pytest.approx(30.0), pytest.approx(44.0)]
    assert [k["is_opening"] for k in first_round] == [True, False]
    assert player_round(parsed, "b1", 1)["kills"] == 2
    (next_kill,) = kills_of(parsed, 2)
    assert next_kill["tick"] == second.freeze_tick + 10 * 64


# --------------------------------------------------------------------------------------- damage


def test_damage_is_capped_by_the_victims_health() -> None:
    parsed = one_round(
        hurts=[
            H(10, "a1", "b1", 27, 73),
            H(11, "a2", "b1", 40, 33),
            H(12, "a1", "b1", 111, 0, weapon="awp"),  # overkill: only 33 health were left
            H(13, "a3", "b2", 450, 0, weapon="awp"),  # 450 damage on a healthy player counts as 100
        ],
        kills=[K(12, "a1", "b1", weapon="awp"), K(13, "a3", "b2", weapon="awp")],
    )
    assert player_round(parsed, "a1")["damage"] == 27 + 33
    assert player_round(parsed, "a2")["damage"] == 40
    assert player_round(parsed, "a3")["damage"] == 100
    total_on_b1 = player_round(parsed, "a1")["damage"] + player_round(parsed, "a2")["damage"]
    assert total_on_b1 == 100


def test_damage_cap_works_without_the_health_field() -> None:
    parsed = one_round(hurts=[H(10, "a1", "b1", 80, None), H(11, "a1", "b1", 80, None)])
    assert player_round(parsed, "a1")["damage"] == 100


def test_team_damage_self_damage_and_world_damage_are_ignored() -> None:
    parsed = one_round(
        hurts=[
            H(10, "a1", "a2", 60, 40, weapon="hegrenade"),  # team damage
            H(11, "a1", "a1", 30, 70, weapon="hegrenade"),  # self damage
            H(12, None, "b1", 20, 80, weapon="world"),  # fall damage
            H(13, "a1", "b1", 90, 0),  # b1 had 80 left after the fall
            H(14, "b2", "a2", 50, 0),  # a2 had 40 left after the team damage
        ]
    )
    assert player_round(parsed, "a1")["damage"] == 80
    assert player_round(parsed, "a1")["utility_damage"] == 0
    assert player_round(parsed, "b2")["damage"] == 40


def test_utility_damage_counts_he_and_fire() -> None:
    parsed = one_round(
        hurts=[
            H(10, "a1", "b1", 45, 55, weapon="hegrenade"),
            H(11, "a1", "b2", 8, 92, weapon="inferno"),
            H(12, "a1", "b2", 8, 84, weapon="inferno"),
            H(13, "a1", "b3", 30, 70, weapon="ak47"),
            H(14, "a2", "b4", 1, 99, weapon="smokegrenade"),  # a smoke hitting somebody is not utility damage
            H(15, "b5", "a3", 12, 88, weapon="molotov"),
        ]
    )
    a1 = player_round(parsed, "a1")
    assert (a1["damage"], a1["utility_damage"]) == (91, 61)
    assert player_round(parsed, "a2")["utility_damage"] == 0
    assert player_round(parsed, "b5")["utility_damage"] == 12


def test_damage_resets_every_round() -> None:
    builder = DemoBuilder()
    builder.add_round("CT", hurts=[H(10, "a1", "b1", 100, 0)], kills=[K(10, "a1", "b1")])
    builder.add_round("CT", hurts=[H(10, "a1", "b1", 100, 0)], kills=[K(10, "a1", "b1")])
    builder.add_rounds("C" * 11)
    parsed = parse(builder.build())
    assert player_round(parsed, "a1", 1)["damage"] == 100
    assert player_round(parsed, "a1", 2)["damage"] == 100


# -------------------------------------------------------------------------------------- utility


FLASH_SCRIPT: dict[str, Any] = {
    "kills": [K(30, "a1", "b3")],
    "blinds": [
        B(10, "a1", "b1", 2.5),
        B(10, "a1", "b2", 0.4),  # barely blinded: below the minimum duration
        B(10, "a1", "a2", 3.0),  # team flash
        B(10, "a1", "a1", 3.0),  # self flash
        B(35, "a1", "b3", 3.0),  # b3 is already dead
        B(40, "b4", "a3", 1.0),
    ],
    "nades": [
        N(10, "flash", "a1"),
        N(35, "flash", "a1"),
        N(40, "flash", "b4"),
        N(50, "flash", "b4"),  # blinds nobody
        N(12, "smoke", "a1"),
        N(14, "he", "a2"),
        N(15, "molotov", "b1"),
        N(16, "molotov", "b1"),
        N(18, "smoke", "b2"),
    ],
}


@pytest.mark.parametrize("has_player_blind", [False, True], ids=["gotv_flash_duration", "player_blind_event"])
def test_flashes_and_thrown_utility(has_player_blind: bool) -> None:
    """GOTV demos have no player_blind event: both sources must give the same numbers."""
    builder = DemoBuilder()
    builder.add_round("CT", **FLASH_SCRIPT)
    builder.add_rounds("C" * 12)
    parser = builder.build(has_player_blind=has_player_blind)
    parsed = parse(parser)

    a1 = player_round(parsed, "a1")
    assert a1["enemies_flashed"] == 1
    assert (a1["flashes_thrown"], a1["smokes_thrown"], a1["he_thrown"], a1["molotovs_thrown"]) == (2, 1, 0, 0)
    assert player_round(parsed, "a2")["he_thrown"] == 1
    assert player_round(parsed, "b1")["molotovs_thrown"] == 2
    assert player_round(parsed, "b2")["smokes_thrown"] == 1
    b4 = player_round(parsed, "b4")
    assert (b4["enemies_flashed"], b4["flashes_thrown"]) == (1, 2)
    assert parsed.player_rounds["enemies_flashed"].sum() == 2
    assert parsed.warnings == []
    asked_flash_duration = any(
        name == "parse_ticks" and "flash_duration" in args[0] for name, args in parser.calls
    )
    assert asked_flash_duration is not has_player_blind


def test_minimum_blind_duration_is_an_option() -> None:
    builder = DemoBuilder()
    builder.add_round("CT", blinds=[B(10, "a1", "b2", 0.4)], nades=[N(10, "flash", "a1")])
    builder.add_rounds("C" * 12)
    strict = parse(builder.build())
    loose = parse(builder.build(), options=ParseOptions(min_blind_duration_s=0.0))
    assert player_round(strict, "a1")["enemies_flashed"] == 0
    assert player_round(loose, "a1")["enemies_flashed"] == 1


def test_flash_duration_only_counts_rising_edges_at_a_detonation() -> None:
    parsed = one_round(
        blinds=[
            B(10, "a1", "b1", 4.0),
            B(12, "a2", "b1", 5.0),  # blinded again, longer, while still blind: counts for a2
            B(20, "a1", "b2", 3.0),
        ],
        nades=[
            N(10, "flash", "a1"),
            N(11, "flash", "a3"),  # pops while b1 is still blind from a1's flash: no new blind
            N(12, "flash", "a2"),
            N(20, "flash", "a1"),
            N(20 + 1 / 64, "flash", "a4"),  # pops one tick later: b2's blind is not credited twice
        ],
    )
    flashed = {name: player_round(parsed, name)["enemies_flashed"] for name in ("a1", "a2", "a3", "a4")}
    assert flashed == {"a1": 2, "a2": 1, "a3": 0, "a4": 0}


def test_flash_duration_with_missing_ticks() -> None:
    """Half-rate demo: the detonation tick itself may be missing, the neighbours are used."""
    builder = DemoBuilder(first_tick=1001)
    builder.add_round("CT", blinds=[B(10, "a1", "b1", 2.0)], nades=[N(10, "flash", "a1")])
    builder.add_rounds("C" * 12)
    parsed = parse(builder.build(tick_step=2))
    assert player_round(parsed, "a1")["enemies_flashed"] == 1


def test_missing_flash_duration_prop_is_reported_not_fatal() -> None:
    builder = DemoBuilder()
    builder.add_round("CT", blinds=[B(10, "a1", "b1", 2.0)], nades=[N(10, "flash", "a1")])
    builder.add_rounds("C" * 12)
    parsed = parse(builder.build(fail_flash_duration=True))
    assert parsed.n_rounds == 13
    assert player_round(parsed, "a1")["enemies_flashed"] == 0
    assert player_round(parsed, "a1")["flashes_thrown"] == 1
    assert any("flash_duration unavailable" in w for w in parsed.warnings)


def test_flashes_outside_the_match_are_not_queried() -> None:
    builder = DemoBuilder()
    builder.add_warmup(nades=[N(5, "flash", "a1")], blinds=[B(5, "a1", "b1", 3.0)])
    builder.add_rounds("C" * 13)
    parser = builder.build()
    parsed = parse(parser)
    assert parsed.player_rounds["enemies_flashed"].sum() == 0
    assert not any(name == "parse_ticks" and "flash_duration" in args[0] for name, args in parser.calls)


# ----------------------------------------------------------------------------------------- KAST


def test_kast_kill_assist_survive_trade() -> None:
    parsed = one_round(
        winner="T",
        kills=[
            K(20, "b1", "a1"),  # a1: dies untraded, nothing else            -> no KAST
            K(30, "b2", "a2"),  # a2: dies, traded by a3                      -> KAST (T)
            K(32, "a3", "b2", assister="a4"),  # a3 kills -> K, a4 assists -> A
            K(40, "b1", "a3"),
            K(50, "b1", "a4"),
            K(60, "b1", "a5"),  # a5: dies untraded without impact           -> no KAST
        ],
    )
    kast = {name: bool(player_round(parsed, name)["kast"]) for name in TEAM_A + TEAM_B}
    assert kast == {
        "a1": False,
        "a2": True,
        "a3": True,
        "a4": True,
        "a5": False,
        "b1": True,  # kills and survives
        "b2": True,  # kill
        "b3": True,  # survived
        "b4": True,
        "b5": True,
    }
    a4 = player_round(parsed, "a4")
    assert (a4["kills"], a4["assists"], a4["deaths"], a4["survived"]) == (0, 1, 1, False)
    assert player_round(parsed, "b3")["survived"]
    assert list(parsed.player_rounds.columns) == list(PLAYER_ROUND_COLUMNS)


def test_pure_flash_assist_is_an_assist_but_not_the_a_of_kast() -> None:
    """HLTV's KAST counts kill assists only; the assist column includes flash assists."""
    parsed = one_round(
        winner="T",
        kills=[
            K(20, "a1", "b1", assister="a2", assistedflash=True),  # a2: flash assist only
            K(21, "a1", "b2", assister="a3"),  # a3: damage assist
            K(30, "b3", "a2"),
            K(40, "b3", "a3"),
            K(50, "b3", "a1"),
        ],
    )
    a2, a3 = player_round(parsed, "a2"), player_round(parsed, "a3")
    assert (a2["assists"], a2["kast"]) == (1, False)
    assert (a3["assists"], a3["kast"]) == (1, True)
    assert kills_of(parsed)[0]["flash_assisted"]


def test_deaths_during_freeze_time_are_ignored() -> None:
    parsed = one_round(kills=[K(-10, "b1", "b1", weapon="world"), K(20, "a1", "b2")])
    assert [k["weapon"] for k in kills_of(parsed)] == ["ak47"]
    b1 = player_round(parsed, "b1")
    assert (b1["deaths"], b1["survived"]) == (0, True)


def test_assist_on_a_team_kill_does_not_count() -> None:
    parsed = one_round(kills=[K(20, "a1", "a2", assister="a3"), K(25, "b1", "a1", assister="b2")])
    assert player_round(parsed, "a3")["assists"] == 0
    assert player_round(parsed, "b2")["assists"] == 1


# -------------------------------------------------------------------------------------- clutches


def test_clutch_won_1v2() -> None:
    parsed = one_round(
        winner="CT",
        kills=[
            K(20, "b1", "a1"),
            K(21, "b1", "a2"),
            K(22, "a3", "b1"),
            K(23, "a3", "b2"),
            K(24, "a3", "b3"),
            K(30, "b4", "a3"),
            K(31, "b4", "a4"),  # a5 is now alone against b4 and b5
            K(40, "a5", "b4"),
            K(45, "a5", "b5"),
        ],
    )
    a5 = player_round(parsed, "a5")
    assert a5["clutch_situation"] == "1v2"
    assert a5["clutch_won"] is True or a5["clutch_won"] == True  # noqa: E712
    # b5 ended up alone too (1v1 after b4 died) and lost
    b5 = player_round(parsed, "b5")
    assert b5["clutch_situation"] == "1v1"
    assert not b5["clutch_won"]
    for name in ("a1", "a2", "a3", "a4", "b1", "b2", "b3", "b4"):
        assert pd.isna(player_round(parsed, name)["clutch_situation"])
        assert pd.isna(player_round(parsed, name)["clutch_won"])


def test_clutch_lost_and_size_is_fixed_when_the_player_becomes_last_alive() -> None:
    parsed = one_round(
        winner="T",
        kills=[
            K(20, "b1", "a1"),
            K(21, "b1", "a2"),
            K(22, "b1", "a3"),
            K(23, "b1", "a4"),  # a5 alone against five
            K(30, "a5", "b1"),
            K(31, "a5", "b2"),
            K(40, "b3", "a5"),
        ],
    )
    a5 = player_round(parsed, "a5")
    assert a5["clutch_situation"] == "1v5"  # not 1v3: the size at the moment the clutch started
    assert not a5["clutch_won"]
    assert all(pd.isna(player_round(parsed, name)["clutch_situation"]) for name in TEAM_B)


def test_clutch_can_be_won_without_killing_everybody() -> None:
    """The last CT wins a 1v2 by defusing / running the clock: the round result decides."""
    parsed = one_round(
        winner="CT",
        reason="time_ran_out",
        kills=[
            K(20, "b1", "a1"),
            K(21, "b1", "a2"),
            K(22, "b1", "a3"),
            K(23, "b1", "a4"),
            K(30, "a5", "b1"),
            K(31, "a5", "b2"),
            K(32, "a5", "b3"),
        ],
    )
    a5 = player_round(parsed, "a5")
    assert a5["clutch_situation"] == "1v5" and a5["clutch_won"]


def test_post_round_deaths_do_not_create_clutches() -> None:
    parsed = one_round(
        winner="CT",
        duration=30.0,
        kills=[K(20, "a1", "b1"), K(31, "a1", "b2"), K(32, "a1", "b3"), K(33, "a1", "b4")],
    )
    assert parsed.player_rounds["clutch_situation"].isna().all()


# ------------------------------------------------------------------------------ rosters / sides


def test_players_table_and_sides_per_round() -> None:
    parsed = parse(standard_match().build())
    assert list(parsed.players.columns) == list(PLAYER_COLUMNS)
    players = {row["name"]: row for row in parsed.players.to_dict("records")}
    assert set(players) == set(TEAM_A + TEAM_B)
    for name in TEAM_A:
        assert players[name]["roster"] == "a" and players[name]["start_side"] == "ct"
        assert players[name]["team_name"] == "Alpha"
        assert players[name]["steamid"] == sid(name)
        assert players[name]["rounds_played"] == 18
    for name in TEAM_B:
        assert players[name]["roster"] == "b" and players[name]["start_side"] == "t"
        assert players[name]["team_name"] == "Bravo"
    assert player_round(parsed, "a1", 12)["side"] == "ct"
    assert player_round(parsed, "a1", 13)["side"] == "t"
    assert player_round(parsed, "b1", 13)["side"] == "ct"
    assert player_round(parsed, "a1", 13)["roster"] == "a"
    assert len(parsed.player_rounds) == 18 * 10


def test_substitute_joins_the_roster_of_his_side() -> None:
    builder = DemoBuilder()
    builder.add_rounds("CCC")
    builder.add_rounds("C" * 10, absent=["a5"])  # a5 drops out, the team plays 4 v 5
    parsed = parse(builder.build())
    players = {row["name"]: row for row in parsed.players.to_dict("records")}
    assert players["a5"]["rounds_played"] == 3
    assert players["a1"]["rounds_played"] == 13
    assert parsed.rounds["ct_players"].tolist() == [5, 5, 5] + [4] * 10


def test_player_missing_at_freeze_end_still_gets_a_stat_line_from_his_kill() -> None:
    """A player who reconnects during the round is absent from the freeze-end snapshot."""
    builder = DemoBuilder()
    builder.add_rounds("C")
    rnd = builder.add_round("CT", kills=[K(30, "a5", "b1")])
    builder.add_rounds("C" * 11)
    parser = builder.build()
    original = parser.player_state

    def state(name: str, tick: int) -> dict[str, Any] | None:
        if name == "a5" and rnd.start_tick <= tick <= rnd.freeze_tick + 10:
            return None
        return original(name, tick)

    parser.player_state = state  # type: ignore[method-assign]
    parsed = parse(parser)
    a5 = player_round(parsed, "a5", 2)
    assert (a5["kills"], a5["side"], a5["roster"]) == (1, "ct", "a")
    assert pd.isna(a5["equipment_value"])


def test_primary_weapon_from_the_inventory() -> None:
    parsed = one_round(
        inventory={
            "a1": ["Knife", "USP-S", "AWP", "Smoke Grenade"],
            "a2": ["Knife", "Desert Eagle", "Flashbang"],
            "a3": ["Knife", "M4A1-S", "USP-S"],
            "b1": ["Knife", "AK-47", "Glock-18", "Molotov"],
            "b2": ["Knife"],
        }
    )
    assert player_round(parsed, "a1")["primary_weapon"] == "awp"
    assert player_round(parsed, "a2")["primary_weapon"] == "deagle"
    assert player_round(parsed, "a3")["primary_weapon"] == "m4a1_silencer"
    assert player_round(parsed, "b1")["primary_weapon"] == "ak47"
    assert pd.isna(player_round(parsed, "b2")["primary_weapon"])


# ---------------------------------------------------------------------------- position samples


def test_position_samples_at_the_configured_seconds() -> None:
    builder = DemoBuilder()
    builder.add_round(
        "CT",
        duration=50.0,
        kills=[K(25, "b1", "a2"), K(45, "a1", "b1")],
        positions={
            "a1": [
                (0, ("CTSpawn", 0.0, 0.0, 0.0)),
                (15, ("Connector", 10.0, 20.0, 30.0)),
                (25, ("Jungle", 1.0, 2.0, 3.0)),
            ],
            "b1": ("TRamp", -5.0, -6.0, -7.0),
        },
    )
    builder.add_rounds("C" * 12)
    parsed = parse(builder.build(), DemoSettings(position_sample_seconds=[10, 20, 30, 45, 60]))

    assert list(parsed.positions.columns) == list(POSITION_COLUMNS)
    first = parsed.positions[parsed.positions["round_number"] == 1]
    # the round lasts 50 s: there is no 60 s sample
    assert sorted(first["seconds_into_round"].unique()) == [10, 20, 30, 45]
    a1 = first[first["steamid"] == sid("a1")].set_index("seconds_into_round")
    assert a1["place"].to_dict() == {10: "CTSpawn", 20: "Connector", 30: "Jungle", 45: "Jungle"}
    assert (a1.loc[20, "x"], a1.loc[20, "y"], a1.loc[20, "z"]) == (10.0, 20.0, 30.0)
    assert (a1["side"] == "ct").all() and a1["is_alive"].all()
    # a2 dies at 25 s: sampled at 10 and 20 only
    a2 = first[first["steamid"] == sid("a2")]
    assert sorted(a2["seconds_into_round"]) == [10, 20]
    # b1 dies exactly at the 45 s sample tick: dead players are never sampled
    b1 = first[first["steamid"] == sid("b1")]
    assert sorted(b1["seconds_into_round"]) == [10, 20, 30]
    assert (b1["place"] == "TRamp").all() and (b1["side"] == "t").all()
    # everybody else is alive throughout
    assert len(first) == 8 * 4 + 2 + 3


def test_position_sample_seconds_follow_the_settings() -> None:
    parser = standard_match().build()
    parsed = parse(parser, DemoSettings(position_sample_seconds=[20, 5, 5]))
    assert sorted(parsed.positions["seconds_into_round"].unique()) == [5, 20]
    assert len(parsed.positions) == 18 * 10 * 2
    none = parse(standard_match().build(), DemoSettings(position_sample_seconds=[]))
    assert none.positions.empty and list(none.positions.columns) == list(POSITION_COLUMNS)
    assert none.n_rounds == 18


def test_ticks_missing_from_the_demo_are_bridged() -> None:
    """A demo recorded at half rate only has every second tick: the next available tick is used."""
    builder = DemoBuilder(first_tick=1001)  # freeze end lands on an odd tick
    builder.add_rounds("C" * 13, equip=3000)
    parser = builder.build(tick_step=2)
    parsed = parse(parser, DemoSettings(position_sample_seconds=[10]))
    assert parsed.rounds["ct_equipment_value"].tolist() == [15000] * 13
    assert len(parsed.positions) == 13 * 10
    assert parsed.warnings == []


def test_missing_player_state_is_reported_but_not_fatal() -> None:
    builder = DemoBuilder(first_tick=1001)
    builder.add_rounds("C" * 13)
    parser = builder.build(tick_step=64)  # (almost) no requested tick exists in the demo
    parsed = parse(parser, options=ParseOptions(tick_search_window=1))
    assert parsed.n_rounds == 13
    assert parsed.rounds["winner_side"].tolist() == ["ct"] * 13
    assert any("no player state at freeze end" in w for w in parsed.warnings)


def test_distance_needs_both_positions() -> None:
    parsed = one_round(kills=[K(20, None, "b1", weapon="planted_c4"), K(21, "a1", "b2")])
    world, normal = kills_of(parsed)
    assert pd.isna(world["distance"])
    assert normal["distance"] == pytest.approx(math.dist((0.0, 50.0, 0.0), (600.0, -50.0, 0.0)))
