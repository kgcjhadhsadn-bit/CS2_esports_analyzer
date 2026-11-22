"""ThreadBoundExecutor: every call runs on one dedicated worker thread (Playwright sync API rule)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from cs2bot.domain.betting import BetOrder, PlacementResult, PlacementStatus
from cs2bot.executor import ExecutorClosedError, FakeExecutor, ThreadBoundExecutor
from cs2bot.utils.time import utcnow


def _order(n: int) -> BetOrder:
    return BetOrder(
        bet_id=n,
        idempotency_key=f"k{n}",
        external_event_id="1",
        market_raw_name="Zwycięzca meczu",
        selection_raw_name="Vitality",
        requested_odds=Decimal("1.9"),
        min_acceptable_odds=Decimal("1.8"),
        stake=Decimal("2.00"),
        dry_run=False,
    )


def test_calls_run_on_one_worker_thread_created_lazily() -> None:
    threads: set[int] = set()
    created: list[FakeExecutor] = []

    def record(order: BetOrder) -> PlacementResult:
        threads.add(threading.get_ident())
        return PlacementResult(status=PlacementStatus.DRY_RUN_OK, at=utcnow())

    def factory() -> FakeExecutor:
        threads.add(threading.get_ident())
        fake = FakeExecutor([record] * 20)
        created.append(fake)
        return fake

    wrapper = ThreadBoundExecutor(factory)
    assert not wrapper.started and created == []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: wrapper.place(_order(n)), range(12)))
    assert all(r.status is PlacementStatus.DRY_RUN_OK for r in results)
    assert len(created) == 1 and wrapper.started
    assert len(threads) == 1 and threading.get_ident() not in threads
    assert wrapper.get_balance() == Decimal("1000.00")
    assert wrapper.is_logged_in() is True
    assert wrapper.health_check().ok is True
    assert wrapper.open_login_window(1.0) is True
    assert wrapper.fetch_bet_history(datetime(2020, 1, 1, tzinfo=UTC)) == []
    wrapper.close()
    assert created[0].closed
    with pytest.raises(ExecutorClosedError):
        wrapper.place(_order(99))
    wrapper.close()  # idempotent


def test_exceptions_propagate_to_the_caller() -> None:
    def boom(order: BetOrder) -> PlacementResult:
        raise RuntimeError("driver crashed")

    with (
        ThreadBoundExecutor(lambda: FakeExecutor([boom])) as wrapper,
        pytest.raises(RuntimeError, match="crashed"),
    ):
        wrapper.place(_order(1))


def test_close_without_start_does_not_create_the_executor() -> None:
    created: list[int] = []
    wrapper = ThreadBoundExecutor(lambda: created.append(1) or FakeExecutor())  # type: ignore[func-returns-value]
    wrapper.close()
    assert created == []


def test_create_betclic_executor_is_lazy_and_reads_the_kill_switch(
    session_factory: Any, tmp_path: Any
) -> None:
    from cs2bot.config import BetclicSettings, Settings
    from cs2bot.db import state
    from cs2bot.executor import create_betclic_executor

    settings = Settings(betclic=BetclicSettings(browser_profile_dir=tmp_path / "profile"))
    launched: list[int] = []

    def no_browser() -> Any:
        launched.append(1)
        raise AssertionError("the browser must not start while trading is halted")

    with session_factory() as s:
        state.set_kill_switch(s, True, actor="test", reason="test")
        s.commit()
    executor = create_betclic_executor(settings, session_factory, playwright_factory=no_browser)
    try:
        assert not executor.started
        result = executor.place(_order(5))
        assert result.status is PlacementStatus.KILL_SWITCH and launched == []
        assert executor.started
    finally:
        executor.close()
