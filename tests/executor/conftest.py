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
    balance_update_wait_s=1.5,
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


@pytest.fixture
def make_executor(executor_settings: Settings) -> Iterator[ExecutorFactory]:
    created: list[BetclicExecutor] = []

    def _make(**kwargs: Any) -> BetclicExecutor:
        settings = kwargs.pop("settings", executor_settings)
        kwargs.setdefault("halt_check", lambda: (False, None))
        kwargs.setdefault("sleep", lambda _s: None)
        kwargs.setdefault("tuning", FAST_TUNING)
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
