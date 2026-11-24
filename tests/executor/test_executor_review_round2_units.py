"""Unit regressions of review round 2 (no browser)."""

from __future__ import annotations

import threading
import time
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
    BookmakerBetStatus,
    ThreadBoundExecutor,
    create_betclic_executor,
    never_halted,
)
from cs2bot.executor.betclic import _latest_possible
from cs2bot.executor.factory import journal_clicked_check
from cs2bot.executor.journal import JOURNAL_FILENAME, PlacementJournal
from cs2bot.executor.selectors import load_selectors
from cs2bot.executor.textutils import names_compatible, normalise_text
from cs2bot.executor.tuning import ExecutorTuning
from cs2bot.utils.time import WARSAW


def _order(key: str = "pred-1:mw#team1", **overrides: Any) -> BetOrder:
    data: dict[str, Any] = {
        "bet_id": 7,
        "idempotency_key": key,
        "external_event_id": "3002201001",
        "event_url": "/e-sport-s102/x-m3002201001",
        "market_raw_name": "Zwycięzca meczu",
        "selection_raw_name": "Natus Vincere",
        "requested_odds": Decimal("1.85"),
        "min_acceptable_odds": Decimal("1.80"),
        "stake": Decimal("12.50"),
        "dry_run": False,
    }
    data.update(overrides)
    return BetOrder(**data)


def _settings(tmp_path: Path, **betclic: Any) -> Settings:
    return Settings(
        betclic=BetclicSettings(browser_profile_dir=tmp_path / "profile", **betclic),
        executor=ExecutorSettings(dry_run=False, screenshot_dir=tmp_path / "shots"),
    )


# ------------------------------------------------------------------ slip name sanity check


@pytest.mark.parametrize(
    ("shown", "wanted", "expected"),
    [
        ("Natus Vincere -1", "Natus Vincere -1,5", False),  # another handicap line
        ("Powyżej 26", "Powyżej 26,5", False),  # another total line
        ("5 rund", "Powyżej 26,5 rund", False),  # "5" is part of 26.5
        ("Powyżej 2,5", "Powyżej 26,5", False),
        ("Natus Vincere", "Natus Vincere -1,5", True),  # name without the line still fits
        ("Vitality (+1,5)", "Vitality +1,5", True),
        ("Powyżej 26,5", "Powyżej 26,5 rund", True),
    ],
)
def test_names_compatible_treats_numbers_as_whole(shown: str, wanted: str, expected: bool) -> None:
    assert names_compatible(normalise_text(shown), normalise_text(wanted)) is expected
    assert names_compatible(normalise_text(wanted), normalise_text(shown)) is expected


# ------------------------------------------------------------------ bet history: day rows across DST


@pytest.mark.parametrize(
    ("day", "hours"),
    [((2026, 10, 25), 25), ((2026, 3, 29), 23), ((2026, 11, 22), 24)],
    ids=["clocks-back", "clocks-forward", "normal"],
)
def test_latest_possible_day_row_ends_at_the_next_local_midnight(
    day: tuple[int, int, int], hours: int
) -> None:
    midnight = datetime(*day, tzinfo=WARSAW).astimezone(UTC)
    end = _latest_possible(midnight)
    assert end - midnight == timedelta(hours=hours)
    local_end = end.astimezone(WARSAW)
    assert (local_end.hour, local_end.minute) == (0, 0)
    # a bet placed at 23:30 local on the 25-hour day is still found
    late = (datetime(*day, 23, 30, tzinfo=WARSAW)).astimezone(UTC)
    assert end >= late


# ------------------------------------------------------------------ bet history: status of a row


def test_whole_row_status_is_unknown_when_several_statuses_match(tmp_path: Path) -> None:
    executor = BetclicExecutor(_settings(tmp_path), halt_check=never_halted)  # no browser is started
    try:
        classify = executor._classify_history_status
        # an OPEN bet offering a cash-out button must not be reported as cashed out
        assert classify("W grze Możliwa wygrana: 20,35 zł Cash out 9,80 zł", whole_row=True) is (
            BookmakerBetStatus.UNKNOWN
        )
        assert classify("W grze Możliwa wygrana: 20,35 zł", whole_row=True) is BookmakerBetStatus.OPEN
        assert classify("Przegrany Wygrana: 0,00 zł", whole_row=True) is BookmakerBetStatus.LOST
        assert classify("Wygrany Wygrana: 20,35 zł", whole_row=True) is BookmakerBetStatus.WON
        assert classify("nothing known here", whole_row=True) is BookmakerBetStatus.UNKNOWN
        # a status LABEL is short: the configured order decides
        assert classify("Cashout") is BookmakerBetStatus.CASHOUT
        assert classify("Wygrany") is BookmakerBetStatus.WON
    finally:
        executor.close()


# ------------------------------------------------------------------ wrapper: executor cannot be created


def _broken_factory() -> Any:
    raise RuntimeError("selector file is broken")


def test_wrapper_answers_the_clicked_check_when_the_executor_cannot_be_created() -> None:
    seen: list[str] = []

    def check(order: BetOrder) -> PlacementResult | None:
        seen.append(order.idempotency_key)
        if order.idempotency_key == "clicked":
            return PlacementResult(status=PlacementStatus.UNCERTAIN, message="journal", at=datetime.now(UTC))
        return None

    with ThreadBoundExecutor(_broken_factory, clicked_check=check) as wrapper:
        assert wrapper.place(_order("clicked")).status is PlacementStatus.UNCERTAIN
        result = wrapper.place(_order("fresh"))
        assert result.status is PlacementStatus.FAILED
        assert "could not be created" in result.message
    assert seen == ["clicked", "fresh"]


def test_wrapper_falls_back_to_failed_when_the_clicked_check_raises() -> None:
    def check(order: BetOrder) -> PlacementResult | None:
        raise OSError("disk gone")

    with ThreadBoundExecutor(_broken_factory, clicked_check=check) as wrapper:
        assert wrapper.place(_order()).status is PlacementStatus.FAILED


def test_journal_clicked_check(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    check = journal_clicked_check(settings, ExecutorTuning())
    journal = PlacementJournal(tmp_path / "profile" / JOURNAL_FILENAME)
    assert check(_order("k-click")) is None  # no journal yet
    journal.record_click("k-click", 1)
    journal.record_click("k-placed", 2)
    journal.record_result("k-placed", 2, PlacementStatus.PLACED)
    journal.record_click("k-rejected", 3)
    journal.record_result("k-rejected", 3, PlacementStatus.INSUFFICIENT_BALANCE)

    crashed = check(_order("k-click"))
    assert crashed is not None and crashed.status is PlacementStatus.UNCERTAIN
    assert "refusing to place twice" in crashed.message and "stopped right after the click" in crashed.message
    placed = check(_order("k-placed"))
    assert placed is not None and placed.status is PlacementStatus.UNCERTAIN
    assert check(_order("k-rejected")) is None  # a definite rejection may be retried
    assert check(_order("k-click", dry_run=True)) is None  # dry runs never click
    dry_settings = settings.model_copy(
        update={"executor": settings.executor.model_copy(update={"dry_run": True})}
    )
    assert journal_clicked_check(dry_settings, ExecutorTuning())(_order("k-click")) is None


def test_create_betclic_executor_never_answers_failed_for_a_clicked_key(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    """A broken selector file makes the executor impossible to create; a key the journal shows as
    clicked (earlier run) must still come back UNCERTAIN - FAILED would tell the caller "no bet"."""
    settings = _settings(tmp_path, selectors_file=tmp_path / "missing.yaml")
    PlacementJournal(tmp_path / "profile" / JOURNAL_FILENAME).record_click("k-clicked", 1)
    with create_betclic_executor(settings, session_factory) as wrapper:
        clicked = wrapper.place(_order("k-clicked"))
        assert clicked.status is PlacementStatus.UNCERTAIN, clicked.message
        fresh = wrapper.place(_order("k-fresh"))
        assert fresh.status is PlacementStatus.FAILED
        assert "SelectorConfigError" in fresh.message


def test_selector_file_still_loads() -> None:
    """The YAML edited in this round stays valid."""
    selectors = load_selectors(settings=Settings())
    assert "readable bet id" in selectors.element("bet_receipt").description


# ------------------------------------------------------------------ wrapper: shutdown during a login wait


class _LoginWaiter:
    """Stand-in executor whose login window waits until it is aborted (or 30 s pass)."""

    def __init__(self) -> None:
        self.aborted = threading.Event()
        self.waiting = threading.Event()
        self.closed = False

    def abort_login(self) -> None:
        self.aborted.set()

    def open_login_window(self, timeout_s: float = 600.0) -> bool:
        self.waiting.set()
        return not self.aborted.wait(timeout=30)  # False once aborted

    def close(self) -> None:
        self.closed = True


def test_wrapper_close_aborts_a_waiting_login_window() -> None:
    inner = _LoginWaiter()
    wrapper = ThreadBoundExecutor(lambda: inner)
    outcome: dict[str, bool] = {}
    caller = threading.Thread(target=lambda: outcome.update(ok=wrapper.open_login_window(30)))
    caller.start()
    assert inner.waiting.wait(timeout=5)
    started = time.monotonic()
    wrapper.close()  # before the fix: blocked until the login window timed out
    assert time.monotonic() - started < 5
    caller.join(timeout=5)
    assert outcome == {"ok": False} and inner.closed
