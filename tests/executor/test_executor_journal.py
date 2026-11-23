"""Placement journal (write-ahead idempotency), tuning from settings, lock re-acquisition, wrapper rules."""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import BetclicSettings, ExecutorSettings, Settings
from cs2bot.domain.betting import BetOrder, PlacementResult, PlacementStatus
from cs2bot.executor import (
    BetclicExecutor,
    ExecutorClosedError,
    ExecutorTuning,
    FakeExecutor,
    JournalError,
    PlacementJournal,
    ProfileLock,
    ProfileLockedError,
    SelectorConfigError,
    ThreadBoundExecutor,
    create_betclic_executor,
)
from cs2bot.executor.betclic import _url_mentions_id
from cs2bot.executor.journal import JOURNAL_FILENAME, JournalEntry
from cs2bot.executor.lock import LOCK_FILENAME, LockInfo
from cs2bot.executor.tuning import SETTINGS_FIELDS
from cs2bot.utils.time import utcnow
from tests.executor.test_executor_launch import Factory


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _order(key: str = "k1", **overrides: Any) -> BetOrder:
    data: dict[str, Any] = {
        "bet_id": 1,
        "idempotency_key": key,
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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        betclic=BetclicSettings(browser_profile_dir=tmp_path / "profile"),
        executor=ExecutorSettings(
            dry_run=False, screenshot_dir=tmp_path / "shots", action_delay_min_s=0, action_delay_max_s=0
        ),
    )


# --------------------------------------------------------------------------------- journal


def test_journal_click_blocks_until_a_definite_rejection(tmp_path: Path) -> None:
    journal = PlacementJournal(tmp_path / "p" / JOURNAL_FILENAME)
    assert journal.entries() == [] and journal.blocking_entry("a") is None  # no file yet
    journal.record_click("a", 7)
    blocked = journal.blocking_entry("a")
    assert blocked is not None and blocked.event == "click" and blocked.bet_id == "7"
    assert blocked.at.tzinfo is not None
    assert journal.blocking_entry("b") is None  # other keys are unaffected
    journal.record_result("a", 7, PlacementStatus.INSUFFICIENT_BALANCE)
    assert journal.blocking_entry("a") is None  # rejected by the bookmaker: may be retried
    journal.record_click("a", 7)
    journal.record_result("a", 7, PlacementStatus.PLACED)
    placed = journal.blocking_entry("a")
    assert placed is not None and placed.status == "placed"
    journal.record_click("c", 9)
    journal.record_result("c", 9, PlacementStatus.UNCERTAIN)
    assert journal.blocking_entry("c") is not None
    journal.record_click("d", 10)
    journal.record_result("d", 10, PlacementStatus.KILL_SWITCH)  # journaled, then halted: never clicked
    assert journal.blocking_entry("d") is None
    # a second journal object (a restarted process) reads the same file
    again = PlacementJournal(journal.path)
    assert again.blocking_entry("a") is not None and again.blocking_entry("d") is None
    for line in journal.path.read_text(encoding="utf-8").splitlines():
        assert set(json.loads(line)) <= {"event", "key", "bet_id", "at", "status"}  # nothing sensitive


def test_journal_skips_torn_lines_and_keeps_unicode_keys(tmp_path: Path) -> None:
    path = tmp_path / JOURNAL_FILENAME
    journal = PlacementJournal(path)
    journal.record_click("zakład-ż:1#team1", 1)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"event": "click", "key": "torn", "bet_\n')  # power loss in the middle of a write
        fh.write('{"event": "result", "key": "naive", "at": "2026-11-23T10:00:00"}\n')  # naive time
        fh.write("[1, 2, 3]\n\n")
    journal.record_click("after", 2)
    assert [e.key for e in journal.entries()] == ["zakład-ż:1#team1", "after"]
    assert journal.blocking_entry("zakład-ż:1#team1") is not None
    assert journal.blocking_entry("torn") is None  # a torn record never reached the click (fsync first)
    assert journal.compact() == 3 and len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_journal_retention_and_compaction(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
    journal = PlacementJournal(tmp_path / JOURNAL_FILENAME, retention_days=30, clock=clock)
    journal.record_click("old", 1)
    journal.record_result("old", 1, PlacementStatus.PLACED)
    clock.now += timedelta(days=29)
    assert journal.blocking_entry("old") is not None
    journal.record_click("new", 2)
    clock.now += timedelta(days=2)  # "old" is now 31 days old: the match is long over
    assert journal.blocking_entry("old") is None and journal.blocking_entry("new") is not None
    assert journal.compact() == 2
    assert [e.key for e in journal.entries()] == ["new"]
    assert journal.compact() == 0
    assert not journal.path.with_name(journal.path.name + ".tmp").exists()
    with pytest.raises(ValueError):
        PlacementJournal(tmp_path / "x", retention_days=0)


def test_journal_io_errors_fail_safe(tmp_path: Path) -> None:
    path = tmp_path / JOURNAL_FILENAME
    path.mkdir()  # a directory in place of the file: neither readable nor writable
    journal = PlacementJournal(path)
    with pytest.raises(JournalError, match="unreadable"):
        journal.blocking_entry("a")
    with pytest.raises(JournalError, match="could not write"):
        journal.record_click("a", 1)
    assert journal.compact() == 0  # best effort, never raises
    entry = JournalEntry(event="click", key="k", bet_id="1", at=utcnow())
    assert JournalEntry.from_json(entry.to_json()) == entry and entry.blocks


def test_blocked_key_in_the_journal_is_refused_without_a_browser(settings: Settings) -> None:
    profile = Path(settings.betclic.browser_profile_dir)
    PlacementJournal(profile / JOURNAL_FILENAME).record_click("k1", 1)  # an earlier run died at the click
    factory = Factory()
    executor = BetclicExecutor(settings, halt_check=lambda: (False, None), playwright_factory=factory)
    result = executor.place(_order("k1"))
    assert result.status is PlacementStatus.UNCERTAIN
    assert "refusing to place twice" in result.message and "journal" in result.message
    assert factory.created == []  # the page was never touched
    # a dry run never clicks the final button, so it is not refused (here: the fake browser has no site)
    dry = executor.place(_order("k1", dry_run=True))
    assert dry.status is PlacementStatus.FAILED and "before the place click" in dry.message
    executor.close()


def test_blank_idempotency_key_and_bad_requested_odds_are_rejected(settings: Settings) -> None:
    factory = Factory()
    executor = BetclicExecutor(settings, halt_check=lambda: (False, None), playwright_factory=factory)
    blank = executor.place(_order("  "))
    assert blank.status is PlacementStatus.FAILED and "idempotency_key" in blank.message
    odds = executor.place(_order(requested_odds=Decimal("1")))
    assert odds.status is PlacementStatus.FAILED and "requested_odds" in odds.message
    assert factory.created == []
    executor.close()


# ---------------------------------------------------------------------------------- tuning


def test_tuning_from_settings_copies_every_executor_knob() -> None:
    custom = ExecutorSettings(
        element_timeout_s=7.0,
        optional_timeout_s=0.5,
        slip_update_timeout_s=6.0,
        confirmation_timeout_s=30.0,
        min_seconds_between_bets=45.0,
        max_odds_change_rounds=1,
        viewport_width=1280,
        viewport_height=720,
        save_html_on_selector_error=False,
        health_check_event_page=False,
        lock_stale_after_s=300.0,
        lock_heartbeat_s=20.0,
    )
    tuning = ExecutorTuning.from_settings(Settings(executor=custom))
    for name in SETTINGS_FIELDS:
        assert getattr(tuning, name) == getattr(custom, name), name
    defaults = ExecutorTuning.from_settings(Settings())
    assert defaults == ExecutorTuning()  # the settings defaults and the dataclass defaults agree
    overridden = ExecutorTuning.from_settings(Settings(executor=custom), confirmation_timeout_s=3.0)
    assert overridden.confirmation_timeout_s == 3.0 and overridden.viewport_width == 1280
    with pytest.raises(TypeError, match="unknown"):
        ExecutorTuning.from_settings(Settings(), no_such_knob=1)


@pytest.mark.parametrize(
    "bad",
    [
        {"lock_heartbeat_s": 400.0},  # more than half of lock_stale_after_s (600)
        {"lock_heartbeat_s": 0.0},
        {"confirmation_timeout_s": 0.0},
        {"element_timeout_s": -1.0},
        {"viewport_width": 0},
        {"max_odds_change_rounds": -1},
        {"min_seconds_between_bets": -5.0},
    ],
)
def test_inconsistent_executor_settings_are_reported_at_start_up(
    bad: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    settings = Settings(executor=ExecutorSettings(**bad))
    with pytest.raises(ValueError):
        ExecutorTuning.from_settings(settings)
    with pytest.raises(ValueError):
        create_betclic_executor(settings, session_factory)  # not deferred to the first bet


def test_tuning_validates_sanity_limits() -> None:
    assert ExecutorTuning(max_odds_rise_factor=None).max_odds_rise_factor is None
    with pytest.raises(ValueError):
        ExecutorTuning(max_odds_rise_factor=1.0)
    with pytest.raises(ValueError):
        ExecutorTuning(potential_win_min_ratio=1.2)
    with pytest.raises(ValueError):
        ExecutorTuning(typing_delay_min_ms=200, typing_delay_max_ms=100)


def test_create_betclic_executor_uses_settings_tuning_and_explicit_tuning_wins(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    settings = Settings(
        betclic=BetclicSettings(browser_profile_dir=tmp_path / "profile"),
        executor=ExecutorSettings(
            screenshot_dir=tmp_path / "shots",
            confirmation_timeout_s=12.0,
            lock_stale_after_s=120.0,
            lock_heartbeat_s=10.0,
        ),
    )
    seen: list[ExecutorTuning] = []

    def tuning_of(wrapper: ThreadBoundExecutor) -> ExecutorTuning:
        wrapper._call(lambda ex: seen.append(ex.tuning))  # type: ignore[attr-defined]
        return seen[-1]

    wrapper = create_betclic_executor(settings, session_factory, playwright_factory=Factory())
    try:
        tuning = tuning_of(wrapper)
        assert tuning.confirmation_timeout_s == 12.0 and tuning.lock_heartbeat_s == 10.0
    finally:
        wrapper.close()
    explicit = ExecutorTuning(confirmation_timeout_s=99.0)
    wrapper = create_betclic_executor(
        settings, session_factory, tuning=explicit, playwright_factory=Factory()
    )
    try:
        assert tuning_of(wrapper) is explicit
    finally:
        wrapper.close()


# ------------------------------------------------------------------------------------ lock


def test_lost_lock_is_taken_again_or_refused(tmp_path: Path) -> None:
    lock = ProfileLock(tmp_path, heartbeat_s=None)
    lock.acquire()
    assert lock.verify() is True
    lock.path.unlink()  # someone deleted the lock file
    assert lock.verify() is False and lock.lost
    lock.acquire()  # nobody else holds it: simply taken again
    assert lock.verify() is True and not lock.lost
    # another live process took the profile over
    now = utcnow()
    lock.path.write_text(
        LockInfo(os.getpid(), socket.gethostname(), "someone-else", now, now).to_json(), encoding="utf-8"
    )
    assert lock.verify() is False
    with pytest.raises(ProfileLockedError):
        lock.acquire()
    assert not lock.held
    info = lock.read_info()
    assert info is not None and info.token == "someone-else"  # their lock file was left alone
    lock.release()
    assert lock.path.exists()


def test_placement_is_refused_when_the_profile_lock_was_taken_over(settings: Settings) -> None:
    factory = Factory()
    executor = BetclicExecutor(
        settings, halt_check=lambda: (False, None), playwright_factory=factory, sleep=lambda _s: None
    )
    assert executor.get_balance() is None  # starts the (fake) browser and takes the lock
    lock_path = Path(settings.betclic.browser_profile_dir) / LOCK_FILENAME
    now = utcnow()
    lock_path.write_text(
        LockInfo(os.getpid(), socket.gethostname(), "another-bot", now, now).to_json(), encoding="utf-8"
    )
    result = executor.place(_order())
    assert result.status is PlacementStatus.FAILED and "in use" in result.message
    context = factory.created[0].chromium.contexts[0]
    assert context.closed  # our browser left the profile to its new holder
    assert context.pages[0].gotos == [settings.betclic.base_url + "/"]  # nothing opened for the bet
    executor.close()
    assert lock_path.exists()  # still the other bot's lock


# --------------------------------------------------------------------------------- wrapper


def test_wrapper_place_reports_a_broken_factory_as_failed_and_recovers() -> None:
    attempts = {"n": 0}

    def factory() -> FakeExecutor:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise SelectorConfigError("invalid selector file config/betclic_selectors.yaml: boom")
        return FakeExecutor()

    wrapper = ThreadBoundExecutor(factory)
    try:
        result = wrapper.place(_order())
        assert result.status is PlacementStatus.FAILED
        assert "SelectorConfigError" in result.message and "boom" in result.message
        assert not wrapper.started
        assert wrapper.place(_order()).status is PlacementStatus.PLACED  # the file was fixed: no restart
    finally:
        wrapper.close()
    with pytest.raises(ExecutorClosedError):
        wrapper.place(_order())
    broken = ThreadBoundExecutor(lambda: (_ for _ in ()).throw(SelectorConfigError("still broken")))
    try:
        with pytest.raises(SelectorConfigError):
            broken.health_check()  # the other methods raise the configuration error
    finally:
        broken.close()


def test_wrapper_never_abandons_a_running_placement() -> None:
    """``call_timeout_s`` must not apply to place(): the bet would go on in the worker unobserved."""
    release = threading.Event()

    def slow(order: BetOrder) -> PlacementResult:
        release.wait(5)
        return PlacementResult(status=PlacementStatus.PLACED, external_bet_id="X1", at=utcnow())

    wrapper = ThreadBoundExecutor(lambda: FakeExecutor([slow]), call_timeout_s=0.05)
    try:
        threading.Timer(0.4, release.set).start()
        result = wrapper.place(_order())  # waits well past call_timeout_s
        assert result.status is PlacementStatus.PLACED and result.external_bet_id == "X1"
    finally:
        release.set()
        wrapper.close()


# ------------------------------------------------------------------------------------- urls


@pytest.mark.parametrize(
    ("url", "event_id", "expected"),
    [
        ("https://www.betclic.pl/e-sport-s102/iem-c1/navi-vitality-m3002201001", "3002201001", True),
        ("https://www.betclic.pl/e-sport-s102/iem-c1/navi-vitality-m3002201001?tab=all", "3002201001", True),
        ("https://www.betclic.pl/e-sport-s102/iem-c1/navi-vitality-m3002201001", "300220100", False),
        ("https://www.betclic.pl/e-sport-s102/iem-c1/navi-vitality-m30022010015", "3002201001", False),
        ("https://www.betclic.pl/e-sport-s102", "3002201001", False),
        ("https://www.betclic.pl/e-sport-s102/x-mEV-77a", "EV-77a", True),
        ("https://www.betclic.pl/e-sport-s102/x-m1", "", False),
    ],
)
def test_url_mentions_id(url: str, event_id: str, expected: bool) -> None:
    assert _url_mentions_id(url, event_id) is expected
