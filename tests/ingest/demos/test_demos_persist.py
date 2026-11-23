"""``persist_parsed_demo`` (orientation, idempotency, unknown players) and ``resolve_steamids``."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import DemoSettings
from cs2bot.db.models import (
    Kill,
    MapPlayed,
    MatchLineup,
    Player,
    PlayerMapStats,
    PlayerPositionSample,
    PlayerRoundStats,
    Round,
)
from cs2bot.domain.enums import BuyType, DataSource, RoundEndReason, Side
from cs2bot.ingest.demos.errors import DemoPersistError
from cs2bot.ingest.demos.parse import ParsedDemo, parse_demo
from cs2bot.ingest.demos.persist import (
    determine_team1_roster,
    known_steamids,
    name_similarity,
    persist_parsed_demo,
    remember_steamid,
    resolve_steamids,
)
from tests.ingest.demos.demos_builders import (
    SITE_A_ID,
    STEAMIDS,
    TEAM_A,
    TEAM_B,
    DemoBuilder,
    H,
    K,
    N,
    factory_for,
    sid,
    standard_match,
)
from tests.ingest.demos.demos_db import seed_match


def parsed_match(builder: DemoBuilder | None = None, **options: Any) -> ParsedDemo:
    builder = builder or rich_match()
    return parse_demo("demo.dem", DemoSettings(), parser_factory=factory_for(builder.build(**options)))


def rich_match() -> DemoBuilder:
    """13-5 for team A (CT first) with an eventful first round."""
    builder = DemoBuilder()
    builder.add_round(
        "T",
        "bomb_exploded",
        duration=75.0,
        kills=[
            K(20, "b1", "a1", weapon="glock", headshot=True),
            K(23, "a2", "b1", weapon="usp_silencer", assister="a3"),
            K(40, "b2", "a2"),
            K(41, "b2", "a3"),
            K(42, "b2", "a4"),
        ],
        hurts=[
            H(19, "b1", "a1", 100, 0),
            H(22, "a2", "b1", 100, 0),
            H(30, "a5", "b3", 35, 65, weapon="hegrenade"),
        ],
        nades=[N(30, "he", "a5"), N(12, "smoke", "b4")],
        plant=(50.0, "b3", SITE_A_ID),
        positions={"b3": ("BombsiteA", 5.0, 6.0, 7.0), "a1": ("Jungle", 1.0, 2.0, 3.0)},
        equip=800,
        money=800,
    )
    builder.add_rounds("CCCCCCCTTTT")
    builder.swap_sides()
    builder.add_rounds("TTTTTT")
    return builder


def count(session: Session, model: Any, map_played_id: int) -> int:
    return (
        session.scalar(select(func.count()).select_from(model).where(model.map_played_id == map_played_id))
        or 0
    )


# ---------------------------------------------------------------------------------- orientation


def test_persist_with_roster_a_as_team1(session: Session) -> None:
    seeded = seed_match(session, alpha_is_team1=True)
    parsed = parsed_match()
    stored = persist_parsed_demo(session, seeded.map_ids[1], parsed, seeded.steamid_map)

    assert stored == 18
    rounds = session.scalars(
        select(Round).where(Round.map_played_id == seeded.map_ids[1]).order_by(Round.round_number)
    ).all()
    assert [r.round_number for r in rounds] == list(range(1, 19))
    assert all(r.source is DataSource.DEMO for r in rounds)
    # team1 = Alpha: CT in the first half, T afterwards
    assert [r.team1_side for r in rounds] == [Side.CT] * 12 + [Side.T] * 6
    first = rounds[0]
    assert first.winner_side is Side.T
    assert first.winner_team_id == seeded.team_bravo_id
    assert first.end_reason is RoundEndReason.BOMB_EXPLODED
    assert (first.ct_buy_type, first.t_buy_type) == (BuyType.PISTOL, BuyType.PISTOL)
    assert (first.ct_equipment_value, first.t_equipment_value) == (4000, 4000)
    assert (first.ct_money_start, first.t_money_start) == (4000, 4000)
    assert first.bomb_planted is True and first.bomb_site == "A"
    assert (first.ct_survivors, first.t_survivors) == (1, 4)
    assert first.duration_s == pytest.approx(75.0)
    assert sum(1 for r in rounds if r.winner_team_id == seeded.team_alpha_id) == 13
    assert sum(1 for r in rounds if r.winner_team_id == seeded.team_bravo_id) == 5
    assert rounds[12].winner_side is Side.T and rounds[12].winner_team_id == seeded.team_alpha_id


def test_persist_with_roster_a_as_team2(session: Session) -> None:
    seeded = seed_match(session, alpha_is_team1=False)
    parsed = parsed_match()
    persist_parsed_demo(session, seeded.map_ids[1], parsed, seeded.steamid_map)

    rounds = session.scalars(
        select(Round).where(Round.map_played_id == seeded.map_ids[1]).order_by(Round.round_number)
    ).all()
    # team1 = Bravo: T in the first half, CT afterwards
    assert [r.team1_side for r in rounds] == [Side.T] * 12 + [Side.CT] * 6
    assert rounds[0].winner_team_id == seeded.team_bravo_id
    assert sum(1 for r in rounds if r.winner_team_id == seeded.team_alpha_id) == 13
    stats = session.scalars(
        select(PlayerRoundStats).where(
            PlayerRoundStats.map_played_id == seeded.map_ids[1], PlayerRoundStats.round_number == 1
        )
    ).all()
    team_of = {s.player_id: s.team_id for s in stats}
    assert all(team_of[seeded.player_ids[name]] == seeded.team_alpha_id for name in TEAM_A)
    assert all(team_of[seeded.player_ids[name]] == seeded.team_bravo_id for name in TEAM_B)
    summary = session.get(MapPlayed, seeded.map_ids[1]).extra["demo"]
    assert summary["team1_roster"] == "b" and summary["orientation"] == "lineup"
    assert summary["score"] == [5, 13]


def test_kills_player_rounds_and_positions_are_stored(session: Session) -> None:
    seeded = seed_match(session)
    parsed = parsed_match()
    map_id = seeded.map_ids[1]
    persist_parsed_demo(session, map_id, parsed, seeded.steamid_map)
    pid = seeded.player_ids

    kills = session.scalars(select(Kill).where(Kill.map_played_id == map_id).order_by(Kill.tick)).all()
    assert len(kills) == 5
    opening, trade = kills[0], kills[1]
    assert (opening.attacker_player_id, opening.victim_player_id) == (pid["b1"], pid["a1"])
    assert (opening.attacker_side, opening.victim_side) == (Side.T, Side.CT)
    assert opening.weapon == "glock" and opening.headshot is True
    assert opening.is_opening and opening.was_traded and not opening.is_trade
    assert opening.round_number == 1 and opening.seconds_into_round == pytest.approx(20.0)
    assert opening.victim_place == "Jungle"
    assert (opening.victim_x, opening.victim_y, opening.victim_z) == (1.0, 2.0, 3.0)
    assert opening.distance is not None and opening.distance > 0
    assert opening.penetrated is False and opening.through_smoke is False
    assert trade.is_trade and trade.assister_player_id == pid["a3"]
    assert trade.tick == opening.tick + 3 * 64

    stats = {
        s.player_id: s
        for s in session.scalars(
            select(PlayerRoundStats).where(
                PlayerRoundStats.map_played_id == map_id, PlayerRoundStats.round_number == 1
            )
        )
    }
    assert len(stats) == 10
    b2 = stats[pid["b2"]]
    assert (b2.kills, b2.deaths, b2.survived, b2.kast, b2.side) == (3, 0, True, True, Side.T)
    assert (b2.equipment_value, b2.money_start, b2.team_id) == (800, 800, seeded.team_bravo_id)
    a1 = stats[pid["a1"]]
    assert (a1.deaths, a1.opening_death, a1.kast, a1.survived) == (1, True, True, False)  # traded
    a5 = stats[pid["a5"]]
    assert (a5.damage, a5.utility_damage, a5.he_thrown) == (35, 35, 1)
    assert (a5.clutch_situation, a5.clutch_won) == ("1v4", False)
    assert stats[pid["a3"]].assists == 1
    assert stats[pid["b4"]].smokes_thrown == 1
    assert stats[pid["b1"]].opening_kill is True
    assert stats[pid["a4"]].clutch_situation is None and stats[pid["a4"]].clutch_won is None
    assert count(session, PlayerRoundStats, map_id) == 180

    samples = session.scalars(
        select(PlayerPositionSample).where(
            PlayerPositionSample.map_played_id == map_id,
            PlayerPositionSample.round_number == 1,
            PlayerPositionSample.seconds_into_round == 10,
        )
    ).all()
    assert len(samples) == 10 and all(s.is_alive for s in samples)
    b3 = next(s for s in samples if s.player_id == pid["b3"])
    assert (b3.place, b3.x, b3.y, b3.z, b3.side) == ("BombsiteA", 5.0, 6.0, 7.0, Side.T)


def test_summary_is_written_to_the_map_row(session: Session) -> None:
    seeded = seed_match(session, scores={1: (13, 5)})
    session.get(MapPlayed, seeded.map_ids[1]).extra = {"hltv": {"mapstats_id": 1}}
    session.flush()
    persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), seeded.steamid_map)

    extra = session.get(MapPlayed, seeded.map_ids[1]).extra
    assert extra["hltv"] == {"mapstats_id": 1}  # other sources' data is kept
    summary = extra["demo"]
    assert summary["rounds"] == 18
    assert summary["score"] == [13, 5] and summary["score_matches_result"] is True
    assert summary["unresolved_steamids"] == []
    assert summary["orientation"] == "lineup"
    assert summary["parser_version"]


def test_score_mismatch_is_flagged_not_fatal(session: Session) -> None:
    seeded = seed_match(session, scores={1: (13, 7)})
    stored = persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), seeded.steamid_map)
    assert stored == 18
    summary = session.get(MapPlayed, seeded.map_ids[1]).extra["demo"]
    assert summary["score_matches_result"] is False
    assert any("differs from the recorded result" in w for w in summary["warnings"])


# ---------------------------------------------------------------------------------- idempotency


def test_persisting_twice_gives_the_same_rows(session: Session) -> None:
    seeded = seed_match(session)
    parsed = parsed_match()
    map_id = seeded.map_ids[1]
    persist_parsed_demo(session, map_id, parsed, seeded.steamid_map)
    before = {m: count(session, m, map_id) for m in (Round, Kill, PlayerRoundStats, PlayerPositionSample)}
    assert before[Round] == 18 and before[Kill] == 5 and before[PlayerRoundStats] == 180
    assert before[PlayerPositionSample] > 0

    assert persist_parsed_demo(session, map_id, parsed, seeded.steamid_map) == 18
    session.commit()
    after = {m: count(session, m, map_id) for m in (Round, Kill, PlayerRoundStats, PlayerPositionSample)}
    assert after == before
    numbers = session.scalars(select(Round.round_number).where(Round.map_played_id == map_id)).all()
    assert sorted(numbers) == list(range(1, 19))


def test_persisting_again_in_a_new_session(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        seeded = seed_match(session)
        session.commit()
    parsed = parsed_match()
    for _ in range(2):
        with session_factory() as session:
            persist_parsed_demo(session, seeded.map_ids[1], parsed, seeded.steamid_map)
            session.commit()
    with session_factory() as session:
        assert count(session, Round, seeded.map_ids[1]) == 18
        assert count(session, Kill, seeded.map_ids[1]) == 5


def test_demo_rows_replace_hltv_round_rows(session: Session) -> None:
    seeded = seed_match(session)
    map_id = seeded.map_ids[1]
    for number in range(1, 25):  # HLTV round history, including rounds the demo does not have
        session.add(
            Round(
                map_played_id=map_id,
                round_number=number,
                team1_side=Side.T,
                winner_side=Side.T,
                winner_team_id=seeded.team_alpha_id,
                source=DataSource.HLTV,
            )
        )
    session.flush()
    persist_parsed_demo(session, map_id, parsed_match(), seeded.steamid_map)
    rounds = session.scalars(select(Round).where(Round.map_played_id == map_id)).all()
    assert len(rounds) == 18
    assert {r.source for r in rounds} == {DataSource.DEMO}


def test_other_maps_are_untouched(session: Session) -> None:
    seeded = seed_match(session, maps=("mirage", "inferno"))
    persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), seeded.steamid_map)
    persist_parsed_demo(
        session, seeded.map_ids[2], parsed_match(standard_match("de_inferno")), seeded.steamid_map
    )
    persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), seeded.steamid_map)
    assert count(session, Round, seeded.map_ids[1]) == 18
    assert count(session, Round, seeded.map_ids[2]) == 18
    assert count(session, Kill, seeded.map_ids[1]) == 5
    assert count(session, Kill, seeded.map_ids[2]) == 0


# ------------------------------------------------------------------------------ unknown players


def test_unknown_steamids_become_null_player_ids(session: Session) -> None:
    seeded = seed_match(session)
    mapping = {s: p for s, p in seeded.steamid_map.items() if s not in (sid("b1"), sid("a3"))}
    map_id = seeded.map_ids[1]
    stored = persist_parsed_demo(session, map_id, parsed_match(), mapping)

    assert stored == 18
    kills = session.scalars(select(Kill).where(Kill.map_played_id == map_id).order_by(Kill.tick)).all()
    assert len(kills) == 5  # every kill is kept
    assert kills[0].attacker_player_id is None and kills[0].victim_player_id == seeded.player_ids["a1"]
    assert kills[1].victim_player_id is None and kills[1].assister_player_id is None
    assert kills[0].attacker_side is Side.T  # sides are known even when the player is not
    # per-player rows need a player: the two unknown players have none
    assert count(session, PlayerRoundStats, map_id) == 18 * 8
    players = set(
        session.scalars(select(PlayerRoundStats.player_id).where(PlayerRoundStats.map_played_id == map_id))
    )
    assert seeded.player_ids["b1"] not in players and len(players) == 8
    summary = session.get(MapPlayed, map_id).extra["demo"]
    assert sorted(summary["unresolved_steamids"]) == sorted([str(sid("b1")), str(sid("a3"))])


def test_malformed_rows_are_skipped(session: Session) -> None:
    seeded = seed_match(session)
    parsed = parsed_match()
    parsed.rounds.loc[3, "winner_side"] = "spectator"  # round 4 unusable
    parsed.kills.loc[0, "round_number"] = 99  # kill of a round that does not exist
    parsed.player_rounds = pd.concat(
        [parsed.player_rounds, parsed.player_rounds.iloc[[0]]], ignore_index=True
    )  # duplicated row
    parsed.positions.loc[0, "side"] = None
    map_id = seeded.map_ids[1]
    stored = persist_parsed_demo(session, map_id, parsed, seeded.steamid_map)

    assert stored == 17
    assert 4 not in set(session.scalars(select(Round.round_number).where(Round.map_played_id == map_id)))
    assert count(session, Kill, map_id) == 4
    assert count(session, PlayerRoundStats, map_id) == 170
    summary = session.get(MapPlayed, map_id).extra["demo"]
    assert any("round row skipped" in w for w in summary["warnings"])


def test_two_demo_accounts_of_one_player_do_not_break_the_unique_constraint(session: Session) -> None:
    seeded = seed_match(session)
    mapping = dict(seeded.steamid_map)
    mapping[sid("a2")] = seeded.player_ids["a1"]  # wrong link: two steam ids -> one player
    stored = persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), mapping)
    session.commit()
    assert stored == 18
    assert count(session, PlayerRoundStats, seeded.map_ids[1]) == 18 * 9


def test_demo_of_another_map_is_refused(session: Session) -> None:
    seeded = seed_match(session, maps=("inferno",))
    with pytest.raises(DemoPersistError, match="'inferno' but the demo was played on 'mirage'"):
        persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), seeded.steamid_map)
    assert count(session, Round, seeded.map_ids[1]) == 0
    # an unknown demo map (no header) is not a reason to refuse
    parsed = parsed_match()
    parsed.map_name = ""
    assert persist_parsed_demo(session, seeded.map_ids[1], parsed, seeded.steamid_map) == 18


def test_missing_map_row_and_empty_demo_are_errors(session: Session) -> None:
    seeded = seed_match(session)
    parsed = parsed_match()
    with pytest.raises(DemoPersistError, match="does not exist"):
        persist_parsed_demo(session, 9999, parsed, seeded.steamid_map)
    parsed.rounds = parsed.rounds.iloc[0:0]
    with pytest.raises(DemoPersistError, match="no rounds"):
        persist_parsed_demo(session, seeded.map_ids[1], parsed, seeded.steamid_map)


# ----------------------------------------------------------------------- orientation fall-backs


def test_orientation_from_clan_names_when_no_player_is_resolved(session: Session) -> None:
    seeded = seed_match(session, alpha_is_team1=False, with_lineups=False)
    stored = persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), {})
    assert stored == 18
    summary = session.get(MapPlayed, seeded.map_ids[1]).extra["demo"]
    assert (summary["orientation"], summary["team1_roster"]) == ("clan_name", "b")
    first = session.scalar(
        select(Round).where(Round.map_played_id == seeded.map_ids[1], Round.round_number == 1)
    )
    assert first.team1_side is Side.T
    assert count(session, PlayerRoundStats, seeded.map_ids[1]) == 0
    assert count(session, Kill, seeded.map_ids[1]) == 5


def test_orientation_from_the_known_start_side(session: Session) -> None:
    # clan names in the demo say nothing ("Alpha"/"Bravo" vs the stored team names)
    seeded = seed_match(
        session,
        with_lineups=False,
        alpha_name="Natus Vincere",
        bravo_name="FaZe Clan",
        team1_start_side=Side.T,
    )
    match, map_played = seeded.match_id, session.get(MapPlayed, seeded.map_ids[1])
    from cs2bot.db.models import Match

    roster, method = determine_team1_roster(
        session, session.get(Match, match), map_played, parsed_match(), {}
    )
    # roster "a" starts as CT; team1 started as T -> team1 is roster "b"
    assert (roster, method) == ("b", "start_side")


def test_orientation_from_the_final_score(session: Session) -> None:
    seeded = seed_match(
        session, with_lineups=False, alpha_name="Natus Vincere", bravo_name="FaZe Clan", scores={1: (5, 13)}
    )
    persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), {})
    summary = session.get(MapPlayed, seeded.map_ids[1]).extra["demo"]
    assert (summary["orientation"], summary["team1_roster"]) == ("score", "b")


def test_undeterminable_orientation_is_an_error_and_keeps_existing_rows(session: Session) -> None:
    seeded = seed_match(session, with_lineups=False, alpha_name="Natus Vincere", bravo_name="FaZe Clan")
    map_id = seeded.map_ids[1]
    session.add(
        Round(
            map_played_id=map_id,
            round_number=1,
            team1_side=Side.CT,
            winner_side=Side.CT,
            source=DataSource.HLTV,
        )
    )
    session.flush()
    with pytest.raises(DemoPersistError, match="cannot tell which demo roster is team1"):
        persist_parsed_demo(session, map_id, parsed_match(), {})
    assert count(session, Round, map_id) == 1  # nothing was deleted


def test_orientation_uses_map_stats_when_there_is_no_lineup(session: Session) -> None:
    seeded = seed_match(session, alpha_is_team1=False, with_lineups=False, alpha_name="X", bravo_name="Y")
    for name in ("a1", "b1"):
        team_id = seeded.team_alpha_id if name == "a1" else seeded.team_bravo_id
        session.add(
            PlayerMapStats(
                map_played_id=seeded.map_ids[1],
                player_id=seeded.player_ids[name],
                team_id=team_id,
                source=DataSource.HLTV,
                kills=10,
                deaths=10,
            )
        )
    session.flush()
    persist_parsed_demo(session, seeded.map_ids[1], parsed_match(), seeded.steamid_map)
    summary = session.get(MapPlayed, seeded.map_ids[1]).extra["demo"]
    assert (summary["orientation"], summary["team1_roster"]) == ("lineup", "b")


# ------------------------------------------------------------------------------ steam id linking


def test_resolve_by_exact_nickname_and_store_the_steamid(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        seeded = seed_match(session)
        session.commit()
    parsed = parsed_match()
    with session_factory() as session:
        mapping = resolve_steamids(session, seeded.match_id, parsed)
        assert mapping == seeded.steamid_map
        session.commit()
    with session_factory() as session:  # the JSON change really reached the database
        player = session.get(Player, seeded.player_ids["a1"])
        assert player.extra["steamid"] == str(sid("a1"))
        assert player.extra["steamids"] == [str(sid("a1"))]
        assert known_steamids(player) == {sid("a1")}


def test_resolve_prefers_a_stored_steamid_over_the_name(session: Session) -> None:
    nicknames = {name: f"pro{name}" for name in TEAM_A + TEAM_B}  # demo names do not match at all
    seeded = seed_match(session, nicknames=nicknames)
    for name, player_id in seeded.player_ids.items():
        session.get(Player, player_id).extra = {"steamid": str(STEAMIDS[name]), "country": "PL"}
    session.flush()
    builder = rich_match()
    mapping = resolve_steamids(session, seeded.match_id, parsed_match(builder))
    assert mapping == seeded.steamid_map
    assert session.get(Player, seeded.player_ids["a1"]).extra == {"steamid": str(sid("a1")), "country": "PL"}


def test_resolve_accepts_integer_steamids_in_extra(session: Session) -> None:
    seeded = seed_match(session, nicknames={"a1": "somebodyelse"})
    session.get(Player, seeded.player_ids["a1"]).extra = {"steamid": sid("a1")}
    session.flush()
    mapping = resolve_steamids(session, seeded.match_id, parsed_match())
    assert mapping[sid("a1")] == seeded.player_ids["a1"]


def test_resolve_fuzzy_names_with_clan_tags(session: Session) -> None:
    nicknames = {
        "a1": "ZywOo",
        "a2": "apEX",
        "a3": "flameZ",
        "a4": "mezii",
        "a5": "ropz",
        "b1": "donk",
        "b2": "sh1ro",
        "b3": "zont1x",
        "b4": "magixx",
        "b5": "chopper",
    }
    seeded = seed_match(session, nicknames=nicknames)
    parsed = parsed_match()
    in_game = {
        "a1": "Vitality ZywOo",  # sponsor / tag around the nickname
        "a2": "apEX-",
        "a3": "flamez",  # case
        "a4": "mezii",
        "a5": "r0pz",  # leetspeak variant: fuzzy
        "b1": "donk666",
        "b2": "sh1ro",
        "b3": "zontix",
        "b4": "magixx",
        "b5": "chopper",
    }
    parsed.players["name"] = parsed.players["name"].map(in_game)
    mapping = resolve_steamids(session, seeded.match_id, parsed)
    assert mapping == seeded.steamid_map
    assert session.get(Player, seeded.player_ids["a5"]).extra["steamid"] == str(sid("a5"))


def test_resolve_by_elimination_within_a_roster(session: Session) -> None:
    seeded = seed_match(session, nicknames={"a5": "TotallyDifferent"})
    mapping = resolve_steamids(session, seeded.match_id, parsed_match())
    # four of roster "a" are matched by name to team Alpha, whose only free lineup player is left
    assert mapping[sid("a5")] == seeded.player_ids["a5"]
    assert len(mapping) == 10


def test_resolve_leaves_unknown_and_ambiguous_players_out(session: Session) -> None:
    seeded = seed_match(session, nicknames={"a4": "Alpha One", "a5": "Alpha Two", "b5": "Zed"})
    parsed = parsed_match()
    names = dict(zip(parsed.players["steamid"], parsed.players["name"], strict=True))
    names[sid("a4")] = "alpha"  # fits "Alpha One" and "Alpha Two" equally well
    names[sid("a5")] = "???"
    names[sid("b5")] = ""
    parsed.players["name"] = [names[s] for s in parsed.players["steamid"]]
    mapping = resolve_steamids(session, seeded.match_id, parsed)
    assert sid("a4") not in mapping and sid("a5") not in mapping
    # b5 is the only unmatched player of his roster and of team Bravo's lineup: elimination applies
    assert mapping[sid("b5")] == seeded.player_ids["b5"]
    assert len(mapping) == 8
    # an ambiguous player's steam id must not be stored on anybody
    assert "steamid" not in (session.get(Player, seeded.player_ids["a4"]).extra or {})


def test_resolve_never_raises_without_lineups(session: Session) -> None:
    seeded = seed_match(session, with_lineups=False)
    assert resolve_steamids(session, seeded.match_id, parsed_match()) == {}
    assert resolve_steamids(session, 424242, parsed_match()) == {}


def test_resolve_does_not_trust_a_steamid_reused_by_another_lineup_player(session: Session) -> None:
    """Tournament accounts get reused: a1's stored id now shows up under b1's exact nickname."""
    seeded = seed_match(session)
    session.get(Player, seeded.player_ids["a1"]).extra = {"steamid": str(sid("b1"))}
    session.flush()
    mapping = resolve_steamids(session, seeded.match_id, parsed_match())
    assert mapping[sid("b1")] == seeded.player_ids["b1"]
    assert mapping[sid("a1")] == seeded.player_ids["a1"]
    extra = session.get(Player, seeded.player_ids["a1"]).extra
    assert extra["steamid"] == str(sid("b1"))  # the first id is kept ...
    assert set(extra["steamids"]) == {str(sid("a1")), str(sid("b1"))}  # ... and the new one is added


def test_resolve_player_outside_the_lineup_by_steamid_and_name(session: Session) -> None:
    """A stand-in who is not in the stored lineup but whose steam id is known from an earlier demo."""
    seeded = seed_match(session)
    lineup = session.scalar(
        select(MatchLineup).where(
            MatchLineup.match_id == seeded.match_id, MatchLineup.player_id == seeded.player_ids["a5"]
        )
    )
    session.delete(lineup)
    session.get(Player, seeded.player_ids["a5"]).extra = {"steamid": str(sid("a5"))}
    session.flush()
    mapping = resolve_steamids(session, seeded.match_id, parsed_match())
    assert mapping[sid("a5")] == seeded.player_ids["a5"]


def test_remember_steamid_assigns_a_new_dict() -> None:
    player = Player(nickname="x", extra={"country": "DK"})
    original = player.extra
    assert remember_steamid(player, 76561198000000001) is True
    assert player.extra is not original and original == {"country": "DK"}
    assert player.extra == {
        "country": "DK",
        "steamid": "76561198000000001",
        "steamids": ["76561198000000001"],
    }
    assert remember_steamid(player, 76561198000000001) is False
    assert remember_steamid(player, 76561198000000003) is True
    assert player.extra["steamid"] == "76561198000000001"
    assert player.extra["steamids"] == ["76561198000000001", "76561198000000003"]
    assert known_steamids(
        Player(nickname="y", extra={"steamid": "garbage", "steamids": [None, 0, "12"]})
    ) == {12}
    assert known_steamids(Player(nickname="z")) == set()


@pytest.mark.parametrize(
    ("demo_name", "nickname", "low", "high"),
    [
        ("s1mple", "s1mple", 100, 100),
        ("S1MPLE", "s1mple", 100, 100),
        ("NAVI s1mple", "s1mple", 95, 95),
        ("ZywOo-Vitality", "ZywOo", 95, 95),
        ("r0pz", "ropz", 70, 94),
        ("xyz", "device", 0, 40),
        ("", "device", 0, 0),
        ("ax1Le", "x", 0, 60),  # a one-letter nickname inside a name proves nothing
    ],
)
def test_name_similarity(demo_name: str, nickname: str, low: float, high: float) -> None:
    assert low <= name_similarity(demo_name, nickname) <= high
