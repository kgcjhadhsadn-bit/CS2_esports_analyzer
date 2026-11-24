"""Regression tests for the issues found in review round 1 of the demos package."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import DemoSettings, Settings
from cs2bot.db.base import Base
from cs2bot.db.models import Demo, DuelAggregate, Kill, PositionAggregate
from cs2bot.db.session import make_engine
from cs2bot.domain.enums import DemoStatus
from cs2bot.ingest.demos.aggregates import rebuild_duel_aggregates, rebuild_position_aggregates
from cs2bot.ingest.demos.parse import ParsedDemo, parse_demo
from cs2bot.ingest.demos.persist import persist_parsed_demo
from cs2bot.ingest.demos.pipeline import PipelineOptions, process_pending_demos
from tests.ingest.demos.demos_builders import DemoBuilder, FakeParser, H, K, factory_for, sid
from tests.ingest.demos.demos_db import PLAYED_AT, seed_match

DEMO_MAGIC = b"PBDEMS2\x00"


def parse(builder: DemoBuilder) -> ParsedDemo:
    return parse_demo("x.dem", DemoSettings(), parser_factory=factory_for(builder.build()))


# ------------------------------------------------------------------------------------------- parsing


def test_late_demo_opening_with_a_knife_kill_is_not_a_knife_round() -> None:
    """A demo that starts recording at round 5 must not lose round 5 nor shift the later numbers."""
    builder = DemoBuilder()
    builder.restore(4)
    builder.add_round(
        "CT",
        kills=[K(10, "a1", "b1", weapon="knife")],
        hurts=[H(9, "a1", "b1", 100, 0, weapon="knife")],
    )
    builder.add_rounds("CCCCCCC")  # rounds 6..12
    builder.swap_sides()
    builder.add_rounds("TTTTTT")  # rounds 13..18
    parsed = parse(builder)
    numbers = parsed.rounds["round_number"].tolist()
    assert numbers == list(range(5, 19))
    assert parsed.dropped_rounds["knife"] == 0
    pistol = parsed.rounds.set_index("round_number").loc[13]
    assert pistol["ct_buy_type"] == "pistol" and pistol["t_buy_type"] == "pistol"


def test_trade_on_the_same_tick_is_found_whatever_the_event_order() -> None:
    """a1 kills b1 on the very tick b1 kills a2, listed first: b1's kill of a2 is still traded."""
    builder = DemoBuilder()
    builder.add_round("CT", kills=[K(20, "a1", "b1"), K(20, "b1", "a2")])
    builder.add_rounds("CCCCCCCCCCCC")
    parser: FakeParser = builder.build()
    # The builder sorts kills by time (stable), so the event order above is kept.
    parsed = parse_demo("x.dem", DemoSettings(), parser_factory=factory_for(parser))
    kills = parsed.kills[parsed.kills["round_number"] == 1].set_index("victim_steamid")
    assert bool(kills.loc[sid("b1"), "is_trade"]) is True
    assert bool(kills.loc[sid("a2"), "was_traded"]) is True
    a2 = parsed.player_rounds[
        (parsed.player_rounds["round_number"] == 1) & (parsed.player_rounds["steamid"] == sid("a2"))
    ].iloc[0]
    assert bool(a2["kast"]) is True


def test_bomb_blast_death_is_a_death_but_nobody_s_kill() -> None:
    builder = DemoBuilder()
    builder.add_round(
        "T", "bomb_exploded", kills=[K(50, "b1", "a1", weapon="planted_c4")], plant=(30, "b1", 292)
    )
    builder.add_rounds("CCCCCCCCCCCC")
    parsed = parse(builder)
    rows = parsed.player_rounds
    first = rows[rows["round_number"] == 1].set_index("steamid")
    assert int(first.loc[sid("b1"), "kills"]) == 0
    assert int(first.loc[sid("a1"), "deaths"]) == 1
    kill = parsed.kills.iloc[0]
    assert not kill["is_enemy_kill"] and not kill["is_opening"]


def _bomb_blast_demo() -> DemoBuilder:
    """Round 1: b1 plants and the blast kills a1; the event names the planter as the attacker."""
    builder = DemoBuilder()
    builder.add_round(
        "T",
        "bomb_exploded",
        kills=[K(20, "a2", "b2"), K(50, "b1", "a1", weapon="planted_c4")],
        plant=(30, "b1", 292),
    )
    builder.add_rounds("CCCCCCCCCCCC")
    return builder


def test_bomb_blast_kill_row_names_no_attacker() -> None:
    """Consumers of ``kills`` count every row with differing sides as a kill (review round 1, pass 2)."""
    parsed = parse(_bomb_blast_demo())
    blast = parsed.kills[parsed.kills["weapon"] == "planted_c4"].iloc[0]
    assert blast["attacker_steamid"] is None
    assert pd.isna(blast["attacker_side"]) and pd.isna(blast["attacker_place"]) and pd.isna(blast["distance"])
    assert blast["victim_steamid"] == sid("a1") and blast["victim_side"] == "ct"
    gun = parsed.kills[parsed.kills["weapon"] == "ak47"].iloc[0]
    assert gun["attacker_steamid"] == sid("a2") and gun["attacker_side"] == "ct"


def test_bomb_blast_death_is_no_kill_in_the_stored_rows_and_aggregates(session: Session) -> None:
    seeded = seed_match(session)
    parsed = parse(_bomb_blast_demo())
    persist_parsed_demo(session, seeded.map_ids[1], parsed, seeded.steamid_map)
    blast = session.scalars(select(Kill).where(Kill.weapon == "planted_c4")).one()
    assert blast.attacker_player_id is None and blast.attacker_side is None
    assert blast.victim_player_id == seeded.player_ids["a1"]

    as_of = PLAYED_AT.date() + timedelta(days=1)
    rebuild_duel_aggregates(session, as_of=as_of)
    planter, victim = seeded.player_ids["b1"], seeded.player_ids["a1"]
    duel = session.scalars(
        select(DuelAggregate).where(
            DuelAggregate.player_id == planter, DuelAggregate.opponent_player_id == victim
        )
    ).all()
    assert duel == []
    rebuild_position_aggregates(session, as_of=as_of)
    planter_kills = session.scalar(
        select(func.coalesce(func.sum(PositionAggregate.kills), 0)).where(
            PositionAggregate.player_id == planter
        )
    )
    assert planter_kills == 0


# ------------------------------------------------------------------------------------------ pipeline


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(f"sqlite:///{tmp_path / 'review.sqlite'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    engine.dispose()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ftp://x.org/a.rar", "http://"])
def test_non_http_demo_urls_are_never_handed_to_a_downloader(
    factory: sessionmaker[Session], tmp_path: Path, url: str
) -> None:
    with factory() as session:
        seeded = seed_match(session)
        demo = Demo(match_id=seeded.match_id, url=url)
        session.add(demo)
        session.commit()
        demo_id = demo.id
    calls: list[str] = []

    def downloader(u: str, _dest: Path) -> Path:
        calls.append(u)
        raise AssertionError("must not be called")

    settings = Settings(demos=DemoSettings(storage_dir=tmp_path / "demos"))
    report = process_pending_demos(
        factory, settings, downloader=downloader, options=PipelineOptions(min_free_bytes=0)
    )
    assert calls == [] and report.unavailable == 1
    with factory() as session:
        row = session.get(Demo, demo_id)
        assert row is not None and row.status is DemoStatus.UNAVAILABLE and row.attempts == 0


def test_protocol_relative_url_gets_https(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as session:
        seeded = seed_match(session)
        session.add(Demo(match_id=seeded.match_id, url="//dl.example.org/demo/9"))
        session.commit()
    calls: list[str] = []

    def downloader(u: str, dest: Path) -> Path:
        calls.append(u)
        raise RuntimeError("offline")

    settings = Settings(demos=DemoSettings(storage_dir=tmp_path / "demos"))
    process_pending_demos(factory, settings, downloader=downloader, options=PipelineOptions(min_free_bytes=0))
    assert calls == ["https://dl.example.org/demo/9"]


def test_files_outside_the_work_directory_are_never_deleted(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A downloader returning a file it keeps elsewhere (a cache) must not lose that file."""
    with factory() as session:
        seeded = seed_match(session)
        session.add(Demo(match_id=seeded.match_id, url="https://dl.example.org/demo/1"))
        session.commit()
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / "alpha-vs-bravo-mirage.dem"
    cached.write_bytes(DEMO_MAGIC + b"cached")

    def downloader(_u: str, _dest: Path) -> Path:
        return cached

    builder = DemoBuilder("de_mirage")
    builder.add_rounds("CCCCCCCCCCCCC")
    settings = Settings(demos=DemoSettings(storage_dir=tmp_path / "demos"))
    report = process_pending_demos(
        factory,
        settings,
        downloader=downloader,
        parser_factory=factory_for(builder.build()),
        options=PipelineOptions(min_free_bytes=0),
    )
    assert report.parsed == 1
    assert cached.is_file()


def test_blocked_download_keeps_the_previous_error(factory: sessionmaker[Session], tmp_path: Path) -> None:
    from cs2bot.ingest.demos.errors import DemoDownloadBlockedError

    with factory() as session:
        seeded = seed_match(session)
        demo = Demo(
            match_id=seeded.match_id,
            url="https://dl.example.org/demo/1",
            status=DemoStatus.FAILED,
            attempts=1,
            error="earlier failure",
        )
        session.add(demo)
        session.commit()
        demo_id = demo.id

    def downloader(_u: str, _dest: Path) -> Path:
        raise DemoDownloadBlockedError("challenge")

    settings = Settings(demos=DemoSettings(storage_dir=tmp_path / "demos"))
    report = process_pending_demos(
        factory, settings, downloader=downloader, options=PipelineOptions(min_free_bytes=0)
    )
    assert report.blocked
    with factory() as session:
        row = session.get(Demo, demo_id)
        assert row is not None
        assert (row.status, row.attempts, row.error) == (DemoStatus.FAILED, 1, "earlier failure")
