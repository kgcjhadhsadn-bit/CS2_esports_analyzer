"""Regression tests for the issues found in review round 1 (second pass)."""

from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import HLTVSettings, Settings
from cs2bot.db.models import Demo, Match, Team, TeamRanking
from cs2bot.db.repo import resolve_team
from cs2bot.domain.enums import DataSource, MatchStatus
from cs2bot.ingest.hltv import (
    HLTVBlockedError,
    HLTVFetcher,
    HLTVSyncOptions,
    LoadedPage,
    sync_mapstats,
    sync_match,
    sync_rankings,
    sync_results,
)
from cs2bot.ingest.hltv.fetcher import FetchOptions
from cs2bot.ingest.hltv.parsers import parse_results_page
from tests.ingest.hltv.conftest import FakeClock, FakeFetcher, load_fixture

MATCH_START = datetime(2026, 11, 21, 16, 30, tzinfo=UTC)  # match_bo3_finished.html
SOON_AFTER = MATCH_START + timedelta(hours=3)
DEMO_LINK_RE = re.compile(
    r'<div class="stream-box"><a class="stream-box a-reset" href="/download/demo/\d+".*?</div>'
)


def _without_demos(html: str) -> str:
    stripped = DEMO_LINK_RE.sub("", html)
    assert "/download/demo/" not in stripped
    return stripped


def _match(session: Session, hltv_id: int) -> Match:
    row = session.scalar(select(Match).where(Match.hltv_id == hltv_id))
    assert row is not None
    return row


# ------------------------------------------------------------------------- final match pages / demos


def test_finished_match_without_demo_stays_incomplete_until_the_demo_appears(
    session: Session, app_settings: Settings
) -> None:
    no_demo = _without_demos(load_fixture("match_bo3_finished.html"))
    fetcher = FakeFetcher({r"^/matches/2378001(/|$)": no_demo})
    report = sync_match(session, fetcher, 2378001, settings=app_settings, now=SOON_AFTER)
    assert report.ok, report.errors
    m = _match(session, 2378001)
    assert m.status is MatchStatus.FINISHED
    # the result is in, but HLTV has not published the GOTV demo yet: keep re-syncing the match
    assert m.extra["hltv"]["complete"] is False
    assert not list(session.scalars(select(Demo).where(Demo.match_id == m.id)))

    later = SOON_AFTER + timedelta(hours=2)
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings, now=later)
    assert m.extra["hltv"]["complete"] is True
    assert len(list(session.scalars(select(Demo).where(Demo.match_id == m.id)))) == 3


def test_finished_match_without_demo_is_final_after_the_settle_time(
    session: Session, app_settings: Settings
) -> None:
    no_demo = _without_demos(load_fixture("match_bo3_finished.html"))
    opts = HLTVSyncOptions()
    late = MATCH_START + timedelta(hours=opts.finalize_after_hours + 1)
    sync_match(
        session, FakeFetcher({r"^/matches/2378001(/|$)": no_demo}), 2378001, settings=app_settings, now=late
    )
    assert _match(session, 2378001).extra["hltv"]["complete"] is True


def test_missing_map_stats_link_keeps_the_match_incomplete(session: Session, app_settings: Settings) -> None:
    html = load_fixture("match_bo3_finished.html")
    # the "Match over" banner is up but the stats link of the decider is not published yet
    first = html.index('href="/stats/matches/mapstatsid/210003/')
    start = html.rindex("<a", 0, first)
    end = html.index("</a>", first) + len("</a>")
    no_link = html[:start] + html[end:]
    assert "mapstatsid/210003" not in no_link
    sync_match(
        session,
        FakeFetcher({r"^/matches/2378001(/|$)": no_link}),
        2378001,
        settings=app_settings,
        now=SOON_AFTER,
    )
    assert _match(session, 2378001).extra["hltv"]["complete"] is False


def test_results_sync_revisits_matches_that_are_not_final(session: Session, app_settings: Settings) -> None:
    no_demo = _without_demos(load_fixture("match_bo3_finished.html"))
    fetcher = FakeFetcher({r"^/matches/2378001(/|$)": no_demo})
    sync_match(session, fetcher, 2378001, settings=app_settings, now=SOON_AFTER)
    assert _match(session, 2378001).extra["hltv"]["complete"] is False
    fetcher = FakeFetcher()  # demo published since
    sync_results(
        session, fetcher, settings=app_settings, since=datetime(2026, 11, 20, tzinfo=UTC), max_pages=1
    )
    assert fetcher.requested(r"^/matches/2378001")
    m = _match(session, 2378001)
    assert m.extra["hltv"]["complete"] is True
    assert len(list(session.scalars(select(Demo).where(Demo.match_id == m.id)))) == 3


def _fetcher(settings: HLTVSettings, clock: FakeClock, pages: list[str]) -> tuple[HLTVFetcher, list[str]]:
    calls: list[str] = []

    def loader(url: str) -> LoadedPage:
        calls.append(url)
        return LoadedPage(pages.pop(0) if len(pages) > 1 else pages[0], 200)

    fetcher = HLTVFetcher(
        settings,
        page_loader=loader,
        sleep=clock.sleep,
        clock=clock.monotonic,
        now=clock.now,
        options=FetchOptions(),
        rng=random.Random(1),
    )
    return fetcher, calls


def test_match_over_page_without_demo_keeps_the_short_ttl(hltv_settings: HLTVSettings) -> None:
    clock = FakeClock(SOON_AFTER)
    no_demo = _without_demos(load_fixture("match_bo3_finished.html"))
    fetcher, calls = _fetcher(hltv_settings, clock, [no_demo])
    url = "https://www.hltv.org/matches/2378001/x"
    short, long_ = hltv_settings.cache_ttl_minutes, hltv_settings.finished_match_cache_ttl_minutes
    assert fetcher.ttl_minutes(url, no_demo, SOON_AFTER) == short
    assert fetcher.ttl_minutes(url, load_fixture("match_bo3_finished.html"), SOON_AFTER) == long_
    assert fetcher.ttl_minutes(url, no_demo, MATCH_START + timedelta(hours=49)) == long_  # settled
    deleted = load_fixture("match_forfeit.html")
    assert "Match deleted" in deleted
    assert fetcher.ttl_minutes(url, deleted, SOON_AFTER) == long_

    fetcher.get("/matches/2378001/x")
    clock.advance(short * 60 + 60)
    fetcher.get("/matches/2378001/x")  # re-fetched: the demo link may have appeared
    assert len(calls) == 2


# ------------------------------------------------------------------------- current rank from old pages


def test_old_match_page_rank_does_not_become_the_current_rank(
    session: Session, app_settings: Settings
) -> None:
    html = load_fixture("match_bo3_finished.html").replace(
        "/ranking/teams/2026/november/16/", "/ranking/teams/2024/march/4/"
    )
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    sync_match(
        session, FakeFetcher({r"^/matches/2378001(/|$)": html}), 2378001, settings=app_settings, now=now
    )
    vit = session.scalar(select(Team).where(Team.hltv_id == 9565))
    assert vit is not None
    assert vit.hltv_rank is None and vit.hltv_rank_updated_at is None  # not today's rank
    history = session.scalar(
        select(TeamRanking).where(TeamRanking.team_id == vit.id, TeamRanking.ranked_on == date(2024, 3, 4))
    )
    assert history is not None and history.rank == 1  # the dated history row is kept


def test_explicit_old_ranking_does_not_set_current_ranks(session: Session) -> None:
    unranked = resolve_team(session, name="Vitality", source=DataSource.HLTV, external_id=9565)
    assert unranked is not None and unranked.hltv_rank is None
    old = load_fixture("ranking_2026_11_16.html").replace("November 16th, 2026", "March 4th, 2024")
    report = sync_rankings(session, FakeFetcher({r"^/ranking/teams": old}), date(2024, 3, 4))
    assert report.ok
    assert unranked.hltv_rank is None
    assert session.scalar(select(TeamRanking).where(TeamRanking.team_id == unranked.id)) is not None
    sync_rankings(session, FakeFetcher())  # the latest ranking always sets the current rank
    assert unranked.hltv_rank == 1


# ------------------------------------------------------------------------- status, cache, cool-down


def test_live_match_is_not_moved_back_to_scheduled(session: Session, app_settings: Settings) -> None:
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    sync_match(session, FakeFetcher(), 2378010, settings=app_settings, now=now)
    m = _match(session, 2378010)
    assert m.status is MatchStatus.SCHEDULED
    m.status = MatchStatus.LIVE  # e.g. PandaScore saw the kick-off
    sync_match(session, FakeFetcher(), 2378010, settings=app_settings, now=now)
    assert m.status is MatchStatus.LIVE


def test_unparsable_cached_mapstats_page_is_dropped(session: Session, app_settings: Settings) -> None:
    clock = FakeClock()
    good = load_fixture("mapstats_210001.html")
    fetcher, calls = _fetcher(
        app_settings.hltv, clock, ["<html><body>Something went wrong</body></html>", good]
    )
    sync_match(session, FakeFetcher(), 2378001, settings=app_settings, with_mapstats=False)
    first = sync_mapstats(session, fetcher, 210001, settings=app_settings)
    assert first.failed == 1
    second = sync_mapstats(session, fetcher, 210001, settings=app_settings)
    assert len(calls) == 2  # the broken copy was not served from the month-long cache
    assert second.ok and second.created == 1


def test_cooldown_counter_cannot_overflow(hltv_settings: HLTVSettings) -> None:
    clock = FakeClock()
    fetcher, _ = _fetcher(hltv_settings, clock, [load_fixture("challenge_just_a_moment.html")])
    fetcher._consecutive_blocks = 5000  # e.g. a corrupt cool-down file
    with pytest.raises(HLTVBlockedError):  # never OverflowError
        fetcher.get("/matches/1/x")
    assert fetcher.blocked_until is not None
    assert fetcher.blocked_until - clock.now() <= timedelta(seconds=hltv_settings.cooldown_max_s)


# ------------------------------------------------------------------------- results rows without a time


def test_date_only_result_row_inside_the_since_day_is_still_synced(
    session: Session, app_settings: Settings
) -> None:
    html = load_fixture("results_offset0.html").replace('data-zonedgrouping-entry-unix="1795125000000"', "")
    rows = {r.hltv_match_id: r for r in parse_results_page(html)}
    assert rows[2378040].played_at_is_date_only and not rows[2378001].played_at_is_date_only
    fetcher = FakeFetcher({r"^/results$": html})
    since = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)  # the BO5 started at 17:50 that day
    sync_results(session, fetcher, settings=app_settings, since=since, max_pages=1)
    assert fetcher.requested(r"^/matches/2378040")
    assert session.scalar(select(Match).where(Match.hltv_id == 2378040)) is not None


def test_corrupt_or_unwritable_cooldown_file_never_breaks_the_fetcher(
    hltv_settings: HLTVSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cs2bot.ingest.hltv import fetcher as fetcher_module

    hltv_settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (hltv_settings.cache_dir / fetcher_module.COOLDOWN_FILE).write_text("[1, 2]", encoding="utf-8")
    clock = FakeClock()
    fetcher, _ = _fetcher(hltv_settings, clock, [load_fixture("challenge_just_a_moment.html")])
    assert fetcher.blocked_until is None

    def disk_full(path: object, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fetcher_module, "_atomic_write", disk_full)
    with pytest.raises(HLTVBlockedError):  # the block is still reported, not an OSError
        fetcher.get("/matches/1/x")
    assert fetcher.blocked_until is not None
