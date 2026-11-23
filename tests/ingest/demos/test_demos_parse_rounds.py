"""Round cleaning, end reasons, economy / buy types and round-level facts of ``parse_demo``."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from cs2bot.config import DemoSettings
from cs2bot.domain.enums import BuyType, RoundEndReason, Side
from cs2bot.ingest.demos.errors import DemoParseError
from cs2bot.ingest.demos.parse import (
    ECO_MAX_AVG_EQUIPMENT,
    EVENT_NAMES,
    FORCE_MAX_AVG_EQUIPMENT,
    ROUND_COLUMNS,
    ParsedDemo,
    ParseOptions,
    classify_buy_type,
    demoparser_version,
    is_knife_weapon,
    map_round_end_reason,
    parse_demo,
    parse_side,
)
from tests.ingest.demos.demos_builders import (
    SITE_A_ID,
    SITE_B_ID,
    TEAM_A,
    TEAM_B,
    DemoBuilder,
    FakeParser,
    H,
    K,
    factory_for,
    standard_match,
)


def parse(parser: FakeParser, settings: DemoSettings | None = None, **kwargs: Any) -> ParsedDemo:
    return parse_demo(
        "match-m1-mirage.dem", settings or DemoSettings(), parser_factory=factory_for(parser), **kwargs
    )


def round_row(parsed: ParsedDemo, number: int) -> dict[str, Any]:
    rows = parsed.rounds[parsed.rounds["round_number"] == number].to_dict("records")
    assert len(rows) == 1
    return rows[0]


# --------------------------------------------------------------------------------------- basics


def test_plain_match_is_parsed_round_by_round() -> None:
    parser = standard_match().build()
    parsed = parse(parser)

    assert parsed.map_name == "mirage"
    assert parsed.header["map_name"] == "de_mirage"
    assert parsed.parser_version == demoparser_version()
    assert parsed.n_rounds == 18
    assert list(parsed.rounds.columns) == list(ROUND_COLUMNS)
    assert parsed.rounds["round_number"].tolist() == list(range(1, 19))
    assert parsed.rounds["winner_side"].tolist() == ["ct"] * 7 + ["t"] * 11
    assert parsed.roster_scores == {"a": 13, "b": 5}
    assert parsed.roster_names == {"a": "Alpha", "b": "Bravo"}
    # roster "a" (the five that started as CT) is CT in the first half and T in the second
    assert parsed.rounds["ct_roster"].tolist() == ["a"] * 12 + ["b"] * 6
    assert not parsed.rounds["is_overtime"].any()
    assert parsed.dropped_rounds == {"warmup": 0, "replayed": 0, "knife": 0, "restarted": 0}
    assert parsed.warnings == []


def test_events_and_props_are_requested_in_one_batch() -> None:
    parser = standard_match().build()
    parse(parser)

    batch = [args for name, args in parser.calls if name == "parse_events"]
    assert len(batch) == 1
    names, player_props, other_props = batch[0]
    assert set(names) == set(EVENT_NAMES)
    assert {"X", "Y", "Z", "last_place_name", "team_num"} <= set(player_props)
    assert {"total_rounds_played", "is_warmup_period"} <= set(other_props)
    # player state is read once, only for the ticks that are needed (never the whole demo)
    tick_calls = [args for name, args in parser.calls if name == "parse_ticks"]
    assert len(tick_calls) == 1
    assert 0 < len(tick_calls[0][1]) < 18 * 6 * 4 + 1


def test_round_timing_and_post_round_window() -> None:
    builder = DemoBuilder()
    first = builder.add_round("CT", duration=70.0)
    second = builder.add_round("T", duration=95.5)
    builder.add_rounds("C" * 12)
    parsed = parse(builder.build())

    row = round_row(parsed, 1)
    assert row["start_tick"] == first.start_tick
    assert row["freeze_end_tick"] == first.freeze_tick
    assert row["end_tick"] == first.end_tick
    # round_officially_ended shares its tick with the next round_start: the round owns everything before
    assert row["official_end_tick"] == second.start_tick - 1
    assert row["duration_s"] == pytest.approx(70.0)
    assert round_row(parsed, 2)["duration_s"] == pytest.approx(95.5)
    # the last round has no following round: the post-round time is assumed (7 s)
    last = round_row(parsed, 14)
    assert last["official_end_tick"] == last["end_tick"] + 7 * 64


# ------------------------------------------------------------------------------- round cleaning


def test_warmup_rounds_are_dropped() -> None:
    builder = DemoBuilder()
    builder.add_warmup(kills=[K(5, "a1", "b1")])
    builder.add_warmup()
    builder.add_rounds("C" * 13)
    parsed = parse(builder.build())

    assert parsed.n_rounds == 13
    assert parsed.dropped_rounds["warmup"] == 2
    assert parsed.rounds["round_number"].tolist() == list(range(1, 14))
    assert parsed.kills.empty  # the warmup kill belongs to no round


def test_knife_round_followed_by_restart_is_dropped() -> None:
    builder = DemoBuilder()
    builder.add_warmup()
    builder.add_knife_round("T")
    builder.restart()
    builder.add_rounds("CCCCCCCTTTTT")
    builder.swap_sides()
    builder.add_rounds("TTTTTT")
    parsed = parse(builder.build())

    assert parsed.n_rounds == 18
    assert parsed.rounds["round_number"].tolist() == list(range(1, 19))
    assert parsed.dropped_rounds["knife"] == 1
    assert parsed.roster_scores == {"a": 13, "b": 5}
    assert not parsed.kills["weapon"].isin(["knife"]).any()
    assert round_row(parsed, 1)["ct_buy_type"] == "pistol"


def test_knife_round_without_restart_is_dropped_and_rounds_are_renumbered() -> None:
    """Some servers count the knife round: the real round 1 then has total_rounds_played == 1."""
    builder = DemoBuilder()
    builder.add_knife_round("CT")  # no restart afterwards
    builder.add_rounds("C" * 13)
    parsed = parse(builder.build())

    assert parsed.dropped_rounds["knife"] == 1
    assert parsed.rounds["round_number"].tolist() == list(range(1, 14))
    assert round_row(parsed, 1)["ct_buy_type"] == "pistol"
    assert round_row(parsed, 13)["t_buy_type"] == "pistol"
    assert parsed.roster_scores == {"a": 13, "b": 0}


def test_a_gun_round_is_never_mistaken_for_a_knife_round() -> None:
    builder = DemoBuilder()
    builder.add_round(
        "CT", kills=[K(20, "a1", "b1", weapon="knife"), K(25, "a2", "b2", weapon="usp_silencer")]
    )
    builder.add_rounds("C" * 12)
    parsed = parse(builder.build())
    assert parsed.dropped_rounds["knife"] == 0
    assert parsed.n_rounds == 13


def test_rounds_before_the_final_restart_are_dropped() -> None:
    """A false start ("live on three"): three rounds, restart, then the real match."""
    builder = DemoBuilder()
    builder.add_rounds("TTT", kills=[K(30, "b1", "a1")])
    builder.restart()
    builder.add_rounds("C" * 13)
    parsed = parse(builder.build())

    assert parsed.n_rounds == 13
    assert parsed.dropped_rounds["restarted"] == 3
    assert parsed.roster_scores == {"a": 13, "b": 0}
    assert parsed.kills.empty


def test_round_replayed_after_a_backup_restore_replaces_the_original() -> None:
    builder = DemoBuilder()
    builder.add_rounds("CCCC")
    builder.add_round("T", kills=[K(30, "b1", "a1", weapon="deagle")])  # round 5, later annulled
    builder.add_round("T")  # round 6, annulled as well
    builder.restore(4)  # technical issue: the admins load the backup of round 5
    builder.add_round("CT", kills=[K(40, "a1", "b1", weapon="awp")])  # round 5 again
    builder.add_rounds("C" * 8)
    parsed = parse(builder.build())

    assert parsed.rounds["round_number"].tolist() == list(range(1, 14))
    assert parsed.dropped_rounds["replayed"] == 2
    assert round_row(parsed, 5)["winner_side"] == "ct"
    assert parsed.kills["weapon"].tolist() == ["awp"]
    assert parsed.roster_scores == {"a": 13, "b": 0}


def test_backup_restore_as_seen_in_a_real_hltv_demo() -> None:
    """Round 16 is played, round 17 starts, the admins restore round 16: it must count once.

    In the real demo (9z vs RED Canids, Nuke) the restore shows as a winner-less round_end on the tick
    of the new round_start, on which all ten players are also killed by "world". Those are not deaths.
    """
    builder = DemoBuilder()
    builder.add_rounds("CCCCCCTTTTTT")  # 6-6
    builder.swap_sides()
    builder.add_rounds("CCC")  # team B (now CT) leads 9-6
    builder.add_round("CT", "time_ran_out", kills=[K(30, "b1", "a1")])  # round 16, later annulled
    builder.add_round("T", round_end_event=False, freeze_end=False, duration=5.0)  # round 17 never gets going
    builder.restore(15)
    replay = builder.add_round(
        "T", kills=[K(40, "a1", "b1"), K(41, "a1", "b2")], hurts=[H(39, "a1", "b1", 100, 0)], restored=True
    )
    builder.add_rounds("C" * 4)
    parsed = parse(builder.build())

    assert parsed.rounds["round_number"].tolist() == list(range(1, 21))
    assert parsed.dropped_rounds["replayed"] == 1
    row = round_row(parsed, 16)
    assert (row["winner_side"], row["end_reason"]) == ("t", "elimination")
    assert row["start_tick"] == replay.start_tick and row["freeze_end_tick"] == replay.freeze_tick
    assert (row["ct_survivors"], row["t_survivors"]) == (3, 5)
    # only the two real kills of the replay: no kill of the annulled attempt, no restore artefact
    in_round = parsed.kills[parsed.kills["round_number"] == 16]
    assert in_round["weapon"].tolist() == ["ak47", "ak47"]
    assert not parsed.kills["weapon"].isin(["world"]).any()
    stats = parsed.player_rounds[parsed.player_rounds["round_number"] == 16].set_index("name")
    assert stats["deaths"].sum() == 2 and stats["survived"].sum() == 8
    # the artefact's "100 damage by the world" must not eat the real damage of the round
    assert stats.loc["a1", "damage"] == 100 and stats.loc["a1", "kills"] == 2
    assert len(parsed.positions[parsed.positions["round_number"] == 16]) > 0
    assert parsed.roster_scores == {"a": 7, "b": 13}  # with the annulled round it would be 6-14


def test_first_round_keeps_its_start_tick_despite_the_pseudo_round_end() -> None:
    builder = DemoBuilder()
    first = builder.add_round("CT")
    builder.add_rounds("C" * 12)
    with_pseudo = parse(builder.build(pseudo_round_end=True))
    without = parse(builder.build(pseudo_round_end=False))
    for parsed in (with_pseudo, without):
        assert round_row(parsed, 1)["start_tick"] == first.start_tick
        assert parsed.n_rounds == 13


def test_aborted_round_without_round_end_does_not_shift_anything() -> None:
    """A round interrupted by a restore has a freeze end but no round end: its events are ignored."""
    builder = DemoBuilder()
    builder.add_rounds("CC")
    builder.add_round("CT", kills=[K(10, "b1", "a1")], round_end_event=False)  # aborted attempt
    replay = builder.add_round("CT", kills=[K(20, "a1", "b1")])
    builder.add_rounds("C" * 10)
    parsed = parse(builder.build())

    assert parsed.rounds["round_number"].tolist() == list(range(1, 14))
    row = round_row(parsed, 3)
    assert row["freeze_end_tick"] == replay.freeze_tick
    assert row["start_tick"] == replay.start_tick
    assert len(parsed.kills) == 1
    assert parsed.kills["attacker_steamid"].tolist() == [76561198000000001]


def test_stray_round_after_the_match_is_ignored() -> None:
    builder = standard_match()
    builder.restart()
    builder.add_round("T")  # e.g. the server restarts the map after the final round
    parsed = parse(builder.build())

    assert parsed.n_rounds == 18
    assert parsed.roster_scores == {"a": 13, "b": 5}
    assert parsed.dropped_rounds["restarted"] == 1
    assert any("after the match" in w for w in parsed.warnings)


def test_incomplete_demo_is_parsed_with_a_warning() -> None:
    builder = DemoBuilder()
    builder.add_rounds("CCTC")
    parsed = parse(builder.build())
    assert parsed.n_rounds == 4
    assert any("incomplete" in w for w in parsed.warnings)


def test_demo_that_starts_late_keeps_the_true_round_numbers() -> None:
    builder = DemoBuilder()
    builder.restore(11)  # recording starts in round 12
    builder.add_round("CT", equip=4000)
    builder.swap_sides()
    builder.add_rounds("C" * 13, equip=4000)
    parsed = parse(builder.build())

    assert parsed.rounds["round_number"].tolist() == list(range(12, 26))
    assert round_row(parsed, 12)["ct_buy_type"] == "full"
    assert round_row(parsed, 13)["ct_buy_type"] == "pistol"
    assert any("starts at round 12" in w for w in parsed.warnings)


def test_draw_and_game_start_pseudo_rounds_are_skipped() -> None:
    builder = DemoBuilder()
    builder.add_round(None, "game_start", count=False)
    builder.add_round("10", "draw", count=False)
    builder.add_rounds("C" * 13)
    parsed = parse(builder.build())
    assert parsed.n_rounds == 13
    assert parsed.rounds["round_number"].tolist() == list(range(1, 14))


def test_round_without_freeze_end_uses_the_round_start() -> None:
    """A round ended by a surrender vote can lack round_freeze_end (awpy accepts start -> end too)."""
    builder = DemoBuilder()
    builder.add_rounds("C" * 12)
    surrender = builder.add_round("CT", "t_surrender", freeze_end=False, duration=5.0)
    parsed = parse(builder.build())

    row = round_row(parsed, 13)
    assert row["end_reason"] == "surrender"
    assert row["freeze_end_tick"] == surrender.start_tick
    assert parsed.n_rounds == 13


def test_overtime_rounds_are_numbered_and_flagged() -> None:
    builder = DemoBuilder()
    builder.add_rounds("CCCCCCTTTTTT")  # 6-6
    builder.swap_sides()
    builder.add_rounds("CCCCCCTTTTTT")  # 12-12
    builder.add_rounds("CCC", equip=5200)  # overtime, first half
    builder.swap_sides()
    builder.add_rounds("TC", equip=5200)
    parsed = parse(builder.build())

    assert parsed.n_rounds == 29
    assert parsed.rounds["is_overtime"].tolist() == [False] * 24 + [True] * 5
    # overtime has no pistol round: buys are classified by equipment value
    assert parsed.rounds.loc[parsed.rounds["round_number"] >= 25, "ct_buy_type"].tolist() == ["full"] * 5
    assert round_row(parsed, 13)["ct_buy_type"] == "pistol"


def test_no_round_at_all_is_a_parse_error() -> None:
    builder = DemoBuilder()
    builder.add_warmup()
    with pytest.raises(DemoParseError, match="no complete match round"):
        parse(builder.build())


def test_parser_exceptions_become_demo_parse_errors() -> None:
    parser = standard_match().build(fail_events=True)
    with pytest.raises(DemoParseError, match="could not read the events"):
        parse(parser)
    parser = standard_match().build(fail_header=True)
    with pytest.raises(DemoParseError, match="truncated"):
        parse(parser)

    def broken_factory(_path: str) -> FakeParser:
        raise OSError("file not found")

    with pytest.raises(DemoParseError, match="file not found"):
        parse_demo("missing.dem", DemoSettings(), parser_factory=broken_factory)


def test_round_events_missing_from_the_batch_are_queried_one_by_one() -> None:
    parser = standard_match().build(batch_omits_round_events=True)
    parsed = parse(parser)
    assert parsed.n_rounds == 18
    single = {args for name, args in parser.calls if name == "parse_event"}
    assert {"round_start", "round_end", "round_officially_ended"} <= single


def test_missing_optional_props_fall_back_to_the_core_props() -> None:
    parser = standard_match().build(fail_optional_props=True)
    parsed = parse(parser)
    assert parsed.n_rounds == 18
    assert any("optional player props unavailable" in w for w in parsed.warnings)
    # money at round start cannot be known without start_balance / cash_spent_this_round
    assert parsed.rounds["ct_money_start"].isna().all()
    assert parsed.rounds["ct_equipment_value"].notna().all()


# ------------------------------------------------------------------------------- reasons / sides


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bomb_exploded", RoundEndReason.BOMB_EXPLODED),
        ("bomb_defused", RoundEndReason.BOMB_DEFUSED),
        ("t_killed", RoundEndReason.ELIMINATION),
        ("ct_killed", RoundEndReason.ELIMINATION),
        ("time_ran_out", RoundEndReason.TIME_EXPIRED),
        ("t_surrender", RoundEndReason.SURRENDER),
        ("ct_surrender", RoundEndReason.SURRENDER),
        ("CT_KILLED", RoundEndReason.ELIMINATION),
        (1, RoundEndReason.BOMB_EXPLODED),
        (7, RoundEndReason.BOMB_DEFUSED),
        (8, RoundEndReason.ELIMINATION),
        (9, RoundEndReason.ELIMINATION),
        (12, RoundEndReason.TIME_EXPIRED),
        (17, RoundEndReason.SURRENDER),
        (18, RoundEndReason.SURRENDER),
        ("9", RoundEndReason.ELIMINATION),
        (9.0, RoundEndReason.ELIMINATION),
        ("9.0", RoundEndReason.ELIMINATION),
        ("12.0", RoundEndReason.TIME_EXPIRED),
        (9.5, RoundEndReason.OTHER),
        ("hostage_rescued", RoundEndReason.OTHER),
        (10, RoundEndReason.OTHER),
        (None, RoundEndReason.OTHER),
        (float("nan"), RoundEndReason.OTHER),
        ("", RoundEndReason.OTHER),
    ],
)
def test_round_end_reason_mapping(raw: Any, expected: RoundEndReason) -> None:
    assert map_round_end_reason(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CT", Side.CT),
        ("ct", Side.CT),
        (3, Side.CT),
        (3.0, Side.CT),
        ("3", Side.CT),
        ("3.0", Side.CT),
        ("2.0", Side.T),
        (True, None),
        ("T", Side.T),
        ("TERRORIST", Side.T),
        (2, Side.T),
        (1, None),
        (0, None),
        ("16", None),
        ("spectator", None),
        (None, None),
        (float("nan"), None),
        (pd.NA, None),
    ],
)
def test_parse_side(raw: Any, expected: Side | None) -> None:
    assert parse_side(raw) is expected


def test_end_reasons_and_winners_of_a_parsed_demo() -> None:
    builder = DemoBuilder()
    builder.add_round("T", "bomb_exploded", plant=(40.0, "b1", SITE_A_ID))
    builder.add_round("CT", "bomb_defused", plant=(40.0, "b1", SITE_A_ID))
    builder.add_round("CT", "time_ran_out")
    builder.add_round("T", "ct_killed")
    builder.add_round("CT", "t_killed")
    builder.add_rounds("C" * 9)
    parsed = parse(builder.build())

    assert parsed.rounds["end_reason"].tolist()[:5] == [
        "bomb_exploded",
        "bomb_defused",
        "time_expired",
        "elimination",
        "elimination",
    ]
    assert parsed.rounds["reason_raw"].tolist()[:2] == ["bomb_exploded", "bomb_defused"]
    assert parsed.rounds["winner_side"].tolist()[:5] == ["t", "ct", "ct", "t", "ct"]


def test_numeric_reason_and_winner_codes_are_accepted() -> None:
    builder = DemoBuilder()
    builder.add_round("T", "bomb_exploded")
    builder.add_round("CT", "bomb_defused")
    builder.add_round("CT", "time_ran_out")
    builder.add_rounds("C" * 11)
    parsed = parse(builder.build(numeric_reasons=True))
    assert parsed.rounds["end_reason"].tolist()[:3] == ["bomb_exploded", "bomb_defused", "time_expired"]
    assert parsed.rounds["winner_side"].tolist()[:3] == ["t", "ct", "ct"]


def test_winner_is_derived_from_the_reason_when_the_winner_field_is_unusable() -> None:
    builder = DemoBuilder()
    builder.add_round("?", "bomb_defused")
    builder.add_round("", "ct_killed")
    builder.add_rounds("C" * 12)
    parsed = parse(builder.build())
    assert parsed.rounds["winner_side"].tolist()[:2] == ["ct", "t"]


# ------------------------------------------------------------------------------ economy and buys


@pytest.mark.parametrize(
    ("average", "round_number", "expected"),
    [
        (800.0, 1, BuyType.PISTOL),
        (5000.0, 13, BuyType.PISTOL),
        (None, 1, BuyType.PISTOL),
        (0.0, 2, BuyType.ECO),
        (ECO_MAX_AVG_EQUIPMENT - 1, 2, BuyType.ECO),
        (ECO_MAX_AVG_EQUIPMENT, 2, BuyType.FORCE),
        (FORCE_MAX_AVG_EQUIPMENT - 1, 14, BuyType.FORCE),
        (FORCE_MAX_AVG_EQUIPMENT, 14, BuyType.FULL),
        (6000.0, 24, BuyType.FULL),
        (900.0, 25, BuyType.ECO),  # first overtime round: not a pistol round
        (900.0, 28, BuyType.ECO),  # first round of the second overtime half
        (5200.0, 25, BuyType.FULL),
        (None, 5, None),
    ],
)
def test_classify_buy_type(average: float | None, round_number: int, expected: BuyType | None) -> None:
    assert classify_buy_type(average, round_number) is expected


def test_buy_thresholds_match_the_round_model() -> None:
    """The simulator's fallback classification and the demo parser must agree."""
    from cs2bot.models.round_model import classify_buy

    for per_player in (0, 1499, 1500, 2500, 3799, 3800, 5500):
        for number in (1, 2, 12, 13, 14, 24):
            assert classify_buy(per_player * 5, number) == classify_buy_type(float(per_player), number)


def test_round_economy_is_summed_per_side_at_freeze_end() -> None:
    builder = DemoBuilder()
    builder.add_round("CT", equip=800, money=800)
    equip = {**dict.fromkeys(TEAM_A, 5000), **dict.fromkeys(TEAM_B, 1000), "b1": 2400}
    money = {**dict.fromkeys(TEAM_A, 6000), **dict.fromkeys(TEAM_B, 1900)}
    builder.add_round("CT", equip=equip, money=money)
    half = {**dict.fromkeys(TEAM_A, 2600), **dict.fromkeys(TEAM_B, 3900)}
    builder.add_round("T", equip=half, money=4000)
    builder.add_rounds("C" * 11)
    parsed = parse(builder.build())

    first = round_row(parsed, 1)
    assert (first["ct_equipment_value"], first["t_equipment_value"]) == (4000, 4000)
    assert (first["ct_money_start"], first["t_money_start"]) == (4000, 4000)
    assert (first["ct_buy_type"], first["t_buy_type"]) == ("pistol", "pistol")
    second = round_row(parsed, 2)
    assert (second["ct_equipment_value"], second["t_equipment_value"]) == (25000, 6400)
    assert (second["ct_money_start"], second["t_money_start"]) == (30000, 9500)
    assert (second["ct_buy_type"], second["t_buy_type"]) == ("full", "eco")  # T average 1280
    third = round_row(parsed, 3)
    assert (third["ct_buy_type"], third["t_buy_type"]) == ("force", "full")
    # player level
    b1 = parsed.player_rounds[
        (parsed.player_rounds["round_number"] == 2) & (parsed.player_rounds["name"] == "b1")
    ].iloc[0]
    assert (b1["equipment_value"], b1["money_start"], b1["side"]) == (2400, 1900, "t")


def test_buy_type_uses_the_average_of_the_players_present() -> None:
    """Four players with full equipment are a full buy even though the team total is lower."""
    builder = DemoBuilder()
    builder.add_round("CT")
    builder.add_round("CT", equip=4000, absent=["a5"])
    builder.add_rounds("C" * 11)
    parsed = parse(builder.build())
    row = round_row(parsed, 2)
    assert row["ct_players"] == 4 and row["t_players"] == 5
    assert row["ct_equipment_value"] == 16000
    assert row["ct_buy_type"] == "full"


# ------------------------------------------------------------------------------ bomb / survivors


def test_bomb_plant_and_site() -> None:
    site_a = ("BombsiteA", 10.0, 20.0, 0.0)
    site_b = ("BombsiteB", -900.0, 40.0, 0.0)
    builder = DemoBuilder()
    builder.add_round("T", "bomb_exploded", plant=(45.0, "b1", SITE_A_ID), positions={"b1": site_a})
    builder.add_round("CT", "bomb_defused", plant=(50.0, "b2", SITE_B_ID), positions={"b2": site_b})
    builder.add_round("CT")
    # planted from the edge of a neighbouring area: the site id is resolved through the other plants
    builder.add_round("T", plant=(50.0, "b3", SITE_B_ID), positions={"b3": ("Apartments", 0.0, 0.0, 0.0)})
    # a site id never seen together with a bombsite place name stays unknown
    builder.add_round("T", plant=(50.0, "b3", 777), positions={"b3": ("Middle", 0.0, 0.0, 0.0)})
    builder.add_rounds("C" * 10)
    parsed = parse(builder.build())

    assert parsed.rounds["bomb_planted"].tolist()[:5] == [True, True, False, True, True]
    sites = parsed.rounds["bomb_site"].tolist()[:5]
    assert sites[:2] == ["A", "B"]
    assert pd.isna(sites[2])
    assert sites[3] == "B"
    assert pd.isna(sites[4])


def test_survivors_are_counted_at_the_round_end_tick() -> None:
    builder = DemoBuilder()
    builder.add_round(
        "CT",
        duration=60.0,
        kills=[
            K(20, "a1", "b1"),
            K(25, "b2", "a1"),
            K(30, "a2", "b2"),
            K(63, "a2", "b3"),  # after the round end (saving player hunted down): still alive at the end
        ],
    )
    builder.add_rounds("C" * 12)
    parsed = parse(builder.build())

    row = round_row(parsed, 1)
    assert (row["ct_survivors"], row["t_survivors"]) == (4, 3)
    b3 = parsed.player_rounds[
        (parsed.player_rounds["round_number"] == 1) & (parsed.player_rounds["name"] == "b3")
    ].iloc[0]
    assert b3["deaths"] == 1 and not b3["survived"]  # the scoreboard counts the post-round death
    assert len(parsed.kills[parsed.kills["round_number"] == 1]) == 4


# -------------------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("weapon", "expected"),
    [
        ("knife", True),
        ("knife_t", True),
        ("knife_karambit", True),
        ("bayonet", True),
        ("knife_m9_bayonet", True),
        ("ak47", False),
        ("taser", False),
        (None, False),
        ("", False),
    ],
)
def test_is_knife_weapon(weapon: str | None, expected: bool) -> None:
    assert is_knife_weapon(weapon) is expected


def test_min_match_rounds_option() -> None:
    """With a lower threshold a short first segment before a restart would win - the default prevents it."""
    builder = DemoBuilder()
    builder.add_rounds("TTTT", hurts=[H(5, "b1", "a1", 20, 80)])
    builder.restart()
    builder.add_rounds("C" * 13)
    builder.restart()
    builder.add_rounds("TT")
    parsed = parse(builder.build(), options=ParseOptions(min_match_rounds=2))
    assert parsed.n_rounds == 2  # explicit option: the last segment with >= 2 rounds
    assert parse(builder.build()).n_rounds == 13
