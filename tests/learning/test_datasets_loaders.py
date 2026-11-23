"""cs2bot.datasets loaders: DB rows -> records (counts, orientation, filters, source preference)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from cs2bot import datasets
from cs2bot.db.models import (
    Kill,
    MapPlayed,
    Match,
    MatchLineup,
    Player,
    PlayerMapStats,
    PlayerRoundStats,
    Round,
    Team,
    Tournament,
    Veto,
)
from cs2bot.domain.enums import (
    BuyType,
    DataSource,
    MatchFormat,
    MatchStatus,
    PlayerRole,
    RoundEndReason,
    Side,
    TeamSlot,
    Tier,
    VetoAction,
)
from cs2bot.domain.records import MAP_RESULT_COLUMNS, PLAYER_MAP_COLUMNS
from cs2bot.testing.synthetic import SynthUniverse
from tests.learning.learning_helpers import populate_small

T0 = datetime(2025, 3, 1, 18, 0, tzinfo=UTC)


def _map_key(r) -> tuple[int, int]:
    return (r.match_id, r.map_number)


# ----------------------------------------------------------------------------------- map results


def test_map_results_match_the_universe_exactly(learning_db: Session, learning_universe: SynthUniverse):
    loaded = datasets.load_map_results(learning_db)
    truth = {_map_key(r): r for r in learning_universe.map_results()}
    assert len(loaded) == len(truth) > 250
    # every field (scores, side splits, overtime, pick, LAN, tier, format, sorted lineups) round-trips
    for rec in loaded:
        assert rec == truth[_map_key(rec)]
    # chronological and stable
    keys = [(r.played_at, r.match_id, r.map_number) for r in loaded]
    assert keys == sorted(keys)
    # both pick orientations and deciders occur, and the winner follows the team1/team2 orientation
    assert {r.picked_by for r in loaded} == {TeamSlot.TEAM1, TeamSlot.TEAM2, None}
    assert {r.team1_start_side for r in loaded} == {Side.CT, Side.T}
    assert any(r.overtime_rounds > 0 for r in loaded)
    for rec in loaded[:50]:
        row = learning_db.execute(
            select(MapPlayed.winner_team_id).where(
                MapPlayed.match_id == rec.match_id, MapPlayed.map_number == rec.map_number
            )
        ).scalar_one()
        assert rec.winner_team_id == row
        assert len(rec.team1_lineup) == len(rec.team2_lineup) == 5


def test_time_window_is_since_inclusive_until_exclusive(learning_db: Session):
    everything = datasets.load_map_results(learning_db)
    pivot = everything[len(everything) // 2]
    before = datasets.load_map_results(learning_db, until=pivot.played_at)
    after = datasets.load_map_results(learning_db, since=pivot.played_at)
    assert all(r.played_at < pivot.played_at for r in before)
    assert after[0].played_at == pivot.played_at
    assert len(before) + len(after) == len(everything)
    # a naive datetime is read as UTC
    naive = pivot.played_at.replace(tzinfo=None)
    assert len(datasets.load_map_results(learning_db, until=naive)) == len(before)
    # the same window applies to the other loaders
    rounds = datasets.load_round_records(learning_db, until=pivot.played_at)
    players = datasets.load_player_map_records(learning_db, until=pivot.played_at)
    assert {(r.match_id, r.map_number) for r in rounds} == {_map_key(r) for r in before}
    assert {(r.match_id, r.map_number) for r in players} == {_map_key(r) for r in before}


def test_scope_filter_and_malformed_rows_are_skipped(session: Session):
    uni = populate_small(session)
    total = len(uni.map_results())
    assert len(datasets.load_map_results(session)) == total

    first, second, third, fourth = (m.id for m in uni.matches[:4])
    n = {
        mid: sum(1 for r in uni.map_results() if r.match_id == mid) for mid in (first, second, third, fourth)
    }
    session.execute(update(Match).where(Match.id == first).values(in_scope=False))
    assert len(datasets.load_map_results(session)) == total - n[first]
    assert len(datasets.load_map_results(session, in_scope_only=False)) == total

    # cancelled match, unknown team, undecided / impossible scores, placeholder map names
    session.execute(update(Match).where(Match.id == second).values(status=MatchStatus.CANCELED))
    session.execute(update(Match).where(Match.id == third).values(team2_id=None))
    maps4 = list(session.scalars(select(MapPlayed).where(MapPlayed.match_id == fourth)))
    maps4[0].team1_rounds, maps4[0].team2_rounds = 9, 9
    if len(maps4) > 1:
        maps4[1].map_name = "TBA"
    session.flush()
    loaded = datasets.load_map_results(session, in_scope_only=False)
    bad = {second, third}
    assert not any(r.match_id in bad for r in loaded)
    assert sum(1 for r in loaded if r.match_id == fourth) == max(0, n[fourth] - 2)
    # the other loaders survive the same damage
    assert not any(r.match_id in bad for r in datasets.load_round_records(session, in_scope_only=False))
    assert not any(r.match_id in bad for r in datasets.load_player_map_records(session, in_scope_only=False))
    assert not any(v.match_id in bad for v in datasets.load_veto_records(session, in_scope_only=False))


def _add_match(
    session: Session,
    *,
    match_id: int,
    t1: int,
    t2: int,
    at: datetime,
    status: MatchStatus = MatchStatus.FINISHED,
    fmt: MatchFormat | None = MatchFormat.BO3,
    tournament_id: int | None = None,
    **extra,
) -> Match:
    match = Match(
        id=match_id,
        team1_id=t1,
        team2_id=t2,
        scheduled_at=at,
        status=status,
        format=fmt,
        in_scope=True,
        tournament_id=tournament_id,
        **extra,
    )
    session.add(match)
    session.flush()
    return match


def _teams(session: Session, n: int = 2, players_per_team: int = 5) -> None:
    for tid in range(1, n + 1):
        session.add(Team(id=tid, name=f"Team {tid}"))
    session.flush()
    for tid in range(1, n + 1):
        for k in range(players_per_team):
            session.add(Player(id=tid * 10 + k, nickname=f"p{tid}{k}", current_team_id=tid))
    session.flush()


def test_map_result_fallbacks_for_lineup_tier_lan_and_overtime(session: Session):
    _teams(session)
    tour = Tournament(name="LAN Major", tier=Tier.S, is_lan=True)
    session.add(tour)
    session.flush()
    # match-level tier unranked and LAN unknown -> tournament values; played_at missing -> scheduled_at
    _add_match(session, match_id=1, t1=1, t2=2, at=T0, tournament_id=tour.id, status=MatchStatus.LIVE)
    session.add_all(
        [
            # finished map inside a live series counts; overtime derived from the score (16-14 = 6 OT rounds)
            MapPlayed(
                id=1,
                match_id=1,
                map_number=1,
                map_name="de_dust2",
                status=MatchStatus.FINISHED,
                team1_rounds=14,
                team2_rounds=16,
                picked_by_team_id=2,
            ),
            # still being played: not a result
            MapPlayed(
                id=2,
                match_id=1,
                map_number=2,
                map_name="Mirage",
                status=MatchStatus.LIVE,
                team1_rounds=5,
                team2_rounds=3,
            ),
        ]
    )
    for pid in (10, 11, 12, 13, 14):
        session.add(
            PlayerMapStats(
                map_played_id=1, player_id=pid, team_id=1, source=DataSource.HLTV, kills=20, deaths=18
            )
        )
    session.flush()
    (rec,) = datasets.load_map_results(session)
    assert rec.map_name == "dust2"
    assert rec.played_at == T0
    assert (rec.tier, rec.is_lan) == (Tier.S, True)
    assert rec.overtime_rounds == 6
    assert rec.picked_by is TeamSlot.TEAM2 and rec.winner is TeamSlot.TEAM2
    # no match_lineups rows: players with a stat line on the map stand in for the lineup
    assert rec.team1_lineup == (10, 11, 12, 13, 14)
    assert rec.team2_lineup == ()


# ------------------------------------------------------------------------------------------ vetoes


def test_veto_records_match_the_universe(learning_db: Session, learning_universe: SynthUniverse):
    loaded = datasets.load_veto_records(learning_db)
    truth = {v.match_id: v for v in learning_universe.veto_records()}
    assert len(loaded) == len(truth) == 150
    for rec in loaded:
        assert rec == truth[rec.match_id]
    assert [v.played_at for v in loaded] == sorted(v.played_at for v in loaded)
    sample = loaded[0]
    assert sample.steps[0].team is TeamSlot.TEAM1 and sample.steps[1].team is TeamSlot.TEAM2
    assert sample.steps[-1].action is VetoAction.LEFTOVER and sample.steps[-1].team is None
    assert sample.map_pool is not None and len(sample.map_pool) == 7


def test_veto_orientation_follows_our_match_record_and_corrupt_vetoes_are_dropped(session: Session):
    _teams(session, n=3)
    _add_match(session, match_id=1, t1=2, t2=1, at=T0, fmt=None)  # our team1 is team id 2
    _add_match(session, match_id=2, t1=1, t2=2, at=T0 + timedelta(days=1))
    _add_match(session, match_id=3, t1=1, t2=2, at=T0 + timedelta(days=2), status=MatchStatus.SCHEDULED)
    pool = ["ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "train"]
    actions = [
        VetoAction.BAN,
        VetoAction.BAN,
        VetoAction.PICK,
        VetoAction.PICK,
        VetoAction.BAN,
        VetoAction.BAN,
    ]
    for step, (action, name) in enumerate(zip(actions, pool, strict=False), start=1):
        team = 1 if step % 2 else 2  # team id 1 (our team2) starts the veto
        session.add(Veto(match_id=1, step=step, team_id=team, action=action, map_name=f"de_{name}"))
        session.add(Veto(match_id=3, step=step, team_id=team, action=action, map_name=name))
    session.add(Veto(match_id=1, step=7, team_id=None, action=VetoAction.LEFTOVER, map_name="Train"))
    # match 2: a step attributed to a team that is not playing -> the whole veto is unusable
    session.add(Veto(match_id=2, step=1, team_id=3, action=VetoAction.BAN, map_name="nuke"))
    session.flush()

    (rec,) = datasets.load_veto_records(session)
    assert rec.match_id == 1
    assert [s.team for s in rec.steps[:4]] == [TeamSlot.TEAM2, TeamSlot.TEAM1, TeamSlot.TEAM2, TeamSlot.TEAM1]
    assert rec.steps[-1].team is None and rec.steps[-1].map_name == "train"
    assert rec.format is MatchFormat.BO3  # inferred from two picks when matches.format is NULL
    assert rec.map_pool == tuple(pool)
    # published vetoes of upcoming matches are available on request (incomplete veto: no pool)
    upcoming = datasets.load_veto_records(session, finished_only=False)
    assert [v.match_id for v in upcoming] == [1, 3]
    assert upcoming[1].map_pool is None


# ------------------------------------------------------------------------------------------ rounds


def test_round_records_are_in_team1_orientation_with_reconstructed_scores(
    learning_db: Session, learning_universe: SynthUniverse
):
    loaded = datasets.load_round_records(learning_db)
    truth = {(r.match_id, r.map_number, r.round_number): r for r in learning_universe.round_records()}
    assert len(loaded) == len(truth) > 5000
    played_at = {_map_key(r): r.played_at for r in learning_universe.map_results()}
    seen_sides = set()
    for rec in loaded:
        expected = truth[(rec.match_id, rec.map_number, rec.round_number)]
        # the synthetic record carries the match time; the loader reports the map's own time
        assert rec.played_at == played_at[(rec.match_id, rec.map_number)]
        assert rec == expected.model_copy(update={"played_at": rec.played_at})
        seen_sides.add((rec.team1_side, rec.winner))
    # the conversion is exercised for both sides and both winners
    assert len(seen_sides) == 4
    # score before each round is the cumulative count of the earlier rounds of that map
    by_map: dict[tuple[int, int], list] = {}
    for rec in loaded:
        by_map.setdefault((rec.match_id, rec.map_number), []).append(rec)
    for rounds in list(by_map.values())[:40]:
        s1 = s2 = 0
        for rec in rounds:
            assert (rec.team1_score_before, rec.team2_score_before) == (s1, s2)
            s1 += rec.winner is TeamSlot.TEAM1
            s2 += rec.winner is TeamSlot.TEAM2
        final = next(m for m in learning_universe.map_results() if _map_key(m) == _map_key(rounds[0]))
        assert (s1, s2) == (final.team1_rounds, final.team2_rounds)


def test_round_score_is_unknown_after_a_gap_and_hltv_rows_have_no_economy(session: Session):
    _teams(session)
    _add_match(session, match_id=1, t1=1, t2=2, at=T0)
    session.add(
        MapPlayed(
            id=1,
            match_id=1,
            map_number=1,
            map_name="nuke",
            status=MatchStatus.FINISHED,
            team1_rounds=13,
            team2_rounds=4,
        )
    )
    session.flush()
    for number in (1, 2, 3, 5, 6):  # round 4 is missing
        ct_wins = number != 2
        session.add(
            Round(
                map_played_id=1,
                round_number=number,
                team1_side=Side.T,
                winner_side=Side.CT if ct_wins else Side.T,
                source=DataSource.DEMO if number <= 3 else DataSource.HLTV,
                end_reason=RoundEndReason.BOMB_DEFUSED,
                ct_equipment_value=21000 if number <= 3 else None,
                t_equipment_value=4000 if number <= 3 else None,
                ct_money_start=9000 if number <= 3 else None,
                t_money_start=1500 if number <= 3 else None,
                ct_buy_type=BuyType.FULL if number <= 3 else None,
                t_buy_type=BuyType.ECO if number <= 3 else None,
                bomb_planted=True if number <= 3 else None,
                ct_survivors=4 if number <= 3 else None,
                t_survivors=0 if number <= 3 else None,
            )
        )
    session.flush()
    rounds = datasets.load_round_records(session)
    assert [r.round_number for r in rounds] == [1, 2, 3, 5, 6]
    first = rounds[0]
    # team1 is T: its values are the T-side columns, team2 gets the CT-side columns
    assert first.team1_side is Side.T and first.winner is TeamSlot.TEAM2 and first.winner_side is Side.CT
    assert (first.team1_equipment_value, first.team2_equipment_value) == (4000, 21000)
    assert (first.team1_money_start, first.team2_money_start) == (1500, 9000)
    assert (first.team1_buy, first.team2_buy) == (BuyType.ECO, BuyType.FULL)
    assert (first.team1_survivors, first.team2_survivors) == (0, 4)
    assert rounds[1].winner is TeamSlot.TEAM1
    assert [(r.team1_score_before, r.team2_score_before) for r in rounds] == [
        (0, 0),
        (0, 1),
        (1, 1),
        (None, None),
        (None, None),
    ]
    hltv = rounds[-1]
    assert hltv.team1_buy is None and hltv.team1_equipment_value is None and hltv.bomb_planted is None
    assert hltv.end_reason is RoundEndReason.BOMB_DEFUSED
    only_demo = datasets.load_round_records(session, sources=[DataSource.DEMO])
    assert [r.round_number for r in only_demo] == [1, 2, 3]


# ---------------------------------------------------------------------------------- player records


def test_player_records_match_the_universe(learning_db: Session, learning_universe: SynthUniverse):
    loaded = datasets.load_player_map_records(learning_db)
    truth = {(r.match_id, r.map_number, r.player_id): r for r in learning_universe.player_map_records()}
    played_at = {_map_key(r): r.played_at for r in learning_universe.map_results()}
    assert len(loaded) == len(truth)
    for rec in loaded[:1500]:
        expected = truth[(rec.match_id, rec.map_number, rec.player_id)]
        assert rec.played_at == played_at[(rec.match_id, rec.map_number)]
        # synthetic kills carry no weapons: awp_kills stays unknown, everything else is equal
        assert rec.awp_kills is None
        assert rec == expected.model_copy(update={"played_at": rec.played_at})
        assert rec.role is learning_universe.players[rec.player_id].role
        assert rec.team_rounds_won is not None and rec.opponent_rounds_won is not None
        assert max(rec.team_rounds_won, rec.opponent_rounds_won) >= 13
    by_map: dict[tuple[int, int], set[int]] = {}
    for rec in loaded:
        by_map.setdefault((rec.match_id, rec.map_number), set()).add(rec.opponent_team_id)
        assert rec.team_id != rec.opponent_team_id
    assert all(len(v) == 2 for v in by_map.values())


def test_player_source_preference_fill_in_standins_and_awp_kills(session: Session):
    _teams(session)
    session.get(Player, 10).primary_role = PlayerRole.AWPER
    _add_match(session, match_id=1, t1=1, t2=2, at=T0)
    session.add(
        MapPlayed(
            id=1,
            match_id=1,
            map_number=1,
            map_name="inferno",
            status=MatchStatus.FINISHED,
            team1_rounds=13,
            team2_rounds=9,
        )
    )
    session.add(MatchLineup(match_id=1, team_id=1, player_id=10, is_standin=True, source=DataSource.HLTV))
    session.add(MatchLineup(match_id=1, team_id=2, player_id=20, is_standin=False, source=DataSource.HLTV))
    session.flush()

    def stat(player_id: int, team_id: int, source: DataSource, kills: int, **kw) -> None:
        session.add(
            PlayerMapStats(
                map_played_id=1,
                player_id=player_id,
                team_id=team_id,
                source=source,
                kills=kills,
                deaths=15,
                **kw,
            )
        )

    # player 10: all four sources disagree on kills -> DEMO wins; the HLTV rating fills the gap
    stat(10, 1, DataSource.SYNTHETIC, 1, rounds_played=22)
    stat(10, 1, DataSource.PANDASCORE, 2, adr=70.0)
    stat(10, 1, DataSource.HLTV, 3, rating=1.31, kast=74.0, adr=88.8)
    stat(10, 1, DataSource.DEMO, 24, first_kills=5, first_deaths=2)
    # player 20: HLTV beats PandaScore; no DEMO row
    stat(20, 2, DataSource.PANDASCORE, 11, awp_kills=7)
    stat(20, 2, DataSource.HLTV, 17)
    # player 21: synthetic only; player 11: a row for a team that is not in the match -> skipped
    stat(21, 2, DataSource.SYNTHETIC, 9)
    session.add(Team(id=3, name="Other"))
    session.flush()
    stat(11, 3, DataSource.HLTV, 30)
    # kills with weapons (demo data): player 10 has two AWP kills, one rifle kill and one team kill
    for weapon, a_side, v_side in (
        ("awp", "ct", "t"),
        ("AWP", "ct", "t"),
        ("ak47", "ct", "t"),
        ("awp", "ct", "ct"),
    ):
        session.add(
            Kill(
                map_played_id=1,
                round_number=1,
                attacker_player_id=10,
                victim_player_id=20,
                attacker_side=Side(a_side),
                victim_side=Side(v_side),
                weapon=weapon,
            )
        )
    session.flush()

    records = {r.player_id: r for r in datasets.load_player_map_records(session)}
    assert set(records) == {10, 20, 21}
    p10 = records[10]
    assert p10.kills == 24 and (p10.first_kills, p10.first_deaths) == (5, 2)
    assert p10.rating == pytest.approx(1.31) and p10.kast == pytest.approx(74.0)
    assert p10.adr == pytest.approx(88.8)  # HLTV outranks PandaScore for the fill-in too
    assert p10.rounds_played == 22  # only the synthetic row knew it
    assert p10.awp_kills == 2  # counted from the kills table; the team kill is ignored
    assert p10.is_standin and p10.role is PlayerRole.AWPER
    assert (p10.team_id, p10.opponent_team_id) == (1, 2)
    assert (p10.team_rounds_won, p10.opponent_rounds_won) == (13, 9)
    p20 = records[20]
    assert p20.kills == 17 and p20.awp_kills == 7 and not p20.is_standin
    assert p20.rounds_played == 22  # falls back to the map's total rounds
    assert (p20.team_rounds_won, p20.opponent_rounds_won) == (9, 13)
    assert records[21].kills == 9 and records[21].awp_kills == 0  # the map has weapon data: a real zero


# ------------------------------------------------------------------------------------- data frames


def test_to_frame(learning_db: Session):
    maps = datasets.load_map_results(learning_db)
    frame = datasets.to_frame(maps)
    assert list(frame.columns) == list(MAP_RESULT_COLUMNS)
    assert len(frame) == len(maps)
    assert set(frame["team1_start_side"].unique()) == {"ct", "t"}
    assert set(frame["tier"].unique()) <= {"s", "a", "b"}
    assert isinstance(frame["team1_lineup"].iloc[0], tuple)
    assert str(frame["played_at"].dt.tz) == "UTC"
    players = datasets.to_frame(datasets.load_player_map_records(learning_db)[:50])
    assert list(players.columns) == list(PLAYER_MAP_COLUMNS)
    assert players["kills"].sum() > 0
    empty = datasets.to_frame([], columns=MAP_RESULT_COLUMNS)
    assert empty.empty and list(empty.columns) == list(MAP_RESULT_COLUMNS)
    assert datasets.to_frame([]).empty
    subset = datasets.to_frame(maps[:3], columns=["match_id", "map_name"])
    assert list(subset.columns) == ["match_id", "map_name"]
    assert isinstance(frame, pd.DataFrame)


# ---------------------------------------------------------------------------------- demo extras


def test_team_demo_stats_and_role_frames(session: Session):
    _teams(session)
    _add_match(session, match_id=1, t1=1, t2=2, at=T0)
    session.add(
        MapPlayed(
            id=1,
            match_id=1,
            map_number=1,
            map_name="mirage",
            status=MatchStatus.FINISHED,
            team1_rounds=13,
            team2_rounds=7,
        )
    )
    session.flush()
    for number in (1, 2):
        session.add(
            Round(
                map_played_id=1,
                round_number=number,
                team1_side=Side.CT if number == 1 else Side.T,
                winner_side=Side.CT,
                source=DataSource.DEMO,
            )
        )
        for pid, team, side in (
            (10, 1, Side.CT if number == 1 else Side.T),
            (20, 2, Side.T if number == 1 else Side.CT),
        ):
            session.add(
                PlayerRoundStats(
                    map_played_id=1,
                    round_number=number,
                    player_id=pid,
                    team_id=team,
                    side=side,
                    utility_damage=30 if team == 1 else 5,
                    enemies_flashed=2 if team == 1 else 0,
                    flashes_thrown=1,
                )
            )

    def kill(number: int, attacker: int, a_side: Side, *, trade: bool = False, traded: bool = False) -> None:
        session.add(
            Kill(
                map_played_id=1,
                round_number=number,
                attacker_player_id=attacker,
                victim_player_id=20 if attacker == 10 else 10,
                attacker_side=a_side,
                victim_side=a_side.other,
                weapon="ak47",
                is_trade=trade,
                was_traded=traded,
                seconds_into_round=12.5,
            )
        )

    kill(1, 10, Side.CT)  # team1 (CT in round 1)
    kill(1, 20, Side.T, traded=True)  # team2 kills, the team1 death is traded ...
    kill(1, 10, Side.CT, trade=True)  # ... by this team1 kill
    kill(2, 20, Side.CT)  # round 2: team2 is CT
    session.flush()

    stats = {s.team_id: s for s in datasets.load_team_demo_stats(session)}
    assert set(stats) == {1, 2}
    t1, t2 = stats[1], stats[2]
    assert (t1.kills, t1.deaths, t1.trade_kills, t1.traded_deaths) == (2, 2, 1, 1)
    assert (t2.kills, t2.deaths, t2.trade_kills, t2.traded_deaths) == (2, 2, 0, 0)
    assert t1.rounds == 20 and t1.opponent_team_id == 2
    assert (t1.utility_damage, t2.utility_damage) == (60.0, 10.0)
    assert (t1.enemies_flashed, t2.enemies_flashed) == (4, 0)

    frames = datasets.load_role_frames(session, as_of=T0 + timedelta(days=1))
    assert list(frames.kills.columns) == list(datasets.demo.KILL_COLUMNS)
    assert len(frames.kills) == 4 and set(frames.kills["attacker_side"]) == {"ct", "t"}
    assert len(frames.player_rounds) == 4 and frames.positions.empty
    assert frames.player_rounds["enemies_flashed"].sum() == 4
    # outside the window: nothing
    old = datasets.load_role_frames(session, as_of=T0 - timedelta(days=1))
    assert old.kills.empty and old.player_rounds.empty
    assert datasets.available_position_seconds(session) == []


def test_loaders_on_an_empty_database(session: Session):
    assert datasets.load_map_results(session) == []
    assert datasets.load_veto_records(session) == []
    assert datasets.load_round_records(session) == []
    assert datasets.load_player_map_records(session) == []
    assert datasets.load_team_demo_stats(session) == []
    session.execute(delete(Kill))


def test_decider_flag_wins_over_a_contradicting_picker(session: Session):
    _teams(session)
    _add_match(session, match_id=1, t1=1, t2=2, at=T0)
    session.add_all(
        [
            MapPlayed(
                match_id=1,
                map_number=1,
                map_name="nuke",
                status=MatchStatus.FINISHED,
                team1_rounds=13,
                team2_rounds=4,
                picked_by_team_id=1,
            ),
            # a source wrote the decider's "picker" (e.g. the team that chose the side): not a pick
            MapPlayed(
                match_id=1,
                map_number=2,
                map_name="ancient",
                status=MatchStatus.FINISHED,
                team1_rounds=13,
                team2_rounds=9,
                picked_by_team_id=2,
                is_decider=True,
            ),
        ]
    )
    session.flush()
    first, decider = datasets.load_map_results(session)
    assert first.picked_by is TeamSlot.TEAM1
    assert decider.picked_by is None
