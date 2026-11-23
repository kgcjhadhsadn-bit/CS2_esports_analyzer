"""Second batch of adversarial browser scenarios (independent review round 1).

Each test was first written against the unmodified executor and failed; together they pin down:

* an odds-change dialog is accepted at most once, even while it lingers on screen;
* the new odds are read again right before the accept click;
* the accept / decline buttons of the dialog are never confused with other buttons of the page (a cookie
  banner's "Akceptuj wszystkie");
* a rejection read off the page after the final click is cross-checked against the balance - a bet that
  WAS taken is never reported as "definitely not placed";
* a selection that turns out to be suspended when clicked is MARKET_UNAVAILABLE, not a selector problem.
"""

from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from cs2bot.config import Settings
from cs2bot.domain.betting import PlacementStatus
from cs2bot.executor.selectors import SelectorSet, load_selectors
from tests.executor.conftest import FAST_TUNING, ExecutorFactory, OrderFactory, requires_chromium
from tests.executor.mock_betclic import EVENT_ID, MockBetclicServer
from tests.executor.test_executor_browser_safety import _hooked_executor

pytestmark = [pytest.mark.browser, requires_chromium]

# A second button that reads exactly like the dialog's accept button, outside the bet slip (think of a
# cookie banner or an "accept odds changes" setting). Clicking it is counted in window.__lookalikeClicks.
_LOOKALIKE_JS = """() => {
  const b = document.createElement('button');
  b.id = 'lookalikeAccept';
  b.textContent = 'Akceptuj zmiany';
  b.addEventListener('click', () => { window.__lookalikeClicks = (window.__lookalikeClicks || 0) + 1; });
  document.body.insertBefore(b, document.body.firstChild);
}"""


def _stake_typed(page: Any) -> bool:
    field = page.locator("#stake")
    return bool(field.count() == 1 and field.input_value() == "12,50")


def _add_lookalike(page: Any) -> bool:
    if not _stake_typed(page):
        return False
    page.evaluate(_LOOKALIKE_JS)
    return True


def _lookalike_clicks(executor: Any) -> int:
    page = executor._session.current_page()
    assert page is not None
    return int(page.evaluate("() => window.__lookalikeClicks || 0"))


def _selectors(**element_locators: list[dict[str, Any]]) -> SelectorSet:
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    for key, locators in element_locators.items():
        data["elements"][key]["locators"] = locators
    return SelectorSet.model_validate(data)


# ------------------------------------------------------------------- odds-change dialog


def test_lingering_odds_dialog_is_accepted_only_once(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The dialog (with a live accept button) stays while the server works: one accept click, no more.

    A second click on "Akceptuj" would send the bet a second time.
    """
    site.state.accept_dialog_lingers = True
    site.state.place_delay_s = 0.9  # longer than the wait after the accept click
    site.script_place({"result": "odds_changed", "new_odds": "1,82"})
    tuning = replace(FAST_TUNING, slip_update_timeout_s=0.3, confirmation_timeout_s=5.0)
    result = make_executor(tuning=tuning).place(make_order(min_acceptable_odds=Decimal("1.80")))
    time.sleep(site.state.place_delay_s + 0.3)  # a second accept request would still be in flight
    assert len(site.state.accept_calls) == 1, site.state.accept_calls
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.placed_odds == Decimal("1.82")
    assert len(site.state.bets) == 1 and len(site.state.place_calls) == 1


def test_odds_dialog_that_never_closes_is_uncertain_after_one_accept(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.accept_dialog_lingers = True
    site.script_place({"result": "odds_changed", "new_odds": "1,82"}, {"result": "hang"})
    tuning = replace(FAST_TUNING, slip_update_timeout_s=0.3, confirmation_timeout_s=1.5)
    order = make_order(min_acceptable_odds=Decimal("1.80"))
    executor = make_executor(tuning=tuning)
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert len(site.state.accept_calls) == 1 and site.state.decline_calls == 0
    assert "still displayed" in result.message
    assert executor.place(order).status is PlacementStatus.UNCERTAIN  # never sent again
    assert len(site.state.place_calls) == 1 and len(site.state.accept_calls) == 1


def test_second_odds_change_right_after_accepting_is_a_new_dialog(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The server answers the accept at once with ANOTHER odds change: the page never shows "no dialog"
    in between. The new dialog (a new element offering a different price) is handled as a new round."""
    site.script_place(
        {"result": "odds_changed", "new_odds": "1,84"}, {"result": "odds_changed", "new_odds": "1,83"}
    )
    result = make_executor().place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.placed_odds == Decimal("1.83")
    assert len(site.state.accept_calls) == 2 and len(site.state.place_calls) == 1
    assert len(site.state.bets) == 1 and site.state.bets[0].odds == "1,83"


def test_second_odds_change_below_the_minimum_is_declined_after_one_accept(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place(
        {"result": "odds_changed", "new_odds": "1,84"}, {"result": "odds_changed", "new_odds": "1,70"}
    )
    result = make_executor().place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert "1.70" in result.message
    assert len(site.state.accept_calls) == 1 and site.state.decline_calls == 1
    assert site.state.bets == []


def test_accepting_odds_changes_can_be_switched_off(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """``CS2BOT_EXECUTOR__MAX_ODDS_CHANGE_ROUNDS=0``: every odds-change dialog is declined, even when
    the new price would be acceptable (the next cycle re-prices the outcome)."""
    site.script_place({"result": "odds_changed", "new_odds": "1,84"})
    executor = make_executor(tuning=replace(FAST_TUNING, max_odds_change_rounds=0))
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert site.state.accept_calls == [] and site.state.decline_calls == 1
    assert site.state.bets == [] and site.state.slip == []


def test_odds_dropping_while_the_dialog_is_open_are_not_accepted(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """The dialog offers 1,82 (fine); while the bot "thinks" the price drops to 1,60 (below the minimum).

    The price must be read again right before the accept click.
    """
    site.script_place({"result": "odds_changed", "new_odds": "1,82"})

    def drop(page: Any) -> bool:
        if page.locator("#oddsChange").count() == 0:
            return False
        site.set_odds("S1001", "1,60")
        page.evaluate(
            "() => { for (const el of document.querySelectorAll("
            "'#slip .betBox .oddValue, #oddsChange .oddValue')) el.textContent = '1,60'; }"
        )
        return True

    executor = _hooked_executor(make_executor, executor_settings, drop)
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert site.state.accept_calls == [], "new odds below the minimum were accepted"
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert "1.60" in result.message
    assert site.state.bets == [] and site.state.decline_calls == 1


def test_lookalike_accept_button_elsewhere_on_the_page_is_never_clicked(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Another "Akceptuj zmiany" button comes FIRST in the page: the bot must click the one inside the
    odds-change dialog, never "the first match"."""
    site.script_place({"result": "odds_changed", "new_odds": "1,82"})
    executor = _hooked_executor(make_executor, executor_settings, _add_lookalike)
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert _lookalike_clicks(executor) == 0
    assert result.status is PlacementStatus.PLACED, result.message
    assert len(site.state.accept_calls) == 1 and len(site.state.bets) == 1


def test_accept_button_outside_the_notice_is_searched_inside_the_bet_slip(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """The notice locator finds only the text element (the buttons are its siblings): the accept
    button is then taken from the bet slip, not from the rest of the page."""
    site.script_place({"result": "odds_changed", "new_odds": "1,82"})
    selectors = _selectors(odds_changed_notice=[{"css": ".betslip_oddsChange .oddValue"}])
    executor = _hooked_executor(make_executor, executor_settings, _add_lookalike, selectors=selectors)
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert _lookalike_clicks(executor) == 0
    assert result.status is PlacementStatus.PLACED, result.message
    assert len(site.state.accept_calls) == 1


def test_ambiguous_accept_button_is_not_clicked(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Neither the notice nor a bet-slip container narrows the search and two buttons say "Akceptuj
    zmiany": the bot does not guess - nothing is accepted, the change is declined."""
    site.script_place({"result": "odds_changed", "new_odds": "1,82"})
    selectors = _selectors(
        odds_changed_notice=[{"css": ".betslip_oddsChange .oddValue"}],
        slip_container=[{"css": "[data-qa='no-such-slip']"}],
    )
    executor = _hooked_executor(make_executor, executor_settings, _add_lookalike, selectors=selectors)
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert _lookalike_clicks(executor) == 0
    assert site.state.accept_calls == []
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert "ambiguous" in result.message
    assert site.state.decline_calls == 1 and site.state.bets == []


def test_production_accept_locator_ignores_cookie_and_settings_buttons(selectors: SelectorSet) -> None:
    """The shipped accept / decline names are anchored: look-alikes with more words do not match."""
    from cs2bot.executor.selectors import text_matcher

    accept = text_matcher(selectors.elements["odds_changed_accept"].locators[0].name or "")
    decline = text_matcher(selectors.elements["odds_changed_decline"].locators[0].name or "")
    assert not isinstance(accept, str) and not isinstance(decline, str)
    for text in ("Akceptuj", "Akceptuj zmiany", "Akceptuj nowy kurs", "Akceptuj i postaw zakład"):
        assert accept.search(text), text
    for text in ("Akceptuj wszystkie", "Akceptuj zmiany kursów", "Zaakceptuj wszystkie pliki cookie"):
        assert not accept.search(text), text
    assert decline.search("Anuluj") and decline.search("Odrzuć")
    assert not decline.search("Odrzuć wszystkie") and not decline.search("Anuluj zakład")


# ------------------------------------------------- rejections read off the page after the click


@pytest.mark.parametrize(
    "notice",
    [
        "Kurs uległ zmianie",  # classified as odds_changed -> would be ODDS_MOVED
        "Cashout niedostępny dla tego zakładu",  # classified as market_suspended -> MARKET_UNAVAILABLE
        "Zakład odrzucony",  # classified as rejected -> REJECTED_BY_BOOKMAKER
    ],
)
def test_bet_taken_behind_an_error_looking_notice_is_uncertain_not_rejected(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory, notice: str
) -> None:
    """The site took the bet (balance debited) but only an error-looking notice is on screen.

    A "definite rejection" here would let the caller place the same bet again with fresh money.
    """
    site.script_place({"result": "ok", "shown_as_error": notice})
    order = make_order()
    executor = make_executor()
    result = executor.place(order)
    assert len(site.state.bets) == 1 and site.state.balance == Decimal("1222.06")  # the bet exists
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert "balance" in result.message and "reconcile" in result.message
    assert executor.place(order).status is PlacementStatus.UNCERTAIN  # blocked, never clicked again
    assert len(site.state.place_calls) == 1


def test_real_rejection_after_the_click_stays_definite_when_the_balance_is_untouched(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place({"result": "error", "message": "Zakład odrzucony przez bukmachera"})
    order = make_order()
    executor = make_executor()
    result = executor.place(order)
    assert result.status is PlacementStatus.REJECTED_BY_BOOKMAKER, result.message
    assert site.state.bets == [] and site.state.balance == Decimal("1234.56")
    assert site.state.slip == []  # cleaned up
    assert executor.place(order).status is PlacementStatus.PLACED  # a definite rejection may be retried


# --------------------------------------------------------------------------- selection click


def test_selection_suspended_at_the_click_is_market_unavailable(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """The outcome is suspended on the server between reading it and clicking it: the slip stays empty
    and shows "Zakład zawieszony". That is the bookmaker's answer, not a broken selector."""

    def suspend(page: Any) -> bool:
        if EVENT_ID not in page.url or page.locator(".marketBox").count() == 0:
            return False
        with site.lock:
            found = site.state.find_selection("S1001")
            assert found is not None
            found[2].suspended = True  # the page is NOT re-rendered: it still shows the open outcome
        return True

    tuning = replace(FAST_TUNING, slip_update_timeout_s=1.0)
    executor = _hooked_executor(make_executor, executor_settings, suspend, tuning=tuning)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE, result.message
    assert "zawieszony" in result.message.lower()
    assert site.state.place_calls == [] and site.state.slip == []


# -------------------------------------------------------------------------------- live event


def test_live_event_is_refused_pre_match_only(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The match already started (the event header shows "Na żywo"): no bet, not even a dry run."""
    site.state.events[EVENT_ID].live = True
    executor = make_executor()
    for order in (make_order(), make_order(dry_run=True)):
        result = executor.place(order)
        assert result.status is PlacementStatus.MARKET_UNAVAILABLE, result.message
        assert "live" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


# ----------------------------------------------------------------------------------- dry run


def test_dry_run_reveals_a_broken_place_button_selector_without_clicking(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The DRY_RUN smoke test must cover the most important selector too: the final button is located
    (never clicked). A selector that does not find it fails the dry run instead of the first real bet."""
    broken = _selectors(slip_place_button=[{"css": "[data-qa='renamed-place-button']"}])
    tuning = replace(FAST_TUNING, element_timeout_s=0.8)
    result = make_executor(selectors=broken, tuning=tuning).place(make_order(dry_run=True))
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "slip_place_button" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


def test_dry_run_with_two_place_buttons_is_ambiguous(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    def add_second_button(page: Any) -> bool:
        if page.locator("#stake").count() == 0:
            return False
        page.evaluate(
            "() => { const b = document.createElement('button'); b.textContent = 'Postaw zakład'; "
            "document.body.appendChild(b); }"
        )
        return True

    executor = _hooked_executor(make_executor, executor_settings, add_second_button)
    result = executor.place(make_order(dry_run=True))
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "slip_place_button" in result.message and "2 visible" in result.message
    assert site.state.place_calls == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_missing_place_button_with_an_underfunded_account_is_insufficient_balance(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
    dry_run: bool,
) -> None:
    """Sites swap "Postaw zakład" for a deposit button when the stake exceeds the balance: that is not a
    broken selector (which would pause the bot) but a plain lack of funds."""
    site.state.balance = Decimal("5.00")

    def swap_button(page: Any) -> bool:
        if page.locator("#stake").count() != 1:
            return False
        # as soon as the stake is typed the site replaces the button (no pause of the bot in between)
        page.evaluate(
            "() => document.getElementById('stake').addEventListener('input', (e) => { "
            "if (e.target.value === '12,50') "
            "document.getElementById('placeBet').textContent = 'Wpłać depozyt'; })"
        )
        return True

    tuning = replace(FAST_TUNING, element_timeout_s=0.8)
    executor = _hooked_executor(make_executor, executor_settings, swap_button, tuning=tuning)
    result = executor.place(make_order(dry_run=dry_run))
    assert result.status is PlacementStatus.INSUFFICIENT_BALANCE, result.message
    assert "exceeds the balance" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


def test_dry_run_message_says_the_button_was_located_not_clicked(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(make_order(dry_run=True))
    assert result.status is PlacementStatus.DRY_RUN_OK, result.message
    assert "located" in result.message and "NOT clicked" in result.message
    assert site.state.place_calls == [] and site.state.accept_calls == []


# ------------------------------------------------------------------------------- bet history


def test_bet_history_returns_a_bet_placed_later_in_the_displayed_minute(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """ "Moje zakłady" shows 18:30 for a bet placed at 18:30:40. Reconciling an UNCERTAIN attempt made
    at 18:30:20 asks for bets since 18:30:20: the row must not be filtered out as "older"."""
    from datetime import timedelta

    executor = make_executor()
    placed = executor.place(make_order())
    assert placed.status is PlacementStatus.PLACED
    shown_minute = site.state.bets[0].placed_at  # what the history displays (seconds are not shown)
    for seconds in (1, 30, 59):
        records = executor.fetch_bet_history(shown_minute + timedelta(seconds=seconds))
        assert [r.external_bet_id for r in records] == ["BC1001"], seconds
    assert executor.fetch_bet_history(shown_minute + timedelta(seconds=61)) == []


# ---------------------------------------------------------------------------- quick-bet mode


@pytest.mark.parametrize("dry_run", [True, False])
def test_bet_placed_by_the_selection_click_itself_is_uncertain_and_loud(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
    dry_run: bool,
) -> None:
    """A "quick bet" account mode places a bet as soon as an outcome is clicked. The bot cannot prevent
    it, but it must never call that a harmless selector problem (or a successful dry run)."""
    site.state.quick_bet_stake = Decimal("10.00")
    tuning = replace(FAST_TUNING, slip_update_timeout_s=1.0)
    executor = make_executor(tuning=tuning)
    order = make_order(dry_run=dry_run)
    result = executor.place(order)
    assert len(site.state.bets) == 1 and site.state.place_calls == []  # the mock took a bet on the click
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert "quick-bet" in result.message and "reconcile" in result.message
    real = order.model_copy(update={"dry_run": False})
    if not dry_run:  # a real order with that key is never sent again ...
        again = executor.place(real)
    else:  # ... also after a restart: the journal remembers what the dry run caused
        executor.close()
        again = make_executor(tuning=tuning).place(real)
    assert again.status is PlacementStatus.UNCERTAIN and "refusing to place twice" in again.message
    assert len(site.state.bets) == 1


def test_event_that_starts_while_the_bot_is_working_is_not_bet(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Kick-off passes during the human-like pauses: the check right before the final click stops it."""
    from datetime import datetime, timedelta

    from cs2bot.domain.betting import BetOrder
    from cs2bot.utils.time import utcnow

    class TimedOrder(BetOrder):
        event_starts_at: datetime | None = None

    holder: dict[str, Any] = {}

    def kick_off(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        holder["order"].event_starts_at = utcnow() - timedelta(seconds=1)  # the match has just started
        return True

    order = TimedOrder(**make_order().model_dump(), event_starts_at=utcnow() + timedelta(minutes=30))
    holder["order"] = order
    executor = _hooked_executor(make_executor, executor_settings, kick_off)
    result = executor.place(order)
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE, result.message
    assert "started" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


# ----------------------------------------------------------------- reads on a re-rendering page


def test_receipt_that_vanishes_between_found_and_read_does_not_end_the_placement(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page re-renders while the bot polls it: an element found a moment ago can be gone when its
    text is read. After the final click that must mean "look again", never UNCERTAIN for a bet that
    was confirmed."""
    from cs2bot.executor import betclic

    real = betclic._soft_text
    state = {"hidden": 0}

    def flaky(locator: Any, timeout_ms: float = 750) -> str | None:
        text = real(locator, timeout_ms)
        if text and "Zakład przyjęty" in text and state["hidden"] < 2:
            state["hidden"] += 1
            return None  # as if the element had been replaced by a re-render before the read
        return text

    monkeypatch.setattr(betclic, "_soft_text", flaky)
    result = make_executor().place(make_order())
    assert state["hidden"] == 2
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.external_bet_id == "BC1001" and len(site.state.place_calls) == 1


def test_soft_text_returns_none_for_an_element_that_is_gone() -> None:
    from playwright.sync_api import Error as PlaywrightError

    from cs2bot.executor.betclic import _soft_text

    class Gone:
        def inner_text(self, timeout: float) -> str:
            raise PlaywrightError("Timeout 750ms exceeded.")

    class Present:
        def inner_text(self, timeout: float) -> str:
            return "  Kurs uległ zmianie \n"

    gone: Any = Gone()
    present: Any = Present()
    assert _soft_text(gone) is None
    assert _soft_text(present) == "Kurs uległ zmianie"
