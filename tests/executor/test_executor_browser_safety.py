"""Adversarial safety scenarios for BetclicExecutor against the local mock site (review round 1).

Each test reproduces a way a real, live-updating bookmaker page could trick an automated bettor into the
wrong bet, the wrong stake, odds below the minimum or a second placement - and proves the executor stops
before the final button (asserted through ``site.state.place_calls``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cs2bot.config import Settings
from cs2bot.domain.betting import PlacementStatus
from cs2bot.executor.betclic import BetclicExecutor
from cs2bot.executor.journal import JOURNAL_FILENAME
from cs2bot.executor.selectors import SelectorSet, load_selectors
from tests.executor.conftest import FAST_TUNING, ExecutorFactory, OrderFactory, requires_chromium
from tests.executor.mock_betclic import EVENT_ID, MockBetclicServer, parse_pl

pytestmark = [pytest.mark.browser, requires_chromium]

Hook = Callable[[Any], bool]


def _hooked_executor(
    make_executor: ExecutorFactory, executor_settings: Settings, hook: Hook, **kwargs: Any
) -> BetclicExecutor:
    """Executor whose human-like pauses call ``hook(page)`` until it returns True (fires once).

    The hook runs on the executor's own thread, so it may drive the page (like the site's own scripts
    re-rendering it while the bot "thinks").
    """
    holder: dict[str, Any] = {"done": False}

    def sleep(_seconds: float) -> None:
        executor = holder.get("executor")
        if holder["done"] or executor is None:
            return
        page = executor._session.current_page()
        if page is not None and hook(page):
            holder["done"] = True

    settings = executor_settings.model_copy(
        update={
            "executor": executor_settings.executor.model_copy(
                update={"action_delay_min_s": 0.001, "action_delay_max_s": 0.002}
            )
        }
    )
    executor = make_executor(settings=settings, sleep=sleep, **kwargs)
    holder["executor"] = executor
    return executor


def _selectors_with(**behaviour: Any) -> SelectorSet:
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    data["behaviour"].update(behaviour)
    return SelectorSet.model_validate(data)


# ------------------------------------------------------------------ wrong market / selection


def test_page_rerender_before_the_selection_click_never_bets_on_another_market(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Markets are located by position; a re-render that reorders them must not redirect the click."""

    def reorder(page: Any) -> bool:
        if EVENT_ID not in page.url or page.locator(".marketBox").count() == 0 or site.state.slip:
            return False
        with site.lock:
            markets = site.state.events[EVENT_ID].markets
            markets[0], markets[1] = markets[1], markets[0]  # "Zwycięzca mapy 1" now comes first
        page.evaluate("window.mockReload()")
        return True

    executor = _hooked_executor(make_executor, executor_settings, reorder)
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.75")))
    assert result.status is not PlacementStatus.PLACED, result.message
    assert site.state.place_calls == [] and site.state.bets == []
    assert site.state.slip == []
    assert "changed" in result.message


def test_slip_showing_another_market_is_refused(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.slip_market_override = "Zwycięzca mapy 1"
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.SELECTOR_ERROR
    assert "Zwycięzca mapy 1" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


def test_slip_market_check_can_be_relaxed_in_the_selector_file(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.slip_market_override = "Zwycięzca mapy 1"
    executor = make_executor(selectors=_selectors_with(require_slip_market_match=False))
    assert executor.place(make_order(dry_run=True)).status is PlacementStatus.DRY_RUN_OK


def test_hidden_stale_slip_selection_blocks_the_bet(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """A stale selection that is in the DOM but hidden would turn the single bet into a combination."""
    site.state.hidden_slip_item = True
    strict = make_executor()
    result = strict.place(make_order())
    assert result.status is PlacementStatus.SELECTOR_ERROR
    assert "hidden" in result.message
    assert site.state.place_calls == []
    strict.close()
    relaxed = make_executor(selectors=_selectors_with(count_hidden_slip_items=False))
    assert relaxed.place(make_order(dry_run=True)).status is PlacementStatus.DRY_RUN_OK


# ----------------------------------------------------------------------------------- stake


def test_stake_changed_by_the_page_before_the_final_click_is_caught(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    def bump_stake(page: Any) -> bool:
        field = page.locator("#stake")
        if field.count() == 0 or field.input_value() != "12,50":
            return False
        page.evaluate(
            "() => { const f = document.getElementById('stake'); f.value = '125,0'; "
            "f.dispatchEvent(new Event('input', {bubbles: true})); }"
        )
        return True

    executor = _hooked_executor(make_executor, executor_settings, bump_stake)
    result = executor.place(make_order(stake=Decimal("12.50")))
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "stake" in result.message and "125,0" in result.message
    assert site.state.place_calls == [] and site.state.balance == Decimal("1234.56")


def test_potential_win_cross_check_catches_a_misread_stake(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The field shows "12,50" but the site understood 1250: the potential win gives it away."""
    site.state.stake_multiplier = Decimal("100")
    executor = make_executor(tuning=replace(FAST_TUNING, slip_update_timeout_s=0.8))
    result = executor.place(make_order(stake=Decimal("12.50")))
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "potential win" in result.message
    assert site.state.place_calls == [] and site.state.balance == Decimal("1234.56")
    # also in dry-run mode: the smoke test must reveal the problem before real money is used
    dry = executor.place(make_order(dry_run=True))
    assert dry.status is PlacementStatus.SELECTOR_ERROR


def test_placement_works_without_a_potential_win_line(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.show_potential_win = False
    assert make_executor().place(make_order()).status is PlacementStatus.PLACED


def test_number_input_stake_field_gets_the_canonical_value(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """<input type="number"> may refuse the typed comma; its DOM value is "12.50" in every locale."""

    def make_numeric(page: Any) -> bool:
        field = page.locator("#stake")
        if field.count() == 0:
            return False
        page.evaluate("() => { document.getElementById('stake').type = 'number'; }")
        return True

    executor = _hooked_executor(make_executor, executor_settings, make_numeric)
    result = executor.place(make_order(stake=Decimal("12.50")))
    assert result.status is PlacementStatus.PLACED, result.message
    assert parse_pl(site.state.place_calls[0]["stake_text"]) == Decimal("12.50")
    assert site.state.balance == Decimal("1222.06")


def test_two_stake_fields_are_ambiguous(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    def add_second_field(page: Any) -> bool:
        if page.locator("#stake").count() == 0:
            return False
        page.evaluate(
            "() => { const i = document.createElement('input'); i.setAttribute('inputmode', 'decimal'); "
            "i.setAttribute('aria-label', 'Stawka'); "
            "document.querySelector('.betslip_stake').appendChild(i); }"
        )
        return True

    executor = _hooked_executor(make_executor, executor_settings, add_second_field)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "slip_stake_input" in result.message
    assert site.state.place_calls == []


# ------------------------------------------------------------------------------------ odds


def test_odds_dialog_number_is_cross_checked_against_the_slip(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The dialog announces 1,90 but the slip already shows 1,60: accept nothing below the minimum."""
    site.script_place({"result": "odds_changed", "new_odds": "1,60", "dialog_odds": "1,90"})
    result = make_executor().place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert site.state.accept_calls == [] and site.state.decline_calls == 1
    assert site.state.bets == []


def test_lingering_odds_notice_after_accepting_is_not_a_rejection(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """Accepting new odds submits the bet; the old "odds changed" text stays visible while the server
    works. Reading it as ODDS_MOVED would leave a real bet at the bookmaker that the bot knows nothing
    about (and may place again)."""
    site.script_place({"result": "odds_changed", "new_odds": "1,82", "message": "Kurs uległ zmianie"})
    site.state.place_delay_s = 0.8
    result = make_executor().place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.external_bet_id == "BC1001" and result.placed_odds == Decimal("1.82")
    assert len(site.state.bets) == 1


def test_lingering_odds_notice_without_a_receipt_is_uncertain(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place(
        {"result": "odds_changed", "new_odds": "1,82", "message": "Kurs uległ zmianie"}, {"result": "hang"}
    )
    executor = make_executor(tuning=replace(FAST_TUNING, confirmation_timeout_s=1.5))
    order = make_order(min_acceptable_odds=Decimal("1.80"))
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN, result.message  # never a "definite" ODDS_MOVED
    assert executor.place(order).status is PlacementStatus.UNCERTAIN  # and never clicked again
    assert len(site.state.place_calls) == 1 and len(site.state.accept_calls) == 1


def test_odds_far_above_the_requested_price_are_not_taken_blindly(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.set_odds("S1001", "3,10")  # requested 1.85: such a jump means news the model has not seen
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.ODDS_MOVED
    assert "3.10" in result.message and "above" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


# ------------------------------------------------------------------------- the final click


def test_blocked_place_button_is_a_definite_failure_not_uncertain(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """An overlay over "Postaw zakład": nothing was clicked, so the bet may be retried later."""

    def cover(page: Any) -> bool:
        field = page.locator("#stake")
        if field.count() == 0 or field.input_value() != "12,50":
            return False
        page.evaluate(
            "() => { const o = document.createElement('div'); o.id = 'overlay'; "
            "o.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.4)'; "
            "document.body.appendChild(o); }"
        )
        return True

    quick = replace(FAST_TUNING, action_timeout_s=1.0, element_timeout_s=1.0, slip_update_timeout_s=1.0)
    executor = _hooked_executor(make_executor, executor_settings, cover, tuning=quick)
    order = make_order()
    result = executor.place(order)
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "not clickable" in result.message
    assert site.state.place_calls == []
    journal = Path(executor_settings.betclic.browser_profile_dir) / JOURNAL_FILENAME
    assert not journal.exists() or order.idempotency_key not in journal.read_text(encoding="utf-8")
    page = executor._session.current_page()
    assert page is not None
    page.evaluate("() => document.getElementById('overlay').remove()")
    assert executor.place(order).status is PlacementStatus.PLACED  # same key: nothing had been clicked


def test_stale_success_message_never_confirms_a_new_bet(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """A leftover "Zakład przyjęty" notice must not be read as this bet's confirmation."""
    site.state.extra_slip_notice = "Zakład przyjęty"
    site.script_place({"result": "hang"})
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    # receipt only by its own element: the notice is then seen by the message classification alone
    data["elements"]["bet_receipt"]["locators"] = [{"css": ".betslip_receipt"}]
    result = make_executor(selectors=SelectorSet.model_validate(data)).place(make_order())
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "earlier bet" in result.message
    assert site.state.place_calls == [] and site.state.bets == []


def test_harmless_notice_listed_under_texts_ignore_does_not_block(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.extra_slip_notice = "Podatek 12% jest pobierany od stawki"
    first = make_executor()
    blocked = first.place(make_order())
    assert blocked.status is PlacementStatus.FAILED and "texts.ignore" in blocked.message
    assert site.state.place_calls == []
    first.close()
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    data["texts"]["ignore"] = ["podatek 12%"]
    placed = make_executor(selectors=SelectorSet.model_validate(data)).place(make_order())
    assert placed.status is PlacementStatus.PLACED, placed.message


def test_harmless_notice_does_not_hide_a_real_error(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """Messages are classified one by one: an ignorable notice next to a stake-limit error."""
    site.state.extra_slip_notice = "Podatek 12% jest pobierany od stawki"
    site.state.max_stake = Decimal("10.00")
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    data["texts"]["ignore"] = ["podatek 12%"]
    data["elements"]["slip_error"]["locators"] = [{"css": ".betslip [role='alert']"}]  # matches both
    result = make_executor(selectors=SelectorSet.model_validate(data)).place(make_order())
    assert result.status is PlacementStatus.REJECTED_BY_BOOKMAKER, result.message
    assert "Maksymalna stawka" in result.message and "Podatek" not in result.message
    assert site.state.place_calls == []


def test_unknown_notice_followed_by_the_receipt_is_placed(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """An unrecognised progress message right after the click must not hide the receipt behind it."""
    site.state.place_delay_s = 1.0  # the receipt arrives one second after the click

    def plant_progress_message(page: Any) -> bool:
        field = page.locator("#stake")
        if field.count() == 0 or field.input_value() != "12,50":
            return False
        page.evaluate("() => { ui.errorText = 'Wysyłamy Twój kupon'; }")  # rendered while pending
        return True

    executor = _hooked_executor(make_executor, executor_settings, plant_progress_message)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.external_bet_id == "BC1001"
    assert len(site.state.place_calls) == 1


def test_receipt_pending_bookmaker_approval_is_uncertain_not_placed(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """ "Przyjęty, oczekuje na akceptację": a trader may still refuse the bet - not a confirmation."""
    site.script_place({"result": "ok", "note": "Zakład oczekuje na akceptację."})
    executor = make_executor(tuning=replace(FAST_TUNING, confirmation_timeout_s=1.5))
    order = make_order()
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert "oczekuje na akceptację" in result.message
    assert executor.place(order).status is PlacementStatus.UNCERTAIN  # reconciled, never re-sent
    assert len(site.state.place_calls) == 1


# ------------------------------------------------------------------- idempotency / journal


def test_uncertain_bet_is_refused_after_a_restart(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    site.script_place({"result": "hang"})
    order = make_order()
    first = make_executor(tuning=replace(FAST_TUNING, confirmation_timeout_s=1.0))
    assert first.place(order).status is PlacementStatus.UNCERTAIN
    first.close()  # the process restarts: the in-memory record is gone
    second = make_executor()
    again = second.place(order)
    assert again.status is PlacementStatus.UNCERTAIN
    assert "refusing to place twice" in again.message
    assert len(site.state.place_calls) == 1
    # a dry run with that key never clicks anyway, and other keys are unaffected
    assert second.place(order.model_copy(update={"dry_run": True})).status is PlacementStatus.DRY_RUN_OK
    assert second.place(make_order()).status is PlacementStatus.PLACED
    assert len(site.state.place_calls) == 2


def test_crash_right_after_the_click_blocks_the_key_after_a_restart(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Power loss between the click and the result: the write-ahead record alone blocks a second click."""
    order = make_order()
    executor = make_executor()

    def crash(_button: Any) -> None:
        raise KeyboardInterrupt  # stands in for the process dying right at the click

    executor._click_place_button = crash  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        executor.place(order)
    executor.close()
    journal = Path(executor_settings.betclic.browser_profile_dir) / JOURNAL_FILENAME
    lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [(e["event"], e["key"]) for e in lines] == [("click", order.idempotency_key)]
    restarted = make_executor()
    result = restarted.place(order)
    assert result.status is PlacementStatus.UNCERTAIN and "refusing to place twice" in result.message
    assert site.state.place_calls == []


def test_definite_rejection_is_retryable_after_a_restart(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.balance = Decimal("5.00")
    order = make_order()
    first = make_executor()
    assert first.place(order).status is PlacementStatus.INSUFFICIENT_BALANCE
    first.close()
    site.state.balance = Decimal("100.00")
    retry = make_executor().place(order)
    assert retry.status is PlacementStatus.PLACED, retry.message
    assert len(site.state.bets) == 1


def test_unwritable_journal_refuses_real_bets_but_allows_dry_runs(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    journal = Path(executor_settings.betclic.browser_profile_dir) / JOURNAL_FILENAME
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.mkdir()  # a directory where the journal file should be: it can be neither read nor written
    executor = make_executor()
    result = executor.place(make_order())
    assert result.status is PlacementStatus.FAILED and "journal" in result.message
    assert site.state.place_calls == [] and result.screenshot_path is None  # refused before the browser
    assert executor.place(make_order(dry_run=True)).status is PlacementStatus.DRY_RUN_OK
    health = executor.health_check()
    assert health.ok is False and "journal" in health.message  # visible on the dashboard


# ------------------------------------------------------------------------------ event page


def test_redirect_to_another_event_is_market_unavailable(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """A finished event redirecting to a live one whose id merely CONTAINS ours is not our event."""
    old_path = "/e-sport-s102/iem-katowice-c1234/navi-vitality-m300220100"
    site.state.redirects[old_path] = site.event_path()  # ...-m3002201001: same teams, same market names
    order = make_order(external_event_id="300220100", event_url=old_path)
    result = make_executor().place(order)
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE, result.message
    assert "redirected" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


def test_tuning_defaults_come_from_settings(executor_settings: Settings, site: MockBetclicServer) -> None:
    settings = executor_settings.model_copy(
        update={
            "executor": executor_settings.executor.model_copy(
                update={"confirmation_timeout_s": 7.5, "viewport_width": 1280, "lock_heartbeat_s": 11.0}
            )
        }
    )
    executor = BetclicExecutor(settings, halt_check=lambda: (False, None))
    try:
        assert executor.tuning.confirmation_timeout_s == 7.5
        assert executor.tuning.viewport_width == 1280
        assert executor._session.lock.heartbeat_s == 11.0
    finally:
        executor.close()
    explicit = BetclicExecutor(settings, halt_check=lambda: (False, None), tuning=FAST_TUNING)
    assert explicit.tuning is FAST_TUNING
    explicit.close()
