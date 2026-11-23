"""Fixtures for the executor tests: the local mock Betclic site, settings, executor and order factories.

Browser tests launch the Chromium at ``PLAYWRIGHT_CHROMIUM_EXECUTABLE`` (default /opt/pw-browsers/chromium)
headless, with a temporary profile, against the mock site on 127.0.0.1 - never the internet.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import sync_playwright

from cs2bot.config import BetclicSettings, ExecutorSettings, Settings
from cs2bot.domain.betting import BetOrder
from cs2bot.executor.betclic import BetclicExecutor
from cs2bot.executor.selectors import SelectorSet, load_selectors
from cs2bot.executor.tuning import ExecutorTuning
from tests.executor.mock_betclic import EVENT_ID, MockBetclicServer

CHROMIUM = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/opt/pw-browsers/chromium")

requires_chromium = pytest.mark.skipif(
    not Path(CHROMIUM).exists(), reason=f"Chromium not found at {CHROMIUM}"
)

FAST_TUNING = ExecutorTuning(
    action_timeout_s=5.0,
    element_timeout_s=3.0,
    optional_timeout_s=0.4,
    login_check_timeout_s=3.0,
    login_logged_out_stable_s=0.4,
    login_poll_s=0.2,
    slip_update_timeout_s=3.0,
    confirmation_timeout_s=3.0,
    balance_update_wait_s=0.75,  # also the post-click "is the rejection real?" watch window
    poll_interval_s=0.05,
    slip_settle_s=0.1,
    min_seconds_between_bets=0.0,
    typing_delay_min_ms=0,
    typing_delay_max_ms=0,
    lock_stale_after_s=60.0,
    lock_heartbeat_s=5.0,
)

_keys = itertools.count(1)


@pytest.fixture(scope="session")
def mock_server() -> Iterator[MockBetclicServer]:
    server = MockBetclicServer().start()
    yield server
    server.stop()


@pytest.fixture
def site(mock_server: MockBetclicServer) -> MockBetclicServer:
    """The mock site with fresh default state for this test."""
    mock_server.reset()
    return mock_server


@pytest.fixture
def executor_settings(tmp_path: Path, site: MockBetclicServer) -> Settings:
    return Settings(
        betclic=BetclicSettings(
            base_url=site.base_url,
            cs2_listing_path="/e-sport-s102",
            browser_profile_dir=tmp_path / "profile",
            chromium_executable=CHROMIUM,
        ),
        executor=ExecutorSettings(
            headless=True,
            dry_run=False,
            screenshot_dir=tmp_path / "screenshots",
            action_delay_min_s=0.0,
            action_delay_max_s=0.0,
            navigation_timeout_s=15.0,
        ),
    )


@pytest.fixture
def selectors() -> SelectorSet:
    """The production selector file (config/betclic_selectors.yaml)."""
    return load_selectors(settings=Settings())


ExecutorFactory = Callable[..., BetclicExecutor]


class _SharedBrowserChromium:
    """``playwright.chromium`` stand-in that opens a fresh CONTEXT in one shared browser.

    Launching Chromium with a persistent profile costs well over half a second per test and there are
    more than a hundred browser tests. The executor only uses the ``BrowserContext`` API of what
    ``launch_persistent_context`` returns, so for most tests a new (isolated: own cookies and storage)
    context of one long-lived browser is equivalent. The profile directory is still created - the lock
    file and the placement journal live there. Tests that are about the real persistent launch pass
    ``make_executor(real_profile=True)``.
    """

    _CONTEXT_OPTIONS = ("locale", "timezone_id", "viewport", "accept_downloads", "proxy")

    def __init__(self, playwright: Any) -> None:
        self._playwright = playwright
        self._browser: Any = None
        self.launch_options: list[dict[str, Any]] = []

    def launch_persistent_context(self, **options: Any) -> Any:
        self.launch_options.append(options)
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._playwright.chromium.launch(
                headless=options.get("headless", True), executable_path=options.get("executable_path")
            )
        Path(options["user_data_dir"]).mkdir(parents=True, exist_ok=True)
        return self._browser.new_context(**{k: options[k] for k in self._CONTEXT_OPTIONS if k in options})

    def close(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            self._browser.close()
        self._browser = None


class _SharedPlaywright:
    """One Playwright driver for all executors of a test module (starting it costs ~0.3 s each time).

    ``BrowserSession.close()`` stops "its" Playwright; for the shared driver that is a no-op - the
    module fixture stops the real one. ``chromium`` is the real launcher (one Chromium with a persistent
    profile per executor) or, by default, :class:`_SharedBrowserChromium`.
    """

    def __init__(self, playwright: Any, chromium: Any = None) -> None:
        self._playwright = playwright
        self._chromium = chromium

    @property
    def chromium(self) -> Any:
        return self._chromium if self._chromium is not None else self._playwright.chromium

    def stop(self) -> None:
        return


class PlaywrightFactories:
    """The two ``playwright_factory`` callables of a test module (same driver underneath)."""

    def __init__(self) -> None:
        self._started: list[Any] = []
        self._shared_chromium: _SharedBrowserChromium | None = None

    def _driver(self) -> Any:
        if not self._started:
            self._started.append(sync_playwright().start())
        return self._started[0]

    def shared_browser(self) -> Any:
        """Fast: every executor gets a new context of one shared browser."""
        driver = self._driver()
        if self._shared_chromium is None:
            self._shared_chromium = _SharedBrowserChromium(driver)
        return _SharedPlaywright(driver, self._shared_chromium)

    def real_profile(self) -> Any:
        """The production path: ``chromium.launch_persistent_context`` with the profile directory."""
        return _SharedPlaywright(self._driver())

    def stop(self) -> None:
        if self._shared_chromium is not None:
            self._shared_chromium.close()
        for playwright in self._started:
            playwright.stop()
        self._started.clear()


@pytest.fixture(scope="module")
def playwright_factories() -> Iterator[PlaywrightFactories]:
    """Module-scoped on purpose: Playwright's sync API allows one driver per thread, so it must be gone
    before another package's browser tests start their own in the same pytest process."""
    factories = PlaywrightFactories()
    yield factories
    factories.stop()


@pytest.fixture
def make_executor(
    executor_settings: Settings, playwright_factories: PlaywrightFactories
) -> Iterator[ExecutorFactory]:
    """Executors against the mock site; ``real_profile=True`` launches Chromium with the profile dir."""
    created: list[BetclicExecutor] = []

    def _make(**kwargs: Any) -> BetclicExecutor:
        settings = kwargs.pop("settings", executor_settings)
        real_profile = kwargs.pop("real_profile", False)
        kwargs.setdefault("halt_check", lambda: (False, None))
        kwargs.setdefault("sleep", lambda _s: None)
        kwargs.setdefault("tuning", FAST_TUNING)
        kwargs.setdefault(
            "playwright_factory",
            playwright_factories.real_profile if real_profile else playwright_factories.shared_browser,
        )
        executor = BetclicExecutor(settings, **kwargs)
        created.append(executor)
        return executor

    yield _make
    for executor in created:
        executor.close()


OrderFactory = Callable[..., BetOrder]


@pytest.fixture
def make_order(site: MockBetclicServer) -> OrderFactory:
    def _make(**overrides: Any) -> BetOrder:
        n = next(_keys)
        data: dict[str, Any] = {
            "bet_id": n,
            "idempotency_key": f"pred-{n}:match_winner#team1",
            "external_event_id": EVENT_ID,
            "event_url": site.event_path(),
            "market_raw_name": "Zwycięzca meczu",
            "selection_raw_name": "Natus Vincere",
            "requested_odds": Decimal("1.85"),
            "min_acceptable_odds": Decimal("1.80"),
            "stake": Decimal("12.50"),
            "dry_run": False,
        }
        data.update(overrides)
        return BetOrder(**data)

    return _make
