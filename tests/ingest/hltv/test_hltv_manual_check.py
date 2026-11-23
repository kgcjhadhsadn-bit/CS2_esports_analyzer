"""open_browser_for_manual_check with a fake Playwright (the real one needs a visible display)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cs2bot.config import HLTVSettings
from cs2bot.ingest.hltv import open_browser_for_manual_check
from cs2bot.ingest.hltv.fetcher import COOLDOWN_FILE
from tests.ingest.hltv.conftest import FakeClock, load_fixture

CHALLENGE_HTML = load_fixture("challenge_just_a_moment.html")
MATCH_HTML = load_fixture("match_bo3_finished.html")


class FakePage:
    """Serves a scripted sequence of page contents; ``closed_after`` polls the owner closes it."""

    def __init__(self, contents: list[str], clock: FakeClock, *, closed_after: int | None = None) -> None:
        self.contents = contents
        self.clock = clock
        self.closed_after = closed_after
        self.polls = 0
        self.goto_urls: list[str] = []
        self.clicks = 0

    def goto(self, url: str, **_: Any) -> None:
        self.goto_urls.append(url)

    def is_closed(self) -> bool:
        return self.closed_after is not None and self.polls >= self.closed_after

    def content(self) -> str:
        index = min(self.polls, len(self.contents) - 1)
        return self.contents[index]

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.polls += 1
        self.clock.advance(timeout_ms / 1000.0)

    def click(self, *_: Any, **__: Any) -> None:  # pragma: no cover - must never be called
        self.clicks += 1


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False
        self.timeout: float | None = None

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self.timeout = timeout

    def new_page(self) -> FakePage:  # pragma: no cover - a page always exists
        return self.pages[0]

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, page: FakePage) -> None:
        self.context = FakeContext(page)
        self.launches: list[tuple[str, dict[str, Any]]] = []
        self.stopped = False
        self.chromium = self

    def launch_persistent_context(self, profile: str, **kwargs: Any) -> FakeContext:
        self.launches.append((profile, kwargs))
        return self.context

    def stop(self) -> None:
        self.stopped = True


def _settings(tmp_path: Path, **updates: Any) -> HLTVSettings:
    values: dict[str, Any] = {
        "cache_dir": tmp_path / "cache",
        "browser_profile_dir": tmp_path / "profile",
        "chromium_executable": "/opt/pw-browsers/chromium",
        "headless": True,  # the manual check is visible regardless
    }
    values.update(updates)
    return HLTVSettings(**values)


def _write_cooldown(settings: HLTVSettings) -> Path:
    path = Path(settings.cache_dir) / COOLDOWN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"blocked_until": "2099-01-01T00:00:00+00:00", "consecutive_blocks": 3}))
    return path


def test_opens_visible_window_with_profile_and_lifts_cooldown_once_cleared(
    tmp_path: Path, clock: FakeClock
) -> None:
    settings = _settings(tmp_path)
    cooldown = _write_cooldown(settings)
    page = FakePage([CHALLENGE_HTML, CHALLENGE_HTML, MATCH_HTML], clock, closed_after=4)
    pw = FakePlaywright(page)
    result = open_browser_for_manual_check(
        settings, url="/matches/2378001/x", wait_s=60.0, start_playwright=lambda: pw, clock=clock.monotonic
    )
    profile, kwargs = pw.launches[0]
    assert profile == str(tmp_path / "profile") and (tmp_path / "profile").is_dir()
    assert kwargs["headless"] is False and kwargs["executable_path"] == "/opt/pw-browsers/chromium"
    assert page.goto_urls == ["https://www.hltv.org/matches/2378001/x"] and page.clicks == 0
    assert result.window_closed and result.cleared and result.last_challenge is None
    assert result.cooldown_cleared and not cooldown.exists()
    assert pw.context.closed and pw.stopped


def test_challenge_still_shown_keeps_the_cooldown(tmp_path: Path, clock: FakeClock) -> None:
    settings = _settings(tmp_path)
    cooldown = _write_cooldown(settings)
    page = FakePage([CHALLENGE_HTML], clock)  # never closed: the wait runs out
    pw = FakePlaywright(page)
    result = open_browser_for_manual_check(
        settings, wait_s=5.0, poll_s=1.0, start_playwright=lambda: pw, clock=clock.monotonic
    )
    assert not result.window_closed and not result.cleared and result.last_challenge is not None
    assert not result.cooldown_cleared and cooldown.exists()
    assert 5.0 <= result.waited_s <= 6.0
    assert page.goto_urls == ["https://www.hltv.org/"]
    assert pw.context.closed and pw.stopped


def test_proxy_credentials_go_to_playwright_not_the_url(tmp_path: Path, clock: FakeClock) -> None:
    settings = _settings(tmp_path, proxy_url="http://user:s3cret@proxy.local:8080")
    pw = FakePlaywright(FakePage([MATCH_HTML], clock, closed_after=1))
    open_browser_for_manual_check(settings, wait_s=5.0, start_playwright=lambda: pw, clock=clock.monotonic)
    proxy = pw.launches[0][1]["proxy"]
    assert proxy == {"server": "http://proxy.local:8080", "username": "user", "password": "s3cret"}


def test_refuses_without_profile_or_for_other_hosts(tmp_path: Path, clock: FakeClock) -> None:
    def never() -> Any:  # pragma: no cover - must not start a browser
        raise AssertionError("browser started")

    with pytest.raises(ValueError, match="browser_profile_dir"):
        open_browser_for_manual_check(_settings(tmp_path, browser_profile_dir=None), start_playwright=never)
    with pytest.raises(ValueError, match="non-HLTV"):
        open_browser_for_manual_check(
            _settings(tmp_path), url="https://evil.example.com/", start_playwright=never
        )
    with pytest.raises(ValueError, match="wait_s"):
        open_browser_for_manual_check(_settings(tmp_path), wait_s=0, start_playwright=never)


def test_browser_is_closed_when_launch_fails(tmp_path: Path, clock: FakeClock) -> None:
    pw = FakePlaywright(FakePage([MATCH_HTML], clock))

    def broken_launch(profile: str, **kwargs: Any) -> FakeContext:
        raise RuntimeError("profile locked by another browser")

    pw.launch_persistent_context = broken_launch  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="profile locked"):
        open_browser_for_manual_check(
            _settings(tmp_path), wait_s=5.0, start_playwright=lambda: pw, clock=clock.monotonic
        )
    assert pw.stopped


def test_accepts_the_full_settings_object(tmp_path: Path, clock: FakeClock, settings: Any) -> None:
    full = settings.model_copy(update={"hltv": _settings(tmp_path)})
    pw = FakePlaywright(FakePage([MATCH_HTML], clock, closed_after=1))
    result = open_browser_for_manual_check(
        full, wait_s=5.0, start_playwright=lambda: pw, clock=clock.monotonic
    )
    assert result.cleared and result.profile_dir == tmp_path / "profile"
