"""Unit regressions of review round 1, third pass (no browser): ``place`` / ``health_check`` never raise for
bad input or a failing profile lock, and an unexpected error after the final click stays UNCERTAIN."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cs2bot.config import BetclicSettings, ExecutorSettings, Settings
from cs2bot.domain.betting import PlacementStatus
from cs2bot.executor import BetclicExecutor, ExecutorTuning, ProfileLock, ProfileLockedError
from cs2bot.executor.betclic import _Abort, _Attempt
from cs2bot.executor.lock import LOCK_FILENAME
from tests.executor.test_executor_launch import Factory, _order

TUNING = ExecutorTuning(lock_stale_after_s=60, lock_heartbeat_s=5)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        betclic=BetclicSettings(browser_profile_dir=tmp_path / "profile"),
        executor=ExecutorSettings(
            dry_run=False, screenshot_dir=tmp_path / "shots", action_delay_min_s=0, action_delay_max_s=0
        ),
    )


def _executor(settings: Settings, factory: Factory | None = None) -> BetclicExecutor:
    return BetclicExecutor(
        settings,
        halt_check=lambda: (False, None),
        playwright_factory=factory or Factory(),
        tuning=TUNING,
        sleep=lambda _s: None,
    )


@pytest.mark.parametrize("stake", ["1E+30", "123456789012345678901234567.5"])
def test_absurd_stake_is_failed_not_an_exception(settings: Settings, stake: str) -> None:
    """``Decimal.quantize`` raises InvalidOperation for too many digits; ``place`` must answer FAILED."""
    factory = Factory()
    executor = _executor(settings, factory)
    result = executor.place(_order(stake=Decimal(stake)))
    assert result.status is PlacementStatus.FAILED
    assert "not a valid amount" in result.message
    assert factory.created == []  # no browser was started
    executor.close()


def _deny_lock_file(monkeypatch: pytest.MonkeyPatch, *, times: int | None = None) -> list[int]:
    """``os.open`` on the lock file raises PermissionError (``times`` times, or always)."""
    real_open = os.open
    denied: list[int] = []

    def fake_open(path: Any, flags: int, *args: Any) -> int:
        if str(path).endswith(LOCK_FILENAME) and (times is None or len(denied) < times):
            denied.append(1)
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", fake_open)
    return denied


def test_lock_file_error_is_a_failed_placement_and_an_unhealthy_check(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError of the profile lock (e.g. an unwritable profile directory) must not escape ``place``,
    ``health_check`` or ``get_balance``."""
    executor = _executor(settings)
    _deny_lock_file(monkeypatch)
    result = executor.place(_order())
    assert result.status is PlacementStatus.FAILED
    assert "PermissionError" in result.message
    health = executor.health_check()
    assert health.ok is False and "PermissionError" in health.message
    assert executor.get_balance() is None
    monkeypatch.undo()
    executor.close()


def test_lock_retries_a_transient_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: a lock file in "delete pending" state cannot be re-created for a moment."""
    monkeypatch.setattr("cs2bot.executor.lock.time.sleep", lambda _s: None)
    denied = _deny_lock_file(monkeypatch, times=1)
    lock = ProfileLock(tmp_path, stale_after_s=60, heartbeat_s=None)
    lock.acquire()
    assert lock.held and denied == [1]
    lock.release()


def test_lock_permission_error_on_an_existing_file_is_profile_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cs2bot.executor.lock.time.sleep", lambda _s: None)
    holder = ProfileLock(tmp_path, stale_after_s=60, heartbeat_s=None)
    holder.acquire()
    _deny_lock_file(monkeypatch)
    with pytest.raises(ProfileLockedError):
        ProfileLock(tmp_path, stale_after_s=60, heartbeat_s=None).acquire()
    monkeypatch.undo()
    holder.release()


def test_lock_permission_error_without_a_lock_file_is_re_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not "in use by another process": the directory is not writable - say so."""
    monkeypatch.setattr("cs2bot.executor.lock.time.sleep", lambda _s: None)
    _deny_lock_file(monkeypatch)
    with pytest.raises(PermissionError):
        ProfileLock(tmp_path, stale_after_s=60, heartbeat_s=None).acquire()


def test_unexpected_error_before_the_click_is_failed(settings: Settings) -> None:
    executor = _executor(settings)

    def boom(_order: Any) -> str | None:
        raise RuntimeError("bug in a guard")

    executor._validate_order = boom  # type: ignore[method-assign]
    result = executor.place(_order())
    assert result.status is PlacementStatus.FAILED
    assert "RuntimeError" in result.message and "nothing was clicked" in result.message
    executor.close()


def test_unexpected_error_after_the_click_is_uncertain_and_blocks_the_key(settings: Settings) -> None:
    """Even an error inside the error handling must not turn a clicked attempt into an exception or a
    "no bet" answer: UNCERTAIN, and the key is refused from then on."""
    executor = _executor(settings)

    def place_on_page(_page: Any, attempt: _Attempt) -> Any:
        attempt.place_clicked = True
        raise _Abort(PlacementStatus.UNCERTAIN, "no confirmation", clear_slip=False)

    def broken_handler(_attempt: _Attempt, _abort: _Abort) -> Any:
        raise RuntimeError("screenshot code exploded")

    executor._place_on_page = place_on_page  # type: ignore[method-assign]
    executor._handle_abort = broken_handler  # type: ignore[method-assign]
    order = _order()
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN
    assert "do NOT retry" in result.message
    again = executor.place(order)
    assert again.status is PlacementStatus.UNCERTAIN and "refusing to place twice" in again.message
    executor.close()
