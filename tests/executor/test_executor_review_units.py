"""Unit tests added in the independent review (no browser): journal robustness, URL and order guards."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cs2bot.config import BetclicSettings, ExecutorSettings, Settings
from cs2bot.domain.betting import BetOrder, PlacementStatus
from cs2bot.executor import BetclicExecutor, ExecutorTuning, JournalError, PlacementJournal, ProfileLock
from cs2bot.executor.betclic import _Attempt, _latest_possible
from cs2bot.executor.journal import JOURNAL_FILENAME
from cs2bot.executor.tuning import SETTINGS_FIELDS
from cs2bot.utils.time import utcnow
from tests.executor.test_executor_launch import Factory

TUNING = ExecutorTuning(lock_stale_after_s=60, lock_heartbeat_s=5)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        betclic=BetclicSettings(browser_profile_dir=tmp_path / "profile"),
        executor=ExecutorSettings(
            dry_run=False, screenshot_dir=tmp_path / "shots", action_delay_min_s=0, action_delay_max_s=0
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


def _executor(settings: Settings, factory: Factory | None = None) -> BetclicExecutor:
    return BetclicExecutor(
        settings,
        halt_check=lambda: (False, None),
        playwright_factory=factory or Factory(),
        tuning=TUNING,
        sleep=lambda _s: None,
    )


# --------------------------------------------------------------------------------- journal


def test_journal_record_after_a_torn_line_is_not_lost(tmp_path: Path) -> None:
    """The process died inside an earlier write (no trailing newline). The next "click" record must
    still be a line of its own - glued to the torn fragment it would be skipped and block nothing."""
    path = tmp_path / JOURNAL_FILENAME
    path.write_bytes(b'{"event": "result", "key": "old", "bet_id": "1", "sta')
    journal = PlacementJournal(path)
    journal.record_click("k-new", 7)
    entry = journal.blocking_entry("k-new")
    assert entry is not None and entry.event == "click" and entry.bet_id == "7"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["key"] == "k-new"
    journal.record_result("k-new", 7, PlacementStatus.ODDS_MOVED)
    assert journal.blocking_entry("k-new") is None
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3  # no blank lines added in between


def test_journal_appends_do_not_add_blank_lines(tmp_path: Path) -> None:
    journal = PlacementJournal(tmp_path / "deep" / JOURNAL_FILENAME)
    journal.record_click("ą-klucz", 1)
    journal.record_result("ą-klucz", 1, PlacementStatus.PLACED)
    raw = journal.path.read_bytes()
    assert raw.count(b"\n") == 2 and raw.endswith(b"\n") and b"\r" not in raw
    assert [e.event for e in journal.entries()] == ["click", "result"]
    assert journal.entries()[0].key == "ą-klucz"


@pytest.mark.parametrize(
    "key",
    [
        "line\u2028separator",  # str.splitlines() would cut the record in two at any of these
        "paragraph\u2029separator",
        "next\x85line",
        "form\x0cfeed",
        "new\nline and\rreturn",
        "lone-\ud800-surrogate",
        "zakład-ż:1#team1 🏆",
    ],
)
def test_journal_click_record_blocks_whatever_characters_the_key_contains(tmp_path: Path, key: str) -> None:
    journal = PlacementJournal(tmp_path / JOURNAL_FILENAME)
    journal.record_click(key, 1)
    journal.record_click("other", 2)
    entry = journal.blocking_entry(key)
    assert entry is not None and entry.key == key
    assert journal.path.read_bytes().count(b"\n") == 2 and journal.path.read_bytes().isascii()
    journal.record_result(key, 1, PlacementStatus.UNCERTAIN)
    assert journal.blocking_entry(key) is not None
    assert journal.compact() == 0  # nothing is malformed
    assert [e.key for e in PlacementJournal(journal.path).entries()] == [key, "other", key]


def test_journal_reads_records_written_with_raw_non_ascii_keys(tmp_path: Path) -> None:
    """Files written by the first version (``ensure_ascii=False``) stay readable, line breaks = \\n only."""
    path = tmp_path / JOURNAL_FILENAME
    at = datetime.now(UTC).isoformat()
    raw = json.dumps(
        {"event": "click", "key": "stary\u2028klucz-ż", "bet_id": "1", "at": at}, ensure_ascii=False
    )
    path.write_text(raw + "\r\n", encoding="utf-8")  # (and a Windows line ending)
    journal = PlacementJournal(path)
    assert journal.blocking_entry("stary\u2028klucz-ż") is not None


def test_journal_write_errors_are_journal_errors(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    journal = PlacementJournal(blocker / JOURNAL_FILENAME)  # the parent is a file
    with pytest.raises(JournalError):
        journal.record_click("k", 1)


def test_constructing_an_executor_never_rewrites_the_journal(settings: Settings) -> None:
    """``cs2bot executor login`` in a second process builds an executor while ``cs2bot up`` holds the
    profile and appends to the journal: only the lock holder may compact (rewrite) the file."""
    profile = Path(settings.betclic.browser_profile_dir)
    profile.mkdir(parents=True)
    journal_path = profile / JOURNAL_FILENAME
    old = (utcnow() - timedelta(days=400)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    content = (
        json.dumps({"event": "click", "key": "expired", "bet_id": "1", "at": old})
        + "\n"
        + json.dumps({"event": "click", "key": "live", "bet_id": "2", "at": fresh})
        + "\n"
    )
    journal_path.write_text(content, encoding="utf-8")
    holder = ProfileLock(profile, heartbeat_s=None)
    holder.acquire()  # "the running bot"
    try:
        factory = Factory()
        executor = _executor(settings, factory)
        assert journal_path.read_text(encoding="utf-8") == content  # untouched by the constructor
        result = executor.place(_order(idempotency_key="other"))
        assert result.status is PlacementStatus.FAILED and "in use" in result.message
        assert journal_path.read_text(encoding="utf-8") == content  # and by a refused placement
        assert executor.place(_order(idempotency_key="live")).status is PlacementStatus.UNCERTAIN
        executor.close()
    finally:
        holder.release()
    # the lock holder compacts once, on its first placement
    owner = _executor(settings)
    assert owner.place(_order(idempotency_key="other")).status is PlacementStatus.FAILED  # fake goto error
    kept = journal_path.read_text(encoding="utf-8")
    assert "expired" not in kept and '"live"' in kept
    owner.close()


# ------------------------------------------------------------------------------ URL guard


@pytest.mark.parametrize(
    "url",
    [
        "http://www.betclic.pl/e-sport-s102/a-b-m1",  # https -> http downgrade (cookies in clear text)
        "https://www.betclic.pl.evil.example/e-sport-s102/a-b-m1",
        "https://evil.example/www.betclic.pl/a-b-m1",
        "https://www.betclic.pl@evil.example/a-b-m1",
        "https://www.betclic.pl:8443/a-b-m1",
        "javascript:alert(1)",
        "file:///C:/Users/owner/secret.txt",
        "",
    ],
)
def test_safe_url_refuses_anything_but_the_configured_site(settings: Settings, url: str) -> None:
    executor = _executor(settings)
    with pytest.raises(ValueError):
        executor._safe_url(url)
    result = executor.place(_order(event_url=url or "ftp://www.betclic.pl/x"))
    assert result.status is PlacementStatus.FAILED
    executor.close()


def test_safe_url_accepts_paths_and_same_site_urls(settings: Settings) -> None:
    executor = _executor(settings)
    assert executor._safe_url("/e-sport-s102/a-b-m1") == "https://www.betclic.pl/e-sport-s102/a-b-m1"
    assert executor._safe_url("https://betclic.pl/x-m1") == "https://betclic.pl/x-m1"  # www. is optional
    assert executor._safe_url("HTTPS://WWW.BETCLIC.PL/x-m1") == "HTTPS://WWW.BETCLIC.PL/x-m1"
    # a protocol-relative lookalike stays on the configured host (it becomes a path)
    assert executor._safe_url("//evil.example/x").startswith("https://www.betclic.pl//evil.example")
    executor.close()


# ---------------------------------------------------------------------------- order guard


@pytest.mark.parametrize("field", ["stake", "requested_odds", "min_acceptable_odds"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_numbers_are_refused_without_raising(settings: Settings, field: str, value: str) -> None:
    """``model_copy(update=...)`` skips pydantic validation; ``place`` must answer FAILED, not raise."""
    factory = Factory()
    executor = _executor(settings, factory)
    order = _order().model_copy(update={field: Decimal(value)})
    result = executor.place(order)
    assert result.status is PlacementStatus.FAILED
    assert field in result.message and "finite" in result.message
    assert factory.created == []  # no browser was started
    executor.close()


# ------------------------------------------------------------------------ final-click guard


@pytest.mark.parametrize(
    "state",
    [
        {"dry_run": True, "journaled": True, "place_clicked": True},  # a dry run, whatever else is set
        {"dry_run": False, "journaled": False, "place_clicked": True},  # no write-ahead record
        {"dry_run": False, "journaled": True, "place_clicked": False},  # not marked as "maybe placed"
    ],
)
def test_place_button_click_is_guarded_by_the_attempt_state(
    settings: Settings, state: dict[str, bool]
) -> None:
    """Even if a future change of the flow called it by mistake: no click without a journaled real attempt."""

    class Button:
        clicks = 0

        def click(self, **_kwargs: Any) -> None:
            Button.clicks += 1

    executor = _executor(settings)
    button: Any = Button()
    attempt = _Attempt(order=_order(), **state)
    with pytest.raises(RuntimeError, match="BUG"):
        executor._click_place_button(button, attempt)
    assert Button.clicks == 0
    executor._click_place_button(
        button, _Attempt(order=_order(), dry_run=False, journaled=True, place_clicked=True)
    )
    assert Button.clicks == 1
    executor.close()


# ------------------------------------------------------------------ bet-history resolution


def test_latest_possible_covers_the_displayed_minute_or_day() -> None:
    minute = datetime(2026, 11, 22, 17, 30, tzinfo=UTC)  # 18:30 in Warsaw
    assert _latest_possible(minute) == minute + timedelta(minutes=1)
    with_seconds = minute.replace(second=5)
    assert _latest_possible(with_seconds) == with_seconds
    warsaw_midnight = datetime(2026, 11, 21, 23, 0, tzinfo=UTC)  # 00:00 local: only the day was shown
    assert _latest_possible(warsaw_midnight) == warsaw_midnight + timedelta(days=1)
    summer_midnight = datetime(2026, 7, 14, 22, 0, tzinfo=UTC)  # 00:00 CEST
    assert _latest_possible(summer_midnight) == summer_midnight + timedelta(days=1)


# ------------------------------------------------------------------------ tuning from settings


def test_settings_fields_lists_exactly_the_knobs_executor_settings_has_today() -> None:
    from dataclasses import fields

    shared = {f.name for f in fields(ExecutorTuning)} & set(ExecutorSettings.model_fields)
    assert shared == set(SETTINGS_FIELDS)


def test_from_settings_picks_up_tuning_fields_added_to_the_settings_later() -> None:
    """Once ``ExecutorSettings`` gains e.g. ``max_odds_rise_factor`` it takes effect without a code change
    here (simulated with a settings object that already has the field)."""
    from types import SimpleNamespace

    base = ExecutorSettings().model_dump()
    future: Any = SimpleNamespace(
        executor=SimpleNamespace(**{**base, "max_odds_rise_factor": 2.5, "journal_retention_days": 10.0})
    )
    tuning = ExecutorTuning.from_settings(future)
    assert tuning.max_odds_rise_factor == 2.5 and tuning.journal_retention_days == 10.0
    assert tuning.confirmation_timeout_s == base["confirmation_timeout_s"]
    assert ExecutorTuning.from_settings(future, max_odds_rise_factor=None).max_odds_rise_factor is None


# ---------------------------------------------------------------------------- pre-match only


class TimedOrder(BetOrder):
    """``BetOrder`` as it will look once the foundation adds the kick-off time (change request)."""

    event_starts_at: datetime | None = None


def test_order_whose_event_already_started_is_refused_without_a_browser(settings: Settings) -> None:
    factory = Factory()
    executor = _executor(settings, factory)
    base = _order().model_dump()
    late = TimedOrder(**{**base, "event_starts_at": utcnow() - timedelta(seconds=1)})
    result = executor.place(late)
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE
    assert "started" in result.message and "pre-match" in result.message
    assert factory.created == []
    naive = TimedOrder(**base).model_copy(update={"event_starts_at": datetime(2030, 1, 1)})
    refused = executor.place(naive)
    assert refused.status is PlacementStatus.MARKET_UNAVAILABLE and "timezone-aware" in refused.message
    assert factory.created == []
    # a future kick-off (or none at all, as today) goes on to the browser (fake page: navigation error)
    for order in (TimedOrder(**{**base, "event_starts_at": utcnow() + timedelta(hours=2)}), _order()):
        assert executor.place(order).status is PlacementStatus.FAILED
    assert len(factory.created) == 1
    executor.close()


# ------------------------------------------------------- a blocked key is UNCERTAIN, always


def test_blocked_key_is_uncertain_even_when_the_order_is_no_longer_placeable(settings: Settings) -> None:
    """The final button was clicked for this key in an earlier run. Whatever is wrong with the order
    now - executor disabled, stake invalid, event started, trading halted - the answer must stay
    UNCERTAIN: FAILED / MARKET_UNAVAILABLE / KILL_SWITCH would all claim "no bet exists"."""
    journal = PlacementJournal(Path(settings.betclic.browser_profile_dir) / JOURNAL_FILENAME)
    journal.record_click("k1", 1)
    base = _order(idempotency_key="k1").model_dump()
    disabled = settings.model_copy(
        update={"executor": settings.executor.model_copy(update={"enabled": False})}
    )
    cases: list[tuple[Settings, BetOrder, bool]] = [
        (disabled, _order(idempotency_key="k1"), False),
        (settings, _order(idempotency_key="k1", stake=Decimal("0.50")), False),
        (settings, TimedOrder(**{**base, "event_starts_at": utcnow() - timedelta(minutes=5)}), False),
        (settings, _order(idempotency_key="k1"), True),
    ]
    for cfg, order, halted in cases:
        factory = Factory()
        executor = BetclicExecutor(
            cfg, halt_check=lambda h=halted: (h, "paused"), playwright_factory=factory, tuning=TUNING
        )
        result = executor.place(order)
        assert result.status is PlacementStatus.UNCERTAIN, result.message
        assert "refusing to place twice" in result.message
        assert factory.created == []
        executor.close()


# ------------------------------------------------------------------------- selector file


def test_required_potential_win_needs_its_element_at_load_time() -> None:
    """A selector file that demands the potential-win check but does not define the element is
    rejected at start-up (not discovered as a SELECTOR_ERROR on every bet)."""
    from cs2bot.executor.selectors import SelectorSet, load_selectors

    data = load_selectors(settings=Settings()).model_dump(mode="json")
    assert data["behaviour"]["require_potential_win"] is True  # the shipped default
    del data["elements"]["slip_potential_win"]
    with pytest.raises(ValueError, match="slip_potential_win"):
        SelectorSet.model_validate(data)
    data["behaviour"]["require_potential_win"] = False
    assert not SelectorSet.model_validate(data).has("slip_potential_win")
