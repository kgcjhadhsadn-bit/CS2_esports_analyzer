"""Fixtures for the HLTV package tests.

HTML fixtures under ``fixtures/`` mirror HLTV's real markup (see ``cs2bot.ingest.hltv.selectors`` for
the sources); they were generated to be internally consistent (round histories agree with half
scores and final scores). Nothing here touches the network.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from cs2bot.config import HLTVSettings, Settings
from cs2bot.ingest.hltv.errors import HLTVNotFoundError

FIXTURES = Path(__file__).parent / "fixtures"
CHROMIUM = "/opt/pw-browsers/chromium"

# Fixture URL routes (regex on path[?query]) -> fixture file.
DEFAULT_ROUTES: dict[str, str] = {
    r"^/matches/2378001(/|$)": "match_bo3_finished.html",
    r"^/matches/2378002(/|$)": "match_bo1_finished.html",
    r"^/matches/2378010(/|$)": "match_upcoming.html",
    r"^/matches/2378011(/|$)": "match_upcoming_veto.html",
    r"^/matches/2378020(/|$)": "match_postponed.html",
    r"^/matches/2378030(/|$)": "match_forfeit.html",
    r"^/matches/2378040(/|$)": "match_bo5_partial_forfeit.html",
    r"^/stats/matches/mapstatsid/210001(/|$)": "mapstats_210001.html",
    r"^/stats/matches/mapstatsid/210002(/|$)": "mapstats_210002.html",
    r"^/stats/matches/mapstatsid/210003(/|$)": "mapstats_210003.html",
    r"^/stats/matches/mapstatsid/210010(/|$)": "mapstats_210010.html",
    r"^/results$": "results_offset0.html",
    r"^/results\?offset=100$": "results_offset100.html",
    r"^/matches$": "matches_upcoming.html",
    r"^/ranking/teams": "ranking_2026_11_16.html",
    r"^/news/archive/2026/november$": "news_archive_2026_november.html",
    r"^/news/43100(/|$)": "news_article_43100.html",
}


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetcher:
    """In-memory :class:`cs2bot.ingest.hltv.sync.PageSource`: serves fixtures by URL pattern.

    A route value is a fixture file name, raw HTML (anything containing ``<``), an exception to raise,
    or a callable returning one of those. Unknown URLs raise :class:`HLTVNotFoundError`.
    """

    base_url = "https://www.hltv.org"

    def __init__(self, routes: dict[str, object] | None = None, *, use_defaults: bool = True) -> None:
        self.routes: dict[str, object] = dict(DEFAULT_ROUTES) if use_defaults else {}
        if routes:
            # explicit routes win over the defaults
            self.routes = {**dict(routes), **{k: v for k, v in self.routes.items() if k not in routes}}
        self.calls: list[tuple[str, int | None]] = []

    @staticmethod
    def _path(path_or_url: str) -> str:
        parts = urlsplit(path_or_url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def get(self, path_or_url: str, *, max_age_minutes: int | None = None) -> str:
        path = self._path(path_or_url)
        self.calls.append((path, max_age_minutes))
        for pattern, value in self.routes.items():
            if re.search(pattern, path):
                resolved = value() if callable(value) else value
                if isinstance(resolved, BaseException):
                    raise resolved
                text = str(resolved)
                return text if "<" in text else load_fixture(text)
        raise HLTVNotFoundError(f"no fixture for {path}", url=path, status=404)

    def requested(self, pattern: str) -> list[str]:
        return [p for p, _ in self.calls if re.search(pattern, p)]


@pytest.fixture
def fixture_html() -> Callable[[str], str]:
    return load_fixture


@pytest.fixture
def fake_fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def hltv_settings(tmp_path: Path) -> HLTVSettings:
    return HLTVSettings(
        cache_dir=tmp_path / "hltv-cache",
        min_delay_s=4.0,
        max_delay_s=9.0,
        chromium_executable=CHROMIUM,
        headless=True,
        navigation_timeout_s=20.0,
    )


@pytest.fixture
def app_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Full settings with the HLTV cache inside the test's temp dir."""
    return settings.model_copy(
        update={"hltv": settings.hltv.model_copy(update={"cache_dir": tmp_path / "c"})}
    )


class FakeClock:
    """Monotonic + wall clock that only moves when told to (or when ``sleep`` is called)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.wall = start or datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
        self.mono = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
