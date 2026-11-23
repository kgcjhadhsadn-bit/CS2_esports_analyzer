"""Browser regressions of review round 1, third pass (mock site on 127.0.0.1).

* an odds-change dialog shown BEFORE the final click is the bookmaker's answer (ODDS_MOVED): the bot
  never clicks "Postaw zakład" while Betclic offers another price, and never reports a broken selector;
* the slip's single selection is re-checked by name right before the final click;
* housekeeping clicks (clear the slip, confirm, close) never hit the final button, even with a wrong
  locator - in a dry run that would place a real bet;
* a transient popup covering the button is dismissed instead of pausing the bot;
* the halt check also runs right before the outcome is clicked (quick-bet accounts bet on that click).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from cs2bot.config import Settings
from cs2bot.domain.betting import PlacementStatus
from cs2bot.executor.selectors import SelectorSet, load_selectors
from tests.executor.conftest import FAST_TUNING, ExecutorFactory, OrderFactory, requires_chromium
from tests.executor.mock_betclic import MockBetclicServer
from tests.executor.test_executor_browser_safety import _hooked_executor

pytestmark = [pytest.mark.browser, requires_chromium]


def _stake_typed(page: Any) -> bool:
    field = page.locator("#stake")
    return bool(field.count() and field.input_value() == "12,50")


def _selectors_with_elements(**elements: list[dict[str, Any]]) -> SelectorSet:
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    for key, locators in elements.items():
        data["elements"][key]["locators"] = locators
    return SelectorSet.model_validate(data)


# ------------------------------------------------------------------ odds change before the final click


@pytest.mark.parametrize("button_enabled", [False, True], ids=["button-disabled", "button-enabled"])
def test_odds_dialog_before_the_final_click_is_odds_moved_and_never_clicked(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
    button_enabled: bool,
) -> None:
    """Betclic offers 1,70 (minimum 1,80) while the slip still reads 1,85.

    Before the fix: with the button enabled the bot clicked "Postaw zakład" anyway (on a real site the
    click may count as accepting 1,70); with the button disabled it reported SELECTOR_ERROR (pausing the
    bot) for what is an ordinary odds change.
    """

    def show_dialog(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(
            "(enabled) => { ui.oddsDialog = {new_odds: '1,70'}; renderSlip(ui.data.slip);"
            " if (enabled) document.getElementById('placeBet').disabled = false; }",
            button_enabled,
        )
        return True

    executor = _hooked_executor(make_executor, executor_settings, show_dialog)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert "odds change before the bet was confirmed" in result.message
    assert site.state.place_calls == [] and site.state.bets == []
    assert site.state.slip == []  # the slip was cleared (nothing pending)


def test_odds_notice_visible_after_typing_the_stake_is_odds_moved_in_a_dry_run(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """A dry run reports the odds change too (the owner's smoke test shows the same answer as a bet)."""
    executor = make_executor()
    original = executor._enter_stake

    def enter_stake_then_dialog(page: Any, stake: Decimal) -> None:
        original(page, stake)  # the dialog shows up right after the stake was typed
        page.evaluate("() => { ui.oddsDialog = {new_odds: '1,90'}; renderSlip(ui.data.slip); }")

    executor._enter_stake = enter_stake_then_dialog  # type: ignore[method-assign]
    result = executor.place(make_order(dry_run=True))
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert site.state.place_calls == []


# ------------------------------------------------------------------ slip re-checked by name


def test_slip_selection_swapped_right_before_the_click_is_refused(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Same count, same odds, another outcome: the count and the odds alone cannot tell."""

    def swap(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(
            "() => { document.querySelector('.betBox .betBox_contestantName').textContent = 'Vitality'; }"
        )
        return True

    executor = _hooked_executor(make_executor, executor_settings, swap)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.SELECTOR_ERROR, result.message
    assert "right before confirming" in result.message and "Vitality" in result.message
    assert site.state.place_calls == []


# ------------------------------------------------------------------ housekeeping clicks


@pytest.mark.parametrize("button_text", ["Postaw zakład", "Potwierdź"], ids=["by-text", "by-identity"])
def test_clearing_the_slip_never_clicks_the_place_button_in_a_dry_run(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
    button_text: str,
) -> None:
    """A wrong 'slip_clear_confirm' locator that hits the final button (e.g. "Potwierdź" inside a bet
    slip rendered as a dialog) must not turn the dry run's final clean-up into a real bet."""
    selectors = _selectors_with_elements(
        slip_place_button=[{"css": "#placeBet"}],
        slip_clear_button=[{"css": ".betslip_title"}],  # a "clear" that clears nothing ...
        slip_clear_confirm=[{"css": "#placeBet"}],  # ... and a "confirm" that is the final button
    )

    def relabel(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate("(text) => { document.getElementById('placeBet').textContent = text; }", button_text)
        return True

    executor = _hooked_executor(
        make_executor,
        executor_settings,
        relabel,
        selectors=selectors,
        tuning=replace(FAST_TUNING, slip_update_timeout_s=0.5),
    )
    result = executor.place(make_order(dry_run=True))
    assert result.status is PlacementStatus.DRY_RUN_OK, result.message
    assert site.state.place_calls == [] and site.state.bets == []
    assert site.state.balance == Decimal("1234.56")


def test_popup_covering_the_place_button_is_dismissed_not_a_selector_error(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    def promo(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(
            "() => { const o = document.createElement('div'); o.className = 'modal';"
            " o.setAttribute('role', 'dialog');"
            " o.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.4)';"
            " const b = document.createElement('button'); b.setAttribute('aria-label', 'Zamknij');"
            " b.textContent = 'x'; b.onclick = () => o.remove(); o.appendChild(b);"
            " document.body.appendChild(o); }"
        )
        return True

    quick = replace(FAST_TUNING, action_timeout_s=1.0, element_timeout_s=1.0)
    executor = _hooked_executor(make_executor, executor_settings, promo, tuning=quick)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.PLACED, result.message
    assert len(site.state.place_calls) == 1


# ------------------------------------------------------------------ halt before the outcome click


def test_halt_right_before_the_outcome_click_leaves_the_slip_empty(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    answers = iter([(False, None), (True, "kill switch is on")])
    result = make_executor(halt_check=lambda: next(answers)).place(make_order())
    assert result.status is PlacementStatus.KILL_SWITCH
    assert "before clicking the outcome" in result.message
    assert site.state.slip == [] and site.state.place_calls == []


# ------------------------------------------------------------------ receipt box carrying a refusal


def test_receipt_box_showing_a_rejection_is_never_reported_as_placed(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """A receipt CONTAINER (``.betslip_receipt``) that shows "Zakład odrzucony" is not a confirmation.

    Before the fix any visible receipt element whose text was not "pending" ended as PLACED: the ledger
    would debit a stake for a bet that does not exist. Inconclusive -> UNCERTAIN (reconciled).
    """
    site.script_place({"result": "hang"})  # the server never answers; the page shows our box instead

    def arm(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(
            "() => document.addEventListener('click', (e) => {"
            " if (!e.target || e.target.id !== 'placeBet') return;"
            " setTimeout(() => { const r = document.createElement('div'); r.className = 'betslip_receipt';"
            " r.textContent = 'Zakład odrzucony przez bukmachera'; document.body.appendChild(r); }, 150);"
            " }, true)"
        )
        return True

    quick = replace(FAST_TUNING, confirmation_timeout_s=1.5)
    executor = _hooked_executor(make_executor, executor_settings, arm, tuning=quick)
    order = make_order()
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert "odrzucony" in result.message
    assert len(site.state.place_calls) == 1
    assert executor.place(order).status is PlacementStatus.UNCERTAIN  # never clicked again
    assert len(site.state.place_calls) == 1


def test_receipt_confirmation_phrase_wins_over_incidental_error_words(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """ "Zakład przyjęty ... kurs uległ zmianie" is a bet taken at the changed odds: PLACED."""
    site.script_place({"result": "ok", "note": "Kurs uległ zmianie."})
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.PLACED, result.message


# ------------------------------------------------------------------ odds-change dialog "old -> new"


def test_odds_dialog_showing_old_and_new_price_is_read_at_the_lower_one(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The dialog reads "1,85 → 1,70" (both prices in .oddValue, the old one first) and the slip still
    shows 1,85. Before the fix the first match (1,85) was taken as the new price and accepted, although
    the bet would go in at 1,70, below the minimum 1,80."""
    site.script_place(
        {"result": "odds_changed", "new_odds": "1,85", "dialog_old_odds": "1,85", "dialog_odds": "1,70"}
    )
    result = make_executor().place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert site.state.accept_calls == []
    assert site.state.bets == []
