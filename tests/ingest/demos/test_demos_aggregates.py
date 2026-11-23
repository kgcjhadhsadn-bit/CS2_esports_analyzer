"""``rebuild_position_aggregates`` / ``rebuild_duel_aggregates``: numbers, windows, idempotency."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cs2bot.config import DemoSettings
from cs2bot.db.models import DuelAggregate, Kill, MapPlayed, PlayerPositionSample, PositionAggregate
from cs2bot.domain.enums import Side
from cs2bot.ingest.demos.aggregates import ALL_MAPS, rebuild_duel_aggregates, rebuild_position_aggregates
from cs2bot.ingest.demos.parse import parse_demo
from cs2bot.ingest.demos.persist import persist_parsed_demo
from tests.ingest.demos.demos_builders import DemoBuilder, K, factory_for
from tests.ingest.demos.demos_db import SeededMatch, seed_match

AS_OF = date(2026, 4, 1)


def at(days_before_as_of: float) -> datetime:
    return datetime(2026, 4, 1, tzinfo=UTC) - timedelta(days=days_before_as_of)


def add_sample(
    session: Session,
    map_id: int,
    player_id: int,
    round_number: int,
    place: str | None,
    *,
    side: Side = Side.CT,
    second: int = 20,
) -> None:
    session.add(
        PlayerPositionSample(
            map_played_id=map_id,
            round_number=round_number,
            player_id=player_id,
            side=side,
            seconds_into_round=second,
            place=place,
            x=0.0,
            y=0.0,
            z=0.0,
            is_alive=True,
        )
    )


def add_kill(
    session: Session,
    map_id: int,
    attacker: int | None,
    victim: int | None,
    *,
    attacker_place: str | None = "Window",
    victim_place: str | None = "Middle",
    attacker_side: Side | None = Side.CT,
    victim_side: Side | None = Side.T,
    opening: bool = False,
    round_number: int = 1,
) -> None:
    session.add(
        Kill(
            map_played_id=map_id,
            round_number=round_number,
            tick=1000,
            attacker_player_id=attacker,
            victim_player_id=victim,
            attacker_side=attacker_side,
            victim_side=victim_side,
            attacker_place=attacker_place,
            victim_place=victim_place,
            weapon="ak47",
            is_opening=opening,
        )
    )


def position_row(
    session: Session, player_id: int, map_name: str, side: Side, place: str, window_end: date = AS_OF
) -> PositionAggregate:
    row = session.scalar(
        select(PositionAggregate).where(
            PositionAggregate.player_id == player_id,
            PositionAggregate.map_name == map_name,
            PositionAggregate.side == side,
            PositionAggregate.place == place,
            PositionAggregate.window_end == window_end,
        )
    )
    assert row is not None, f"no positions_agg row for {player_id}/{map_name}/{side}/{place}"
    return row


def duel_row(
    session: Session, player_id: int, opponent_id: int, map_name: str, window_end: date = AS_OF
) -> DuelAggregate:
    row = session.scalar(
        select(DuelAggregate).where(
            DuelAggregate.player_id == player_id,
            DuelAggregate.opponent_player_id == opponent_id,
            DuelAggregate.map_name == map_name,
            DuelAggregate.window_end == window_end,
        )
    )
    assert row is not None, f"no duels_agg row for {player_id} vs {opponent_id} on {map_name}"
    return row


@pytest.fixture
def seeded(session: Session) -> SeededMatch:
    return seed_match(session, maps=("mirage", "nuke"), played_at=at(10))


# ------------------------------------------------------------------------------------- positions


def test_position_aggregates_presence_and_kills(session: Session, seeded: SeededMatch) -> None:
    mirage, nuke = seeded.map_ids[1], seeded.map_ids[2]
    a1, b1 = seeded.player_ids["a1"], seeded.player_ids["b1"]
    for number in range(1, 9):  # 8 CT rounds on mirage: 6 x Window, 2 x Connector
        add_sample(session, mirage, a1, number, "Window" if number <= 6 else "Connector")
        add_sample(session, mirage, a1, number, "CTSpawn", second=10)  # another sample second: ignored
    for number in range(13, 17):  # 4 T rounds
        add_sample(session, mirage, a1, number, "TRamp", side=Side.T)
    add_sample(session, nuke, a1, 1, "Heaven")
    add_kill(session, mirage, a1, b1, opening=True)
    add_kill(session, mirage, a1, b1)
    add_kill(
        session,
        mirage,
        b1,
        a1,
        attacker_place="Middle",
        victim_place="Window",
        attacker_side=Side.T,
        victim_side=Side.CT,
        opening=True,
    )
    add_kill(session, mirage, a1, b1, attacker_place="Jungle")  # a place a1 was never sampled in
    session.flush()

    written = rebuild_position_aggregates(session, window_days=120, sample_second=20, as_of=AS_OF)

    window = position_row(session, a1, "mirage", Side.CT, "Window")
    assert window.rounds_sampled == 8
    assert window.presence_share == pytest.approx(6 / 8)
    assert (window.kills, window.opening_kills, window.deaths, window.opening_deaths) == (2, 1, 1, 1)
    assert (window.window_start, window.window_end) == (AS_OF - timedelta(days=120), AS_OF)
    connector = position_row(session, a1, "mirage", Side.CT, "Connector")
    assert connector.presence_share == pytest.approx(2 / 8) and connector.kills == 0
    jungle = position_row(session, a1, "mirage", Side.CT, "Jungle")
    assert (jungle.presence_share, jungle.rounds_sampled, jungle.kills) == (0.0, 8, 1)
    ramp = position_row(session, a1, "mirage", Side.T, "TRamp")
    assert (ramp.rounds_sampled, ramp.presence_share) == (4, 1.0)
    assert position_row(session, a1, "nuke", Side.CT, "Heaven").rounds_sampled == 1
    # b1 was never sampled but has kills / deaths in "Middle" as T
    middle = position_row(session, b1, "mirage", Side.T, "Middle")
    assert (middle.kills, middle.opening_kills, middle.deaths, middle.opening_deaths) == (1, 1, 3, 1)
    assert (middle.rounds_sampled, middle.presence_share) == (0, 0.0)
    assert written == session.scalar(select(func.count()).select_from(PositionAggregate)) == 6
    # the presence shares of one player / map / side add up to one
    shares = session.scalars(
        select(PositionAggregate.presence_share).where(
            PositionAggregate.player_id == a1,
            PositionAggregate.map_name == "mirage",
            PositionAggregate.side == Side.CT,
        )
    ).all()
    assert sum(shares) == pytest.approx(1.0)


def test_position_aggregates_other_sample_second(session: Session, seeded: SeededMatch) -> None:
    a1 = seeded.player_ids["a1"]
    add_sample(session, seeded.map_ids[1], a1, 1, "CTSpawn", second=10)
    add_sample(session, seeded.map_ids[1], a1, 1, "Window", second=20)
    session.flush()
    rebuild_position_aggregates(session, sample_second=10, as_of=AS_OF)
    places = set(session.scalars(select(PositionAggregate.place)))
    assert places == {"CTSpawn"}


def test_samples_without_a_place_count_in_the_denominator_only(session: Session, seeded: SeededMatch) -> None:
    a1 = seeded.player_ids["a1"]
    add_sample(session, seeded.map_ids[1], a1, 1, "Window")
    add_sample(session, seeded.map_ids[1], a1, 2, None)
    session.flush()
    assert rebuild_position_aggregates(session, as_of=AS_OF) == 1
    row = position_row(session, a1, "mirage", Side.CT, "Window")
    assert (row.rounds_sampled, row.presence_share) == (2, 0.5)


def test_team_kills_and_unknown_players_are_ignored(session: Session, seeded: SeededMatch) -> None:
    mirage = seeded.map_ids[1]
    a1, a2, b1 = (seeded.player_ids[n] for n in ("a1", "a2", "b1"))
    add_kill(session, mirage, a1, a2, victim_side=Side.CT)  # team kill
    add_kill(session, mirage, a1, a1, victim_side=Side.CT)  # suicide
    add_kill(session, mirage, None, b1, attacker_side=None, attacker_place=None)  # the bomb
    add_kill(
        session, mirage, None, b1
    )  # attacker not linked to a player row: the victim's death still counts
    add_kill(session, mirage, a1, None)  # victim not linked: the kill still counts
    add_kill(session, mirage, a1, b1, attacker_side=None)  # side unknown: cannot be attributed
    session.flush()

    rebuild_position_aggregates(session, as_of=AS_OF)
    rebuild_duel_aggregates(session, as_of=AS_OF)
    window = position_row(session, a1, "mirage", Side.CT, "Window")
    assert (window.kills, window.deaths) == (1, 0)
    middle = position_row(session, b1, "mirage", Side.T, "Middle")
    assert (middle.kills, middle.deaths) == (0, 1)
    assert session.scalar(select(func.count()).select_from(PositionAggregate)) == 2
    assert session.scalar(select(func.count()).select_from(DuelAggregate)) == 0


# ----------------------------------------------------------------------------------------- duels


def test_duel_aggregates_per_map_and_overall(session: Session, seeded: SeededMatch) -> None:
    mirage, nuke = seeded.map_ids[1], seeded.map_ids[2]
    a1, b1, b2 = (seeded.player_ids[n] for n in ("a1", "b1", "b2"))
    add_kill(session, mirage, a1, b1, opening=True)
    add_kill(session, mirage, a1, b1)
    add_kill(session, mirage, a1, b1)
    add_kill(session, mirage, b1, a1, attacker_side=Side.T, victim_side=Side.CT, opening=True)
    add_kill(session, nuke, b1, a1, attacker_side=Side.T, victim_side=Side.CT)
    add_kill(session, nuke, a1, b2)
    session.flush()

    written = rebuild_duel_aggregates(session, window_days=120, as_of=AS_OF)

    on_mirage = duel_row(session, a1, b1, "mirage")
    assert (on_mirage.kills, on_mirage.deaths, on_mirage.opening_kills, on_mirage.opening_deaths) == (
        3,
        1,
        1,
        1,
    )
    mirrored = duel_row(session, b1, a1, "mirage")
    assert (mirrored.kills, mirrored.deaths, mirrored.opening_kills, mirrored.opening_deaths) == (1, 3, 1, 1)
    on_nuke = duel_row(session, a1, b1, "nuke")
    assert (on_nuke.kills, on_nuke.deaths) == (0, 1)
    overall = duel_row(session, a1, b1, ALL_MAPS)
    assert (overall.kills, overall.deaths, overall.opening_kills, overall.opening_deaths) == (3, 2, 1, 1)
    assert ALL_MAPS == "*"
    one_sided = duel_row(session, b2, a1, "nuke")
    assert (one_sided.kills, one_sided.deaths) == (0, 1)
    # pairs: (a1,b1) on mirage, nuke, *  and (a1,b2) on nuke, *  -> 5 pairs, two rows each
    assert written == session.scalar(select(func.count()).select_from(DuelAggregate)) == 10


# ---------------------------------------------------------------------------------------- windows


def test_window_excludes_the_as_of_day_and_old_maps(session: Session) -> None:
    """Rows for window_end D only contain maps played before D: usable for matches on D."""
    inside = seed_match(session, played_at=at(1))  # the day before
    first_day = seed_match(session, alpha_name="C", bravo_name="D", played_at=at(30))  # exactly window start
    same_day = seed_match(session, alpha_name="E", bravo_name="F", played_at=at(0) + timedelta(hours=3))
    too_old = seed_match(session, alpha_name="G", bravo_name="H", played_at=at(30) - timedelta(seconds=1))
    for seeded in (inside, first_day, same_day, too_old):
        add_kill(session, seeded.map_ids[1], seeded.player_ids["a1"], seeded.player_ids["b1"])
        add_sample(session, seeded.map_ids[1], seeded.player_ids["a1"], 1, "Window")
    session.flush()

    rebuild_duel_aggregates(session, window_days=30, as_of=AS_OF)
    rebuild_position_aggregates(session, window_days=30, as_of=AS_OF)
    duel_players = set(session.scalars(select(DuelAggregate.player_id)))
    assert duel_players == {
        inside.player_ids["a1"],
        inside.player_ids["b1"],
        first_day.player_ids["a1"],
        first_day.player_ids["b1"],
    }
    position_players = set(
        session.scalars(select(PositionAggregate.player_id).where(PositionAggregate.rounds_sampled > 0))
    )
    assert position_players == {inside.player_ids["a1"], first_day.player_ids["a1"]}


def test_map_date_falls_back_to_the_match_date(session: Session) -> None:
    seeded = seed_match(session, played_at=at(5))
    session.get(MapPlayed, seeded.map_ids[1]).played_at = None
    add_kill(session, seeded.map_ids[1], seeded.player_ids["a1"], seeded.player_ids["b1"])
    session.flush()
    assert rebuild_duel_aggregates(session, window_days=10, as_of=AS_OF) == 4
    assert rebuild_duel_aggregates(session, window_days=3, as_of=AS_OF) == 0


def test_default_as_of_is_today(session: Session) -> None:
    seeded = seed_match(session, played_at=datetime.now(UTC) - timedelta(days=2))
    add_kill(session, seeded.map_ids[1], seeded.player_ids["a1"], seeded.player_ids["b1"])
    session.flush()
    assert rebuild_duel_aggregates(session) == 4
    assert set(session.scalars(select(DuelAggregate.window_end))) == {datetime.now(UTC).date()}


def test_invalid_window_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError):
        rebuild_duel_aggregates(session, window_days=0)
    with pytest.raises(ValueError):
        rebuild_position_aggregates(session, window_days=-5)


# ------------------------------------------------------------------------------------ idempotency


def test_rebuilding_replaces_rows_of_the_same_window_end(session: Session, seeded: SeededMatch) -> None:
    mirage = seeded.map_ids[1]
    a1, b1 = seeded.player_ids["a1"], seeded.player_ids["b1"]
    add_kill(session, mirage, a1, b1)
    add_sample(session, mirage, a1, 1, "Window")
    session.flush()

    for _ in range(2):
        assert rebuild_position_aggregates(session, as_of=AS_OF) == 2
        assert rebuild_duel_aggregates(session, as_of=AS_OF) == 4
    session.commit()
    assert session.scalar(select(func.count()).select_from(PositionAggregate)) == 2
    assert session.scalar(select(func.count()).select_from(DuelAggregate)) == 4

    # new data + rebuild: the rows are updated, not duplicated
    add_kill(session, mirage, a1, b1, opening=True)
    session.flush()
    rebuild_duel_aggregates(session, as_of=AS_OF)
    rebuild_position_aggregates(session, as_of=AS_OF)
    session.commit()
    assert duel_row(session, a1, b1, "mirage").kills == 2
    assert position_row(session, a1, "mirage", Side.CT, "Window").kills == 2
    assert session.scalar(select(func.count()).select_from(DuelAggregate)) == 4


def test_other_window_ends_are_kept_and_can_be_pruned(session: Session, seeded: SeededMatch) -> None:
    mirage = seeded.map_ids[1]
    a1, b1 = seeded.player_ids["a1"], seeded.player_ids["b1"]
    add_kill(session, mirage, a1, b1)
    add_sample(session, mirage, a1, 1, "Window")
    session.flush()
    earlier = AS_OF - timedelta(days=5)
    for as_of in (earlier, AS_OF):
        rebuild_duel_aggregates(session, as_of=as_of)
        rebuild_position_aggregates(session, as_of=as_of)
    assert set(session.scalars(select(DuelAggregate.window_end))) == {earlier, AS_OF}
    assert set(session.scalars(select(PositionAggregate.window_end))) == {earlier, AS_OF}
    assert duel_row(session, a1, b1, "mirage", earlier).kills == 1

    rebuild_duel_aggregates(session, as_of=AS_OF, prune_before=AS_OF)
    rebuild_position_aggregates(session, as_of=AS_OF, prune_before=AS_OF)
    assert set(session.scalars(select(DuelAggregate.window_end))) == {AS_OF}
    assert set(session.scalars(select(PositionAggregate.window_end))) == {AS_OF}


def test_empty_database(session: Session) -> None:
    assert rebuild_position_aggregates(session, as_of=AS_OF) == 0
    assert rebuild_duel_aggregates(session, as_of=AS_OF) == 0


# ------------------------------------------------------------------------- parse -> persist -> agg


def test_aggregates_from_a_parsed_demo(session: Session) -> None:
    seeded = seed_match(session, played_at=at(3))
    window = ("Window", 10.0, 20.0, 30.0)
    builder = DemoBuilder()
    for number in range(12):
        kills = [K(30, "a1", "b1"), K(40, "b2", "a2")] if number < 3 else []
        builder.add_round("CT", kills=kills, positions={"a1": window} if number < 9 else {})
    builder.swap_sides()
    builder.add_round("T", kills=[K(25, "b1", "a1")])
    parsed = parse_demo("d.dem", DemoSettings(), parser_factory=factory_for(builder.build()))
    persist_parsed_demo(session, seeded.map_ids[1], parsed, seeded.steamid_map)

    rebuild_position_aggregates(session, as_of=AS_OF)
    rebuild_duel_aggregates(session, as_of=AS_OF)
    a1, b1 = seeded.player_ids["a1"], seeded.player_ids["b1"]
    row = position_row(session, a1, "mirage", Side.CT, "Window")
    assert row.rounds_sampled == 12
    assert row.presence_share == pytest.approx(9 / 12)
    assert (row.kills, row.opening_kills) == (3, 3)
    assert position_row(session, a1, "mirage", Side.CT, "CTSpawn").presence_share == pytest.approx(3 / 12)
    duel = duel_row(session, a1, b1, "mirage")
    assert (duel.kills, duel.deaths, duel.opening_kills, duel.opening_deaths) == (3, 1, 3, 1)
    assert duel_row(session, b1, a1, ALL_MAPS).kills == 1
