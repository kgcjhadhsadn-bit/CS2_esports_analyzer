"""Launch options, browser-session lifecycle and pre-browser executor guards, with a fake Playwright."""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from pydantic import SecretStr

from cs2bot.config import BetclicSettings, ExecutorSettings, Settings
from cs2bot.domain.betting import BetOrder, PlacementStatus
from cs2bot.executor import (
    BetclicExecutor,
    BrowserStartError,
    ExecutorClosedError,
    ExecutorThreadError,
    ExecutorTuning,
    ProfileLock,
)
from cs2bot.executor.session import (
    BROWSER_LOCALE,
    BROWSER_TIMEZONE,
    BrowserSession,
    build_launch_options,
    parse_proxy_url,
)

TUNING = ExecutorTuning(lock_stale_after_s=60, lock_heartbeat_s=5)


class FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.url = "about:blank"
        self.gotos: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    def set_default_navigation_timeout(self, ms: float) -> None:
        self.nav_timeout = ms

    def set_default_timeout(self, ms: float) -> None:
        self.timeout = ms

    def goto(self, url: str, **kwargs: Any) -> None:
        self.gotos.append(url)
        raise PlaywrightError("net::ERR_INTERNET_DISCONNECTED (fake)")

    def screenshot(self, path: str, **kwargs: Any) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def content(self) -> str:
        return "<html></html>"


class FakeContext:
    def __init__(self) -> None:
        self.pages = [FakePage()]
        self.handlers: dict[str, Any] = {}
        self.closed = False

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def set_default_navigation_timeout(self, ms: float) -> None:
        self.nav_timeout = ms

    def set_default_timeout(self, ms: float) -> None:
        self.timeout = ms

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True
        if "close" in self.handlers:
            self.handlers["close"](self)

    def owner_closes_window(self) -> None:
        self.close()


class FakeChromium:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.launches: list[dict[str, Any]] = []
        self.contexts: list[FakeContext] = []

    def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
        self.launches.append(kwargs)
        if self.fail:
            raise RuntimeError("Executable doesn't exist at /nowhere/chrome")
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx


class FakePlaywright:
    def __init__(self, fail: bool = False) -> None:
        self.chromium = FakeChromium(fail)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class Factory:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.created: list[FakePlaywright] = []

    def __call__(self) -> Any:
        pw = FakePlaywright(self.fail)
        self.created.append(pw)
        return pw

    @property
    def launches(self) -> list[dict[str, Any]]:
        return [kw for pw in self.created for kw in pw.chromium.launches]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        betclic=BetclicSettings(browser_profile_dir=tmp_path / "profile"),
        executor=ExecutorSettings(
            screenshot_dir=tmp_path / "shots", action_delay_min_s=0, action_delay_max_s=0
        ),
    )


def _order(**overrides: Any) -> BetOrder:
    data: dict[str, Any] = {
        "bet_id": 1,
        "idempotency_key": "k1",
        "external_event_id": "3002201001",
        "event_url": "/e-sport-s102/x/a-b-m3002201001",
        "market_raw_name": "Zwycięzca meczu",
        "selection_raw_name": "Natus Vincere",
        "requested_odds": Decimal("1.85"),
        "min_acceptable_odds": Decimal("1.80"),
        "stake": Decimal("10.00"),
        "dry_run": False,
    }
    data.update(overrides)
    return BetOrder(**data)


# ----------------------------------------------------------------------------- launch options


def test_build_launch_options_defaults(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":99")  # a desktop / Xvfb display (see the headless fallback test)
    opts = build_launch_options(settings, TUNING, tmp_path / "profile")
    assert opts["user_data_dir"] == str(tmp_path / "profile")
    assert opts["headless"] is False  # visible by default (owner's desktop)
    assert opts["locale"] == BROWSER_LOCALE == "pl-PL"
    assert opts["timezone_id"] == BROWSER_TIMEZONE == "Europe/Warsaw"
    assert opts["viewport"] == {"width": 1440, "height": 900}
    assert "executable_path" not in opts and "proxy" not in opts


def test_build_launch_options_with_executable_proxy_and_headless(tmp_path: Path) -> None:
    s = Settings(
        betclic=BetclicSettings(
            chromium_executable="C:/Chromium/chrome.exe",
            proxy_url=SecretStr("http://us%40er:p%3Ass@10.0.0.1:3128"),
        ),
        executor=ExecutorSettings(headless=True),
    )
    opts = build_launch_options(s, ExecutorTuning(viewport_width=1280, viewport_height=800), tmp_path)
    assert opts["executable_path"] == "C:/Chromium/chrome.exe"
    assert opts["headless"] is True
    assert opts["viewport"] == {"width": 1280, "height": 800}
    assert opts["proxy"] == {"server": "http://10.0.0.1:3128", "username": "us@er", "password": "p:ss"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("socks5://proxy.local:1080", {"server": "socks5://proxy.local:1080"}),
        ("http://user:pw@host", {"server": "http://host", "username": "user", "password": "pw"}),
        ("http://[::1]:8080", {"server": "http://[::1]:8080"}),
    ],
)
def test_parse_proxy_url(url: str, expected: dict[str, str]) -> None:
    assert parse_proxy_url(url) == expected


@pytest.mark.parametrize("url", ["", "proxy.local:1080", "http://"])
def test_parse_proxy_url_rejects_garbage_without_echoing_it(url: str) -> None:
    with pytest.raises(ValueError) as err:
        parse_proxy_url(url)
    assert url == "" or url not in str(err.value)


# ----------------------------------------------------------------------------- browser session


def test_session_launches_once_and_reuses_the_page(settings: Settings) -> None:
    factory = Factory()
    session = BrowserSession(settings, tuning=TUNING, playwright_factory=factory)
    page = session.ensure_page()
    assert session.ensure_page() is page
    assert len(factory.launches) == 1 and session.is_running and session.lock.held
    launch = factory.launches[0]
    assert launch["user_data_dir"] == str(session.profile_dir)
    ctx = factory.created[0].chromium.contexts[0]
    assert ctx.nav_timeout == settings.executor.navigation_timeout_s * 1000
    session.close()
    assert not session.lock.held and factory.created[0].stopped and ctx.closed
    session.close()  # idempotent


def test_session_reopens_tab_and_relaunches_after_owner_closes_window(settings: Settings) -> None:
    factory = Factory()
    session = BrowserSession(settings, tuning=TUNING, playwright_factory=factory)
    first = session.ensure_page()
    first.closed = True  # type: ignore[attr-defined]  # owner closed the tab
    second = session.ensure_page()
    assert second is not first and len(factory.launches) == 1
    factory.created[0].chromium.contexts[0].owner_closes_window()
    assert session.current_page() is None and not session.is_running
    third = session.ensure_page()
    assert third is not second and len(factory.launches) == 2
    assert session.lock.held
    session.close()


def test_session_failed_launch_releases_lock(settings: Settings) -> None:
    factory = Factory(fail=True)
    session = BrowserSession(settings, tuning=TUNING, playwright_factory=factory)
    with pytest.raises(BrowserStartError, match="Executable doesn't exist"):
        session.ensure_page()
    assert not session.lock.held and not session.lock.path.exists()
    assert factory.created[0].stopped


def test_session_is_bound_to_its_thread(settings: Settings) -> None:
    session = BrowserSession(settings, tuning=TUNING, playwright_factory=Factory())
    session.ensure_page()
    errors: list[BaseException] = []

    def other_thread() -> None:
        try:
            session.ensure_page()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    assert len(errors) == 1 and isinstance(errors[0], ExecutorThreadError)
    session.close()


# ----------------------------------------------------------------------------- executor guards


def test_halted_before_start_never_launches_the_browser(settings: Settings) -> None:
    factory = Factory()
    executor = BetclicExecutor(
        settings,
        halt_check=lambda: (True, "kill switch is on"),
        playwright_factory=factory,
        tuning=TUNING,
        sleep=lambda _s: None,
    )
    result = executor.place(_order())
    assert result.status is PlacementStatus.KILL_SWITCH
    assert "kill switch is on" in result.message
    assert factory.created == [] and result.screenshot_path is None
    executor.close()


def test_halt_check_errors_count_as_halted(settings: Settings) -> None:
    def broken() -> tuple[bool, str | None]:
        raise RuntimeError("db down")

    factory = Factory()
    executor = BetclicExecutor(settings, halt_check=broken, playwright_factory=factory, tuning=TUNING)
    result = executor.place(_order())
    assert result.status is PlacementStatus.KILL_SWITCH and factory.created == []
    executor.close()


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"stake": Decimal("0")}, "positive"),
        ({"stake": Decimal("10.005")}, "decimal places"),
        ({"stake": Decimal("1.50")}, "below the Betclic minimum"),
        ({"min_acceptable_odds": Decimal("1.00")}, "min_acceptable_odds"),
        ({"selection_raw_name": "  "}, "required"),
        ({"event_url": None, "external_event_id": ""}, "neither"),
        ({"event_url": None, "external_event_id": "1'] a"}, "unsupported characters"),
    ],
)
def test_invalid_orders_fail_without_a_browser(
    settings: Settings, overrides: dict[str, Any], match: str
) -> None:
    factory = Factory()
    executor = BetclicExecutor(
        settings, halt_check=lambda: (False, None), playwright_factory=factory, tuning=TUNING
    )
    result = executor.place(_order(**overrides))
    assert result.status is PlacementStatus.FAILED
    assert match in result.message
    assert factory.created == []
    executor.close()


def test_disabled_executor_refuses(settings: Settings) -> None:
    s = settings.model_copy(update={"executor": settings.executor.model_copy(update={"enabled": False})})
    factory = Factory()
    executor = BetclicExecutor(s, halt_check=lambda: (False, None), playwright_factory=factory, tuning=TUNING)
    result = executor.place(_order())
    assert result.status is PlacementStatus.FAILED and "disabled" in result.message
    assert factory.created == []


def test_browser_errors_before_the_click_are_failed_and_screenshotted(settings: Settings) -> None:
    factory = Factory()
    executor = BetclicExecutor(
        settings,
        halt_check=lambda: (False, None),
        playwright_factory=factory,
        tuning=TUNING,
        sleep=lambda _s: None,
    )
    result = executor.place(_order())
    assert result.status is PlacementStatus.FAILED
    assert "before the place click" in result.message and "ERR_INTERNET_DISCONNECTED" in result.message
    assert result.screenshot_path is not None and Path(result.screenshot_path).exists()
    assert "_bet1_k1_failed" in Path(result.screenshot_path).name
    assert executor.get_balance() is None  # same fake error, reported as "unknown balance"
    executor.close()


def test_profile_in_use_by_another_process_fails_cleanly(settings: Settings) -> None:
    holder = ProfileLock(Path(settings.betclic.browser_profile_dir), heartbeat_s=None)
    holder.acquire()
    factory = Factory()
    executor = BetclicExecutor(
        settings, halt_check=lambda: (False, None), playwright_factory=factory, tuning=TUNING
    )
    result = executor.place(_order())
    assert result.status is PlacementStatus.FAILED and "in use" in result.message
    assert factory.created == []
    health = executor.health_check()
    assert health.ok is False and "in use" in health.message
    executor.close()
    holder.release()


def test_minimum_gap_between_bets(settings: Settings) -> None:
    slept: list[float] = []
    halted = {"on": False}
    executor = BetclicExecutor(
        settings,
        halt_check=lambda: (halted["on"], "paused"),
        playwright_factory=Factory(),
        tuning=ExecutorTuning(min_seconds_between_bets=30, lock_stale_after_s=60, lock_heartbeat_s=5),
        sleep=slept.append,
    )
    assert executor.place(_order(idempotency_key="a")).status is PlacementStatus.FAILED  # fake goto error
    assert slept == []
    executor.place(_order(idempotency_key="b"))
    assert len(slept) == 1 and 29 <= slept[0] <= 30
    halted["on"] = True  # a halted bot answers at once: no pacing sleep for a bet it will not place
    assert executor.place(_order(idempotency_key="c")).status is PlacementStatus.KILL_SWITCH
    assert len(slept) == 1
    executor.close()


def test_closed_executor_raises(settings: Settings) -> None:
    executor = BetclicExecutor(
        settings, halt_check=lambda: (False, None), playwright_factory=Factory(), tuning=TUNING
    )
    executor.close()
    executor.close()
    with pytest.raises(ExecutorClosedError):
        executor.place(_order())
    with pytest.raises(ExecutorClosedError):
        executor.get_balance()
