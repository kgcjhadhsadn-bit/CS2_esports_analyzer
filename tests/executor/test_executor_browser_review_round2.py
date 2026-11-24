"""Browser regressions of review round 2 (mock site on 127.0.0.1)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from cs2bot.config import Settings
from cs2bot.domain.betting import BookmakerBetStatus, PlacementStatus
from tests.executor.conftest import ExecutorFactory, OrderFactory, requires_chromium
from tests.executor.mock_betclic import MockBetclicServer
from tests.executor.test_executor_browser_safety import _hooked_executor, _selectors_with

pytestmark = [pytest.mark.browser, requires_chromium]


def _stake_typed(page: Any) -> bool:
    field = page.locator("#stake")
    return bool(field.count() and field.input_value() == "12,50")


# A receipt container that is rendered (visible, with a spinner but no text yet) as soon as the request
# is sent, and replaced by the bookmaker's real answer when it arrives.
_RECEIPT_PLACEHOLDER_JS = """
(delayMs) => {
  window.placeBet = async (accept) => {
    ui.pending = true;
    renderSlip(ui.data.slip);
    const box = document.createElement('div');
    box.className = 'betslip_receipt';
    box.style.minHeight = '30px';
    box.style.border = '1px solid #ccc';
    const spinner = document.createElement('div');
    spinner.className = 'spinner';
    box.appendChild(spinner);
    document.getElementById('slip').appendChild(box);
    const r = await api('/api/place', { stake_text: ui.stakeText, accept: accept });
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    box.remove();
    applyPlaceResponse(r);
  };
}
"""


def test_empty_receipt_placeholder_is_not_a_confirmation(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """A receipt box that is displayed but still empty (a spinner) confirms nothing.

    Before the fix an empty receipt text counted as "no error phrase" = confirmed: the bet below was
    reported PLACED (the ledger would debit a stake) although Betclic rejected it a moment later.
    """
    site.script_place({"result": "error", "message": "Zakład odrzucony przez bukmachera"})

    def install(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(_RECEIPT_PLACEHOLDER_JS, 1200)
        return True

    executor = _hooked_executor(make_executor, executor_settings, install)
    result = executor.place(make_order())
    assert len(site.state.place_calls) == 1
    assert site.state.bets == []
    assert result.status is PlacementStatus.REJECTED_BY_BOOKMAKER, result.message


def test_receipt_placeholder_followed_by_the_real_receipt_is_placed(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    def install(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(_RECEIPT_PLACEHOLDER_JS, 800)
        return True

    executor = _hooked_executor(make_executor, executor_settings, install)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.external_bet_id == site.state.bets[0].bet_id


def _render_receipt_after_place_js() -> str:
    """``placeBet`` replaced: the real request is sent (the mock takes the bet), then a receipt box
    with the given inner HTML is rendered instead of the mock's own receipt."""
    return """
(html) => {
  window.placeBet = async (accept) => {
    const r = await api('/api/place', { stake_text: ui.stakeText, accept: accept });
    ui.data.slip = r.slip || [];
    ui.stakeText = '';
    ui.data.balance_text = r.balance_text || ui.data.balance_text;
    renderHeader();
    renderSlip(ui.data.slip);
    const box = document.createElement('div');
    box.className = 'betslip_receipt';
    box.innerHTML = html;
    document.getElementById('slip').appendChild(box);
  };
}
"""


def test_receipt_without_confirmation_phrase_or_bet_id_is_uncertain(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """A receipt box with an unknown text and no bet id proves nothing: UNCERTAIN (reconcile), never
    PLACED on a guess - and never a definite "no bet" either."""

    def install(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(_render_receipt_after_place_js(), "<p>Dziękujemy!</p>")
        return True

    executor = _hooked_executor(make_executor, executor_settings, install)
    order = make_order()
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert "Dziękujemy" in result.message
    assert len(site.state.place_calls) == 1 and len(site.state.bets) == 1
    again = executor.place(order)  # the key stays blocked
    assert again.status is PlacementStatus.UNCERTAIN and len(site.state.place_calls) == 1


def test_receipt_with_a_bet_id_but_no_known_phrase_is_placed(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    def install(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        page.evaluate(_render_receipt_after_place_js(), "Gratulacje! Nr kuponu: BC1001")
        return True

    executor = _hooked_executor(make_executor, executor_settings, install)
    result = executor.place(make_order())
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.external_bet_id == "BC1001" == site.state.bets[0].bet_id


# ------------------------------------------------------------------ profile lock and journal


def test_key_clicked_by_another_process_before_the_lock_was_taken_is_refused(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    """The first journal check runs before the profile lock is taken (and before the pacing gap).
    Another process that held the profile meanwhile may have clicked the same key: after taking the
    lock the journal is read again. Before the fix the bet was placed a second time."""
    executor = make_executor()
    order = make_order()
    original = executor._session.ensure_lock

    def other_process_clicked_first() -> None:
        executor._journal.record_click(order.idempotency_key, order.bet_id)  # the other bot's record
        original()

    executor._session.ensure_lock = other_process_clicked_first
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert "refusing to place twice" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


def test_profile_lock_taken_over_before_the_final_click_never_clicks(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Another process broke our lock as stale (e.g. after the PC slept) while the bot was in the middle
    of an attempt: the journal is no longer ours alone, so the final button is not clicked."""
    holder: dict[str, Any] = {}

    def take_over(page: Any) -> bool:
        if not _stake_typed(page):
            return False
        lock = holder["executor"]._session.lock
        info = lock.read_info()
        assert info is not None
        lock.path.write_text(info.to_json().replace(info.token, "f" * 32), encoding="utf-8")
        return True

    executor = _hooked_executor(make_executor, executor_settings, take_over)
    holder["executor"] = executor
    order = make_order()
    result = executor.place(order)
    assert result.status is PlacementStatus.FAILED, result.message
    assert "lock" in result.message
    assert site.state.place_calls == [] and site.state.bets == []
    assert executor._journal.latest(order.idempotency_key) is None  # nothing was journaled either


# ------------------------------------------------------------------ acknowledge-mode re-click


def test_acknowledge_mode_reclick_checks_which_selection_the_slip_holds(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    """Before "Postaw zakład" is clicked a second time (acknowledge-mode dialog) the slip must still
    hold the ordered selection; otherwise no second click (UNCERTAIN: the first click may have bet)."""
    site.state.accept_mode = "acknowledges"
    site.script_place({"result": "odds_changed", "new_odds": "1,81"})

    def swap_after_acknowledge(page: Any) -> bool:
        if not site.state.accept_calls or page.locator("#oddsChange").count():
            return False
        page.evaluate(
            "() => { document.querySelector('.betBox .betBox_contestantName').textContent = 'Vitality'; }"
        )
        return True

    executor = _hooked_executor(
        make_executor,
        executor_settings,
        swap_after_acknowledge,
        selectors=_selectors_with(odds_change_accept_places_bet=False),
    )
    result = executor.place(make_order())
    assert result.status is PlacementStatus.UNCERTAIN, result.message
    assert "Vitality" in result.message
    assert len(site.state.place_calls) == 1  # never re-clicked
    assert site.state.bets == []


# ------------------------------------------------------------------ bet history without a status label


def test_history_row_without_status_label_and_a_cash_out_button_is_unknown(
    site: MockBetclicServer, make_executor: ExecutorFactory
) -> None:
    executor = make_executor()
    page = executor._session.ensure_page()
    page.set_content(
        """
        <div class="myBets_item">
          <span class="myBets_date">22.11.2026 18:30</span> <span>Nr zakładu: BC77001</span>
          <span>W grze</span> <div class="myBets_selection">Natus Vincere</div>
          <div>Kurs: 1,85</div> <div>Stawka: 12,50 zł</div>
          <div>Możliwa wygrana: 20,35 zł</div> <button>Cash out 9,80 zł</button>
        </div>
        <div class="myBets_item">
          <span class="myBets_date">21.11.2026 18:30</span> <span>Nr zakładu: BC77000</span>
          <span>Przegrany</span> <div>Kurs: 1,85</div> <div>Stawka: 10,00 zł</div>
        </div>
        """
    )
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    rows = page.locator(".myBets_item")
    open_row = executor._parse_history_row(rows.nth(0), now)
    assert open_row is not None
    assert open_row.status is BookmakerBetStatus.UNKNOWN  # not CASHOUT (it is still open)
    assert open_row.payout is None  # neither the potential win nor the cash-out offer is a payout
    assert open_row.external_bet_id == "BC77001" and open_row.stake == Decimal("12.50")
    lost_row = executor._parse_history_row(rows.nth(1), now)
    assert lost_row is not None
    assert lost_row.status is BookmakerBetStatus.LOST and lost_row.payout == Decimal("0")


# ------------------------------------------------------------------ login window and shutdown


def test_abort_login_ends_a_waiting_login_window(
    site: MockBetclicServer, make_executor: ExecutorFactory
) -> None:
    site.state.logged_in = False
    executor = make_executor()
    executor.abort_login()  # e.g. ThreadBoundExecutor.close() during the wait
    started = time.monotonic()
    assert executor.open_login_window(timeout_s=60) is False
    assert time.monotonic() - started < 15
