"""``process_pending_demos`` with a fake downloader and a fake parser: success, failure, retries, clean-up."""

from __future__ import annotations

import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import DemoSettings, Settings
from cs2bot.db.base import Base
from cs2bot.db.models import Demo, Kill, MapPlayed, Match, Player, PlayerRoundStats, Round, Team
from cs2bot.db.session import make_engine
from cs2bot.domain.enums import DataSource, DemoStatus, MatchStatus
from cs2bot.ingest.demos.parse import demoparser_version
from cs2bot.ingest.demos.pipeline import (
    PipelineOptions,
    ProcessReport,
    process_pending_demos,
    select_pending_demo_ids,
)
from tests.ingest.demos.demos_builders import DemoBuilder, FakeParser, K, sid, standard_match
from tests.ingest.demos.demos_db import SeededMatch, seed_match

DEMO_MAGIC = b"PBDEMS2\x00"
OPTIONS = PipelineOptions(min_free_bytes=0)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A file database: the pipeline opens its own short-lived sessions."""
    engine = make_engine(f"sqlite:///{tmp_path / 'pipeline.sqlite'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    engine.dispose()


@pytest.fixture
def demo_settings(tmp_path: Path) -> Settings:
    return Settings(demos=DemoSettings(storage_dir=tmp_path / "demos"))


def add_demo(
    session: Session, match_id: int, *, url: str | None = "https://dl.example.org/demo/1", **fields: Any
) -> int:
    demo = Demo(match_id=match_id, url=url, **fields)
    session.add(demo)
    session.flush()
    return demo.id


def seed(factory: sessionmaker[Session], **kwargs: Any) -> tuple[SeededMatch, int]:
    with factory() as session:
        seeded = seed_match(session, **kwargs)
        demo_id = add_demo(session, seeded.match_id)
        session.commit()
    return seeded, demo_id


class ZipDownloader:
    """Downloader that writes a zip with one fake ``.dem`` per given file name."""

    def __init__(self, names: list[str], *, fail: Exception | None = None) -> None:
        self.names = names
        self.fail = fail
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, url: str, dest_dir: Path) -> Path:
        self.calls.append((url, dest_dir))
        if self.fail is not None:
            raise self.fail
        target = dest_dir / "match-archive.zip"
        with zipfile.ZipFile(target, "w") as zf:
            for name in self.names:
                zf.writestr(name, DEMO_MAGIC + name.encode())
        return target


def parser_factory_by_map(parsers: dict[str, FakeParser | Exception]) -> Callable[[str], FakeParser]:
    """``parser_factory`` choosing the fake by the map name in the file name."""

    def make(path: str) -> FakeParser:
        for key, parser in parsers.items():
            if key in Path(path).name:
                if isinstance(parser, Exception):
                    raise parser
                return parser
        raise AssertionError(f"unexpected demo file {path}")

    return make


def two_map_parsers() -> dict[str, FakeParser | Exception]:
    mirage = DemoBuilder("de_mirage")
    mirage.add_round("CT", kills=[K(20, "a1", "b1")])
    mirage.add_rounds("CCCCCCTTTTT")
    mirage.swap_sides()
    mirage.add_rounds("TTTTTT")
    return {"mirage": mirage.build(), "inferno": standard_match("de_inferno").build()}


def get_demo(factory: sessionmaker[Session], demo_id: int) -> Demo:
    with factory() as session:
        demo = session.get(Demo, demo_id)
        assert demo is not None
        return demo


def run(
    factory: sessionmaker[Session],
    settings: Settings,
    downloader: Callable[[str, Path], Path] | None,
    parsers: dict[str, FakeParser | Exception],
    **kwargs: Any,
) -> ProcessReport:
    kwargs.setdefault("options", OPTIONS)
    return process_pending_demos(
        factory, settings, downloader=downloader, parser_factory=parser_factory_by_map(parsers), **kwargs
    )


# ---------------------------------------------------------------------------------------- success


def test_pending_demo_is_downloaded_parsed_and_cleaned_up(
    factory: sessionmaker[Session], demo_settings: Settings, tmp_path: Path
) -> None:
    seeded, demo_id = seed(factory, maps=("mirage", "inferno"))
    downloader = ZipDownloader(["alpha-vs-bravo-m2-inferno.dem", "alpha-vs-bravo-m1-mirage.dem"])
    before = datetime.now(UTC) - timedelta(seconds=5)
    report = run(factory, demo_settings, downloader, two_map_parsers())

    assert (report.selected, report.parsed, report.failed, report.unavailable) == (1, 1, 0, 0)
    assert (report.maps_persisted, report.rounds_persisted) == (2, 36)
    assert report.ok and report.items == 1 and not report.blocked
    assert report.demo_ids == [demo_id] and report.match_ids == [seeded.match_id]
    assert report.as_dict()["parsed"] == 1

    demo = get_demo(factory, demo_id)
    assert demo.status is DemoStatus.PARSED
    assert demo.parser_version == demoparser_version() and demo.parser_version
    assert demo.attempts == 1 and demo.error is None
    assert demo.parsed_at is not None and demo.parsed_at >= before
    assert demo.downloaded_at is not None and demo.size_bytes and demo.size_bytes > 0
    assert demo.dem_files == ["alpha-vs-bravo-m1-mirage.dem", "alpha-vs-bravo-m2-inferno.dem"]
    assert demo.archive_path is None  # the archive is gone

    with factory() as session:
        for number in (1, 2):
            rounds = session.scalars(select(Round).where(Round.map_played_id == seeded.map_ids[number])).all()
            assert len(rounds) == 18 and {r.source for r in rounds} == {DataSource.DEMO}
        assert session.scalar(select(func.count()).select_from(Kill)) == 1
        kill = session.scalar(select(Kill))
        assert kill.map_played_id == seeded.map_ids[1]
        assert (kill.attacker_player_id, kill.victim_player_id) == (
            seeded.player_ids["a1"],
            seeded.player_ids["b1"],
        )
        assert session.scalar(select(func.count()).select_from(PlayerRoundStats)) == 360
        # steam ids learned from the demo are remembered for the next one
        assert session.get(Player, seeded.player_ids["a1"]).extra["steamid"] == str(sid("a1"))
        assert session.get(MapPlayed, seeded.map_ids[2]).extra["demo"]["rounds"] == 18

    # the downloader got a per-demo working directory below the storage dir, and nothing is left in it
    ((url, work_dir),) = downloader.calls
    assert url == "https://dl.example.org/demo/1"
    storage = tmp_path / "demos"
    assert storage in work_dir.parents
    assert [p for p in storage.rglob("*") if p.is_file()] == []
    assert not work_dir.exists()


def test_keep_flags_keep_the_raw_files(factory: sessionmaker[Session], tmp_path: Path) -> None:
    _seeded, demo_id = seed(factory)
    settings = Settings(
        demos=DemoSettings(storage_dir=tmp_path / "demos", keep_archives=True, keep_dem_files=True)
    )
    report = run(factory, settings, ZipDownloader(["a-vs-b-mirage.dem"]), two_map_parsers())
    assert report.parsed == 1

    files = sorted(p.name for p in (tmp_path / "demos").rglob("*") if p.is_file())
    assert files == ["a-vs-b-mirage.dem", "match-archive.zip"]
    demo = get_demo(factory, demo_id)
    assert demo.archive_path is not None and Path(demo.archive_path).is_file()

    # only the archive
    _seeded2, _demo2 = seed(factory, alpha_name="Gamma", bravo_name="Delta")
    settings = Settings(demos=DemoSettings(storage_dir=tmp_path / "demos2", keep_archives=True))
    run(factory, settings, ZipDownloader(["a-vs-b-mirage.dem"]), two_map_parsers())
    assert sorted(p.name for p in (tmp_path / "demos2").rglob("*") if p.is_file()) == ["match-archive.zip"]
    # only the demo files
    _seeded3, demo3 = seed(factory, alpha_name="Epsilon", bravo_name="Zeta")
    settings = Settings(demos=DemoSettings(storage_dir=tmp_path / "demos3", keep_dem_files=True))
    run(factory, settings, ZipDownloader(["a-vs-b-mirage.dem"]), two_map_parsers())
    assert sorted(p.name for p in (tmp_path / "demos3").rglob("*") if p.is_file()) == ["a-vs-b-mirage.dem"]
    assert get_demo(factory, demo3).archive_path is None


def test_plain_dem_download(factory: sessionmaker[Session], demo_settings: Settings, tmp_path: Path) -> None:
    _seeded, demo_id = seed(factory)

    def plain(_url: str, dest_dir: Path) -> Path:
        target = dest_dir / "alpha-vs-bravo-mirage.dem"
        target.write_bytes(DEMO_MAGIC + b"plain")
        return target

    report = run(factory, demo_settings, plain, two_map_parsers())
    assert report.parsed == 1 and report.rounds_persisted == 18
    assert [p for p in (tmp_path / "demos").rglob("*") if p.is_file()] == []
    assert get_demo(factory, demo_id).dem_files == ["alpha-vs-bravo-mirage.dem"]


def test_relative_hltv_urls_are_made_absolute(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    with factory() as session:
        seeded = seed_match(session)
        add_demo(session, seeded.match_id, url="/download/demo/4711", hltv_demo_id=4711)
        session.commit()
    downloader = ZipDownloader(["x-mirage.dem"])
    run(factory, demo_settings, downloader, two_map_parsers())
    assert downloader.calls[0][0] == "https://www.hltv.org/download/demo/4711"


def test_processing_twice_does_nothing_the_second_time(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    seed(factory)
    downloader = ZipDownloader(["x-mirage.dem"])
    assert run(factory, demo_settings, downloader, two_map_parsers()).parsed == 1
    second = run(factory, demo_settings, downloader, two_map_parsers())
    assert (second.selected, second.parsed) == (0, 0)
    assert len(downloader.calls) == 1


# --------------------------------------------------------------------------------------- failures


def test_download_failure_marks_failed_and_retries_three_times(
    factory: sessionmaker[Session], demo_settings: Settings, tmp_path: Path
) -> None:
    _seeded, demo_id = seed(factory)
    broken = ZipDownloader([], fail=ConnectionError("connection reset by peer"))

    for attempt in (1, 2, 3):
        report = run(factory, demo_settings, broken, two_map_parsers())
        assert (report.selected, report.parsed, report.failed) == (1, 0, 1)
        assert not report.ok and "connection reset by peer" in report.errors[0]
        demo = get_demo(factory, demo_id)
        assert demo.status is DemoStatus.FAILED
        assert demo.attempts == attempt
        assert demo.error == "ConnectionError: connection reset by peer"
        assert demo.parsed_at is None

    # the fourth run does not touch it any more
    report = run(factory, demo_settings, broken, two_map_parsers())
    assert report.selected == 0 and len(broken.calls) == 3
    assert get_demo(factory, demo_id).attempts == 3
    assert [p for p in (tmp_path / "demos").rglob("*") if p.is_file()] == []


def test_failed_demo_succeeds_on_a_later_attempt(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    _seeded, demo_id = seed(factory)
    run(factory, demo_settings, ZipDownloader([], fail=TimeoutError("timed out")), two_map_parsers())
    assert get_demo(factory, demo_id).status is DemoStatus.FAILED

    report = run(factory, demo_settings, ZipDownloader(["x-mirage.dem"]), two_map_parsers())
    assert report.parsed == 1
    demo = get_demo(factory, demo_id)
    assert (demo.status, demo.attempts, demo.error) == (DemoStatus.PARSED, 2, None)


def test_one_bad_demo_does_not_stop_the_batch(
    factory: sessionmaker[Session], demo_settings: Settings, tmp_path: Path
) -> None:
    with factory() as session:
        good = seed_match(session, played_at=datetime(2026, 3, 1, tzinfo=UTC))
        bad = seed_match(
            session, alpha_name="Gamma", bravo_name="Delta", played_at=datetime(2026, 3, 5, tzinfo=UTC)
        )
        ugly = seed_match(
            session, alpha_name="Eps", bravo_name="Zeta", played_at=datetime(2026, 3, 4, tzinfo=UTC)
        )
        good_id = add_demo(session, good.match_id, url="https://dl.example.org/good")
        bad_id = add_demo(session, bad.match_id, url="https://dl.example.org/corrupt")
        ugly_id = add_demo(session, ugly.match_id, url="https://dl.example.org/html")
        session.commit()

    def downloader(url: str, dest_dir: Path) -> Path:
        if url.endswith("corrupt"):
            target = dest_dir / "corrupt.zip"
            target.write_bytes(b"PK\x03\x04 this is not a zip file")
            return target
        if url.endswith("html"):
            target = dest_dir / "page.rar"
            target.write_text("<html>blocked</html>")
            return target
        return ZipDownloader(["x-mirage.dem"])(url, dest_dir)

    report = run(factory, demo_settings, downloader, two_map_parsers())

    assert (report.selected, report.parsed, report.failed) == (3, 1, 2)
    assert report.demo_ids == [bad_id, ugly_id, good_id]  # newest match first
    assert get_demo(factory, good_id).status is DemoStatus.PARSED
    bad_demo, ugly_demo = get_demo(factory, bad_id), get_demo(factory, ugly_id)
    assert bad_demo.status is DemoStatus.FAILED and "ArchiveError" in (bad_demo.error or "")
    assert ugly_demo.status is DemoStatus.FAILED and "HTML page" in (ugly_demo.error or "")
    assert len(report.errors) == 2
    assert [p for p in (tmp_path / "demos").rglob("*") if p.is_file()] == []  # failures are cleaned up too


def test_parse_failure_of_one_map_fails_the_demo_but_keeps_the_other_map(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    seeded, demo_id = seed(factory, maps=("mirage", "inferno"))
    parsers = two_map_parsers()
    parsers["inferno"] = RuntimeError("demo file is corrupt")
    report = run(factory, demo_settings, ZipDownloader(["a-m1-mirage.dem", "a-m2-inferno.dem"]), parsers)

    assert (report.parsed, report.failed, report.maps_persisted) == (0, 1, 1)
    demo = get_demo(factory, demo_id)
    assert demo.status is DemoStatus.FAILED and demo.attempts == 1
    assert "map 2 (a-m2-inferno.dem)" in (demo.error or "") and "corrupt" in (demo.error or "")
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Round).where(Round.map_played_id == seeded.map_ids[1])
            )
            == 18
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Round).where(Round.map_played_id == seeded.map_ids[2])
            )
            == 0
        )


def test_persist_failure_rolls_back_that_map_only(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    """No lineup, unknown clan names, no score: the orientation is unknown, nothing may be stored."""
    _seeded, _demo_id = seed(factory, with_lineups=False, alpha_name="Natus Vincere", bravo_name="FaZe")
    report = run(factory, demo_settings, ZipDownloader(["x-mirage.dem"]), two_map_parsers())
    assert report.failed == 1 and "cannot tell which demo roster is team1" in report.errors[0]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Round)) == 0
        assert session.scalar(select(func.count()).select_from(Kill)) == 0


def test_demo_without_matching_map_or_without_maps(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    _seeded, demo_id = seed(factory, maps=("nuke",))
    report = run(factory, demo_settings, ZipDownloader(["x-m1-mirage.dem"]), two_map_parsers())
    assert report.failed == 1
    assert "no demo file matches the maps" in (get_demo(factory, demo_id).error or "")

    _seeded2, demo2 = seed(factory, maps=(), alpha_name="Gamma", bravo_name="Delta")
    report = run(factory, demo_settings, ZipDownloader(["x-m1-mirage.dem"]), two_map_parsers())
    assert "has no maps_played rows yet" in (get_demo(factory, demo2).error or "")


def test_extra_demo_files_are_reported(factory: sessionmaker[Session], demo_settings: Settings) -> None:
    _seeded, _demo_id = seed(factory)
    report = run(
        factory, demo_settings, ZipDownloader(["a-m1-mirage.dem", "a-m2-inferno.dem"]), two_map_parsers()
    )
    assert report.parsed == 1
    assert any("a-m2-inferno.dem matches no map" in w for w in report.warnings)


def test_match_archive_lacking_a_map_is_reported(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    seeded, demo_id = seed(factory, maps=("mirage", "inferno", "nuke"))
    report = run(
        factory, demo_settings, ZipDownloader(["a-m1-mirage.dem", "a-m2-inferno.dem"]), two_map_parsers()
    )
    assert (report.parsed, report.maps_persisted) == (1, 2)
    assert get_demo(factory, demo_id).status is DemoStatus.PARSED
    assert [w for w in report.warnings if "no demo file for map" in w] == [
        f"demo {demo_id}: no demo file for map 3 (nuke)"
    ]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Round).where(Round.map_played_id == seeded.map_ids[3])
            )
            == 0
        )


def test_per_map_download_does_not_warn_about_the_other_maps(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    """HLTV also offers one download per map: such a demo row covers one map and that is fine."""
    seeded, _demo_id = seed(factory, maps=("mirage", "inferno"))
    report = run(factory, demo_settings, ZipDownloader(["a-m2-inferno.dem"]), two_map_parsers())
    assert (report.parsed, report.maps_persisted, report.warnings) == (1, 1, [])
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Round).where(Round.map_played_id == seeded.map_ids[2])
            )
            == 18
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Round).where(Round.map_played_id == seeded.map_ids[1])
            )
            == 0
        )


def test_demo_without_url_is_unavailable(factory: sessionmaker[Session], demo_settings: Settings) -> None:
    with factory() as session:
        seeded = seed_match(session)
        demo_id = add_demo(session, seeded.match_id, url=None)
        session.commit()
    report = run(factory, demo_settings, ZipDownloader([]), two_map_parsers())
    assert (report.unavailable, report.failed, report.parsed) == (1, 0, 0)
    assert get_demo(factory, demo_id).status is DemoStatus.UNAVAILABLE
    assert run(factory, demo_settings, ZipDownloader([]), two_map_parsers()).selected == 0


def test_blocked_source_stops_the_batch_without_using_an_attempt(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    class HLTVFetchError(Exception):
        pass

    class HLTVBlockedError(HLTVFetchError):  # same name as cs2bot.ingest.hltv.errors.HLTVBlockedError
        pass

    with factory() as session:
        first = seed_match(session, played_at=datetime(2026, 3, 9, tzinfo=UTC))
        second = seed_match(
            session, alpha_name="Gamma", bravo_name="Delta", played_at=datetime(2026, 3, 1, tzinfo=UTC)
        )
        first_id = add_demo(session, first.match_id)
        second_id = add_demo(session, second.match_id)
        session.commit()
    downloader = ZipDownloader([], fail=HLTVBlockedError("challenge did not clear"))
    report = run(factory, demo_settings, downloader, two_map_parsers())

    assert report.blocked and not report.ok
    assert (report.parsed, report.failed) == (0, 0)
    assert len(downloader.calls) == 1  # the second demo was not even tried
    for demo_id in (first_id, second_id):
        demo = get_demo(factory, demo_id)
        assert (demo.status, demo.attempts) == (DemoStatus.PENDING, 0)


def test_plain_http_download_of_a_protected_source_is_a_block_not_a_failure(
    factory: sessionmaker[Session], demo_settings: Settings, respx_mock: Any
) -> None:
    """Without a browser downloader HLTV answers 403: the demo must stay PENDING for a later run."""
    import httpx

    _seeded, demo_id = seed(factory)
    respx_mock.get("https://dl.example.org/demo/1").mock(return_value=httpx.Response(403, content=b"denied"))
    report = run(factory, demo_settings, None, two_map_parsers())
    assert report.blocked and (report.failed, report.parsed) == (0, 0)
    assert any("browser downloader" in w for w in report.warnings)
    demo = get_demo(factory, demo_id)
    assert (demo.status, demo.attempts) == (DemoStatus.PENDING, 0)


def test_the_real_hltv_block_error_is_recognised(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    from cs2bot.ingest.hltv.errors import HLTVBlockedError

    seed(factory)
    try:
        error = HLTVBlockedError("blocked")
    except TypeError:  # the constructor takes more arguments
        error = HLTVBlockedError.__new__(HLTVBlockedError)
    report = run(factory, demo_settings, ZipDownloader([], fail=error), two_map_parsers())
    assert report.blocked


# -------------------------------------------------------------------------------------- selection


def test_selection_order_limit_and_eligibility(factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    with factory() as session:
        team_a, team_b = Team(name="A"), Team(name="B")
        session.add_all([team_a, team_b])
        session.flush()

        def match(days_ago: int) -> int:
            m = Match(
                team1_id=team_a.id,
                team2_id=team_b.id,
                scheduled_at=now - timedelta(days=days_ago),
                status=MatchStatus.FINISHED,
            )
            session.add(m)
            session.flush()
            return m.id

        ids = {
            "old_pending": add_demo(session, match(30)),
            "new_pending": add_demo(session, match(1)),
            "failed_retry": add_demo(session, match(0), status=DemoStatus.FAILED, attempts=2),
            "failed_final": add_demo(session, match(0), status=DemoStatus.FAILED, attempts=3),
            "parsed": add_demo(session, match(0), status=DemoStatus.PARSED, attempts=1),
            "unavailable": add_demo(session, match(0), status=DemoStatus.UNAVAILABLE),
            "stale_download": add_demo(session, match(2), status=DemoStatus.DOWNLOADING, attempts=1),
            "fresh_download": add_demo(session, match(2), status=DemoStatus.DOWNLOADED, attempts=1),
        }
        session.commit()
        session.execute(update(Demo).values(updated_at=now - timedelta(minutes=10)))
        session.execute(
            update(Demo).where(Demo.id == ids["stale_download"]).values(updated_at=now - timedelta(hours=5))
        )
        session.commit()

        selected = select_pending_demo_ids(session, limit=10, now=now)
        # untouched demos first (newest match first), then retries / interrupted ones
        assert selected == [
            ids["new_pending"],
            ids["old_pending"],
            ids["failed_retry"],
            ids["stale_download"],
        ]
        assert select_pending_demo_ids(session, limit=1, now=now) == [ids["new_pending"]]
        assert select_pending_demo_ids(session, limit=0, now=now) == []
        strict = PipelineOptions(max_attempts=2)
        assert ids["failed_retry"] not in select_pending_demo_ids(session, limit=10, now=now, options=strict)


def test_limit_is_respected(factory: sessionmaker[Session], demo_settings: Settings) -> None:
    with factory() as session:
        for index in range(3):
            seeded = seed_match(session, alpha_name=f"Alpha{index}", bravo_name=f"Bravo{index}")
            add_demo(session, seeded.match_id, url=f"https://dl.example.org/{index}")
        session.commit()
    downloader = ZipDownloader(["x-mirage.dem"])
    report = run(factory, demo_settings, downloader, two_map_parsers(), limit=2)
    assert (report.selected, report.parsed) == (2, 2)
    with factory() as session:
        statuses = sorted(d.status.value for d in session.scalars(select(Demo)))
    assert statuses == ["parsed", "parsed", "pending"]


def test_interrupted_demo_on_its_last_attempt_is_failed_not_retried_forever(
    factory: sessionmaker[Session], demo_settings: Settings
) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC)
    with factory() as session:
        seeded = seed_match(session)
        demo_id = add_demo(session, seeded.match_id, status=DemoStatus.DOWNLOADING, attempts=3)
        session.commit()
        session.execute(update(Demo).values(updated_at=now - timedelta(days=1)))
        session.commit()
    downloader = ZipDownloader(["x-mirage.dem"])
    report = run(factory, demo_settings, downloader, two_map_parsers(), now=now)
    assert report.failed == 1 and downloader.calls == []
    assert get_demo(factory, demo_id).status is DemoStatus.FAILED


def test_low_disk_space_pauses_processing(factory: sessionmaker[Session], demo_settings: Settings) -> None:
    _seeded, demo_id = seed(factory)
    downloader = ZipDownloader(["x-mirage.dem"])
    report = run(
        factory, demo_settings, downloader, two_map_parsers(), options=PipelineOptions(min_free_bytes=10**18)
    )
    assert report.parsed == 0 and downloader.calls == []
    assert any("demo processing paused" in w for w in report.warnings)
    assert get_demo(factory, demo_id).status is DemoStatus.PENDING


def test_default_httpx_downloader_is_used_when_none_is_injected(
    factory: sessionmaker[Session], demo_settings: Settings, respx_mock: Any
) -> None:
    import httpx

    _seeded, demo_id = seed(factory)
    respx_mock.get("https://dl.example.org/demo/1").mock(
        return_value=httpx.Response(
            200,
            content=DEMO_MAGIC + b"x" * 64,
            headers={"Content-Disposition": 'attachment; filename="a-mirage.dem"'},
        )
    )
    report = run(factory, demo_settings, None, two_map_parsers())
    assert report.parsed == 1
    assert get_demo(factory, demo_id).dem_files == ["a-mirage.dem"]
