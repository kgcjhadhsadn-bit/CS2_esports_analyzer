"""BetclicExecutor end to end against the local mock Betclic site (headless Chromium, temp profile).

Every click on "Postaw zakład" reaches the mock server (``site.state.place_calls``), so the tests can
prove e.g. that a dry run never clicks the final button and that UNCERTAIN results are never retried.
"""

from __future__ import annotations

import logging
import re
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cs2bot.config import Settings
from cs2bot.domain.betting import PlacementStatus
from cs2bot.executor.selectors import SelectorSet, load_selectors
from tests.executor.conftest import ExecutorFactory, OrderFactory, requires_chromium
from tests.executor.mock_betclic import EVENT_ID, OTHER_EVENT_ID, MockBetclicServer

pytestmark = [pytest.mark.browser, requires_chromium]


def _shot(result_path: str | None, status: str, bet_id: int, key: str) -> Path:
    assert result_path is not None, "every attempt must be screenshotted"
    path = Path(result_path)
    assert path.is_file() and path.stat().st_size > 0
    assert f"_bet{bet_id}_" in path.name and path.name.endswith(f"_{status}.png")
    assert re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-.") in path.name
    return path


def _modified_selectors(**element_locators: list[dict[str, Any]]) -> SelectorSet:
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    for key, locators in element_locators.items():
        data["elements"][key]["locators"] = locators
    return SelectorSet.model_validate(data)


# --------------------------------------------------------------------------------- dry run


def test_dry_run_never_clicks_the_final_button(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    order = make_order(dry_run=True)
    result = make_executor().place(order)
    assert result.status is PlacementStatus.DRY_RUN_OK, result.message
    assert result.placed_odds == Decimal("1.85")
    assert "NOT clicked" in result.message
    assert site.state.place_calls == [] and site.state.accept_calls == []
    assert site.state.slip == []  # the slip is cleared afterwards
    assert site.state.balance == Decimal("1234.56")
    _shot(result.screenshot_path, "dry_run_ok", order.bet_id, order.idempotency_key)


def test_global_dry_run_setting_wins_over_the_order(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    settings = executor_settings.model_copy(
        update={"executor": executor_settings.executor.model_copy(update={"dry_run": True})}
    )
    result = make_executor(settings=settings).place(make_order(dry_run=False))
    assert result.status is PlacementStatus.DRY_RUN_OK
    assert site.state.place_calls == []


# ------------------------------------------------------------------------------ placement


def test_successful_placement(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    order = make_order()
    result = make_executor().place(order)
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.external_bet_id == "BC1001"
    assert result.placed_odds == Decimal("1.85")
    assert result.balance_after == Decimal("1222.06")
    assert result.at.tzinfo is not None
    # exactly one click, with the stake typed Polish-style and only our selection in the slip
    assert site.state.place_calls == [{"stake_text": "12,50", "accept": False, "slip": ["S1001"]}]
    _shot(result.screenshot_path, "placed", order.bet_id, order.idempotency_key)


def test_stake_is_typed_polish_style_and_verified(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(make_order(stake=Decimal("7.5")))
    assert result.status is PlacementStatus.PLACED
    assert site.state.place_calls[0]["stake_text"] == "7,50"
    assert site.state.balance == Decimal("1227.06")


def test_handicap_and_decimal_comma_names_match(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    executor = make_executor()
    result = executor.place(
        make_order(
            market_raw_name="Handicap map",
            selection_raw_name="Natus Vincere (-1.5)",
            requested_odds=Decimal("2.75"),
            min_acceptable_odds=Decimal("2.70"),
            dry_run=True,
        )
    )
    assert result.status is PlacementStatus.DRY_RUN_OK, result.message
    assert result.placed_odds == Decimal("2.75")


def test_market_only_listed_under_all_markets_tab(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(
        make_order(
            market_raw_name="Liczba map",
            selection_raw_name="Poniżej 2.5",
            requested_odds=Decimal("1.70"),
            min_acceptable_odds=Decimal("1.65"),
        )
    )
    assert result.status is PlacementStatus.PLACED, result.message
    assert site.state.place_calls[0]["slip"] == ["S1032"]


def test_event_located_by_external_id_without_url(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(
        make_order(
            event_url=None,
            external_event_id=OTHER_EVENT_ID,
            selection_raw_name="FaZe",
            requested_odds=Decimal("1.70"),
            min_acceptable_odds=Decimal("1.60"),
        )
    )
    assert result.status is PlacementStatus.PLACED, result.message
    assert site.state.place_calls[0]["slip"] == ["S2001"]


def test_unknown_event_id_is_market_unavailable(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(make_order(event_url=None, external_event_id="999999"))
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE
    assert "not found on the CS2 listing" in result.message
    assert site.state.place_calls == []


def test_removed_event_page_is_market_unavailable(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(make_order(event_url="/e-sport-s102/iem-c1/old-match-m111"))
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE
    assert site.state.place_calls == []


def test_foreign_host_is_refused(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(
        make_order(event_url="https://evil.example.com/e-sport-s102/x-m3002201001")
    )
    assert result.status is PlacementStatus.FAILED
    assert "non-Betclic URL" in result.message
    assert site.state.place_calls == []


def test_stale_slip_is_cleared_before_adding_the_selection(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.slip = ["S2001", "S1012"]  # left over from a manual session
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.PLACED, result.message
    assert site.state.place_calls[0]["slip"] == ["S1001"]


def test_stale_slip_cleared_one_by_one_without_clear_all_button(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.slip = ["S2001", "S1012"]
    selectors = _modified_selectors(slip_clear_button=[{"css": ".no-clear-all"}, {"css": ".still-nothing"}])
    result = make_executor(selectors=selectors).place(make_order(dry_run=True))
    assert result.status is PlacementStatus.DRY_RUN_OK, result.message
    assert site.state.slip == []


# ------------------------------------------------------------------------------ odds moves


def test_odds_moved_before_the_click(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.set_odds("S1001", "1,70")
    order = make_order(min_acceptable_odds=Decimal("1.80"))
    result = make_executor().place(order)
    assert result.status is PlacementStatus.ODDS_MOVED
    assert "1.70" in result.message and "1.80" in result.message
    assert site.state.place_calls == [] and site.state.slip == []
    _shot(result.screenshot_path, "odds_moved", order.bet_id, order.idempotency_key)


def test_odds_change_dialog_accepted_when_still_acceptable(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place({"result": "odds_changed", "new_odds": "1,82"})
    result = make_executor().place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.placed_odds == Decimal("1.82")
    assert len(site.state.place_calls) == 1
    assert len(site.state.accept_calls) == 1 and site.state.accept_calls[0]["accept"] is True
    assert site.state.decline_calls == 0


def test_odds_change_dialog_declined_below_minimum(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place({"result": "odds_changed", "new_odds": "1,60"})
    order = make_order(min_acceptable_odds=Decimal("1.80"))
    result = make_executor().place(order)
    assert result.status is PlacementStatus.ODDS_MOVED
    assert "1.60" in result.message and "declined" in result.message
    assert len(site.state.place_calls) == 1 and site.state.accept_calls == []
    assert site.state.decline_calls == 1
    assert site.state.slip == [] and site.state.bets == []
    _shot(result.screenshot_path, "odds_moved", order.bet_id, order.idempotency_key)


def test_repeated_odds_changes_are_declined(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    from dataclasses import replace

    from tests.executor.conftest import FAST_TUNING

    site.script_place(
        {"result": "odds_changed", "new_odds": "1,84"}, {"result": "odds_changed", "new_odds": "1,83"}
    )
    result = make_executor(tuning=replace(FAST_TUNING, max_odds_change_rounds=1)).place(make_order())
    assert result.status is PlacementStatus.ODDS_MOVED
    assert "2 times" in result.message
    assert len(site.state.accept_calls) == 1 and site.state.decline_calls == 1


def test_acknowledge_mode_dialog_reclicks_place_once(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.accept_mode = "acknowledges"
    site.script_place({"result": "odds_changed", "new_odds": "1,81"})
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    data["behaviour"]["odds_change_accept_places_bet"] = False
    result = make_executor(selectors=SelectorSet.model_validate(data)).place(make_order())
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.placed_odds == Decimal("1.81")
    assert len(site.state.place_calls) == 2  # original click + one re-click after acknowledging
    assert site.state.accept_calls == [{"slip": ["S1001"], "mode": "acknowledge"}]


def test_acknowledge_site_with_default_behaviour_is_uncertain_never_double(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.accept_mode = "acknowledges"
    site.script_place({"result": "odds_changed", "new_odds": "1,81"})
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.UNCERTAIN
    assert len(site.state.place_calls) == 1 and site.state.bets == []


# --------------------------------------------------------------------- missing / ambiguous


def test_market_missing(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(make_order(market_raw_name="Zwycięzca mapy 3"))
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE
    assert "Zwycięzca mapy 3" in result.message and "Zwycięzca meczu" in result.message  # lists markets seen
    assert site.state.place_calls == [] and site.state.slip == []


def test_selection_missing(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(make_order(selection_raw_name="FaZe"))
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE
    assert "selections seen" in result.message
    assert site.state.place_calls == []


def test_ambiguous_selection_is_a_selector_error(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    order = make_order(market_raw_name="Zwycięzca mapy 2", selection_raw_name="Vitality")
    result = make_executor().place(order)
    assert result.status is PlacementStatus.SELECTOR_ERROR
    assert "unambiguously" in result.message
    assert site.state.place_calls == [] and site.state.slip == []
    _shot(result.screenshot_path, "selector_error", order.bet_id, order.idempotency_key)


def test_suspended_selection_is_market_unavailable(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    result = make_executor().place(
        make_order(
            market_raw_name="Dokładny wynik",
            selection_raw_name="2:0",
            requested_odds=Decimal("3.20"),
            min_acceptable_odds=Decimal("3.00"),
        )
    )
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE
    assert "suspended" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


def test_broken_selectors_fail_safe_and_loud(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    selectors = _modified_selectors(
        market_container=[{"css": ".renamedMarketBox"}, {"css": "[data-qa='gone']"}]
    )
    order = make_order()
    result = make_executor(selectors=selectors).place(order)
    assert result.status is PlacementStatus.SELECTOR_ERROR
    assert "market_container" in result.message
    shot = _shot(result.screenshot_path, "selector_error", order.bet_id, order.idempotency_key)
    assert shot.with_suffix(".html").is_file()  # page HTML saved for fixing the selectors
    assert site.state.place_calls == []


# ------------------------------------------------------------------- bookmaker rejections


def test_insufficient_balance(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.balance = Decimal("5.00")
    result = make_executor().place(make_order(stake=Decimal("12.50")))
    assert result.status is PlacementStatus.INSUFFICIENT_BALANCE
    assert "Niewystarczające środki" in result.message
    assert len(site.state.place_calls) == 1 and site.state.bets == []
    assert site.state.slip == []


def test_market_suspended_at_placement(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place({"result": "error", "message": "Zakład zawieszony. Rynek jest chwilowo niedostępny."})
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.MARKET_UNAVAILABLE
    assert len(site.state.place_calls) == 1


def test_stake_limit_detected_before_the_click(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.max_stake = Decimal("10.00")
    result = make_executor().place(make_order(stake=Decimal("12.50")))
    assert result.status is PlacementStatus.REJECTED_BY_BOOKMAKER
    assert "Maksymalna stawka" in result.message
    assert site.state.place_calls == []


def test_unknown_message_after_click_is_uncertain(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place({"result": "error", "message": "Coś poszło nie tak (kod 7)"})
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.UNCERTAIN
    assert "reconcile" in result.message


# ----------------------------------------------------------------- login / halt / uncertain


def test_login_required(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.logged_in = False
    order = make_order()
    result = make_executor().place(order)
    assert result.status is PlacementStatus.LOGIN_REQUIRED
    assert site.state.place_calls == [] and site.state.slip == []
    _shot(result.screenshot_path, "login_required", order.bet_id, order.idempotency_key)


def test_halt_switched_on_between_steps(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    answers = iter([(False, None), (True, "kill switch is on")])
    calls: list[int] = []

    def halt_check() -> tuple[bool, str | None]:
        calls.append(1)
        return next(answers)

    order = make_order()
    result = make_executor(halt_check=halt_check).place(order)
    assert result.status is PlacementStatus.KILL_SWITCH
    assert "right before confirming" in result.message
    assert len(calls) == 2
    assert site.state.place_calls == [] and site.state.slip == []
    _shot(result.screenshot_path, "kill_switch", order.bet_id, order.idempotency_key)


def test_no_confirmation_is_uncertain_and_never_retried(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.script_place({"result": "hang"})
    executor = make_executor()
    order = make_order()
    result = executor.place(order)
    assert result.status is PlacementStatus.UNCERTAIN
    assert "do NOT retry" in result.message
    assert len(site.state.place_calls) == 1
    _shot(result.screenshot_path, "uncertain", order.bet_id, order.idempotency_key)
    again = executor.place(order)  # same idempotency key: refused without touching the page
    assert again.status is PlacementStatus.UNCERTAIN and "refusing to place twice" in again.message
    assert len(site.state.place_calls) == 1


def test_placed_bet_is_never_placed_twice(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    executor = make_executor()
    order = make_order()
    assert executor.place(order).status is PlacementStatus.PLACED
    second = executor.place(order)
    assert second.status is PlacementStatus.UNCERTAIN
    assert len(site.state.place_calls) == 1 and len(site.state.bets) == 1
    # a different idempotency key is a different bet
    third = executor.place(make_order())
    assert third.status is PlacementStatus.PLACED and third.external_bet_id == "BC1002"


def test_leftover_receipt_is_closed_and_never_reported_as_the_new_bet(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.last_receipt = "BC0999"  # e.g. the bot crashed right after an earlier bet
    result = make_executor().place(make_order())
    assert result.status is PlacementStatus.PLACED
    assert result.external_bet_id == "BC1001"  # not the old receipt's id
    assert site.state.last_receipt is None  # the new receipt was closed afterwards


def test_receipt_that_cannot_be_closed_blocks_the_next_click(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    selectors = _modified_selectors(
        receipt_close=[{"css": ".receipt-close-renamed"}]
    )  # first receipt stays open
    executor = make_executor(selectors=selectors)
    assert executor.place(make_order()).status is PlacementStatus.PLACED
    assert site.state.last_receipt == "BC1001"
    # the old receipt cannot be closed -> refuse to click (it could confirm the wrong bet)
    blocked = executor.place(make_order())
    assert blocked.status is PlacementStatus.SELECTOR_ERROR
    assert "receipt from an earlier bet" in blocked.message
    assert len(site.state.place_calls) == 1


# ------------------------------------------------------------------- account operations


def test_balance_and_login_state(site: MockBetclicServer, make_executor: ExecutorFactory) -> None:
    executor = make_executor()
    assert executor.get_balance() == Decimal("1234.56")
    assert executor.is_logged_in() is True
    site.state.logged_in = False
    assert executor.is_logged_in() is False
    assert executor.get_balance() is None


def test_health_check(site: MockBetclicServer, make_executor: ExecutorFactory) -> None:
    executor = make_executor()
    health = executor.health_check()
    assert health.ok is True, health.message
    assert health.logged_in is True and health.balance == Decimal("1234.56")
    assert all(health.selectors_ok.values()) and "market_container" in health.selectors_ok
    site.state.logged_in = False
    bad = executor.health_check()
    assert bad.ok is False and bad.logged_in is False and "not logged in" in bad.message


def test_fetch_bet_history_with_load_more_and_since_filter(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    from datetime import timedelta

    from cs2bot.executor.base import BookmakerBetStatus
    from cs2bot.utils.time import utcnow
    from tests.executor.mock_betclic import history_bet

    for bet in [
        history_bet("BC0900", days_ago=10, status="Wygrany", payout="18.50"),
        history_bet("BC0901", days_ago=6, status="Przegrany"),
        history_bet("BC0902", days_ago=5, status="Zwrot", payout="10.00"),
        history_bet("BC0903", days_ago=4, status="Cashout", payout="7.30"),
        history_bet("BC0904", days_ago=3, status="Wygrany", odds="2,10", stake="20.00", payout="42.00"),
        history_bet("BC0905", days_ago=2, status="W grze"),
    ]:
        site.add_bet(bet)
    executor = make_executor()
    placed = executor.place(make_order())
    assert placed.status is PlacementStatus.PLACED
    records = executor.fetch_bet_history(utcnow() - timedelta(days=7))  # needs "Pokaż więcej" (page size 5)
    by_id = {r.external_bet_id: r for r in records}
    assert set(by_id) == {"BC1001", "BC0901", "BC0902", "BC0903", "BC0904", "BC0905"}  # BC0900 is older
    new = by_id["BC1001"]
    assert new.status is BookmakerBetStatus.OPEN and new.payout is None
    assert new.odds == Decimal("1.85") and new.stake == Decimal("12.50")
    assert new.selection_name == "Natus Vincere" and new.market_name == "Zwycięzca meczu"
    assert new.placed_at is not None and abs((utcnow() - new.placed_at).total_seconds()) < 180
    assert by_id["BC0901"].status is BookmakerBetStatus.LOST and by_id["BC0901"].payout == Decimal("0")
    assert by_id["BC0902"].status is BookmakerBetStatus.VOID and by_id["BC0902"].payout == Decimal("10.00")
    assert by_id["BC0903"].status is BookmakerBetStatus.CASHOUT and by_id["BC0903"].payout == Decimal("7.30")
    won = by_id["BC0904"]
    assert (
        won.status is BookmakerBetStatus.WON
        and won.payout == Decimal("42.00")
        and won.odds == Decimal("2.10")
    )


def test_fetch_bet_history_requires_login(site: MockBetclicServer, make_executor: ExecutorFactory) -> None:
    from datetime import UTC, datetime

    from cs2bot.executor.base import LoginRequiredError

    site.state.logged_in = False
    with pytest.raises(LoginRequiredError):
        make_executor().fetch_bet_history(datetime(2026, 1, 1, tzinfo=UTC))


def test_empty_history(site: MockBetclicServer, make_executor: ExecutorFactory) -> None:
    from datetime import UTC, datetime

    assert make_executor().fetch_bet_history(datetime(2026, 1, 1, tzinfo=UTC)) == []


def test_open_login_window_prefills_env_credentials_and_waits_for_the_owner(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    executor_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from pydantic import SecretStr

    site.state.logged_in = False
    betclic = executor_settings.betclic.model_copy(
        update={
            "credentials_source": "env",
            "username": SecretStr("owner@example.com"),
            "password": SecretStr("S3cret-Haslo!"),
        }
    )
    settings = executor_settings.model_copy(update={"betclic": betclic})

    def owner_finishes_login() -> None:  # the owner types the SMS code and submits by hand
        with site.lock:
            site.state.logged_in = True

    timer = threading.Timer(1.5, owner_finishes_login)
    timer.start()
    try:
        with caplog.at_level(logging.DEBUG):
            ok = make_executor(settings=settings).open_login_window(timeout_s=15)
    finally:
        timer.cancel()
    assert ok is True
    assert site.state.login_form == {"username": "owner@example.com", "password": "S3cret-Haslo!"}
    assert site.state.login_submits == 0  # pre-filled, never submitted, no 2FA automation
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "S3cret-Haslo!" not in logs and "owner@example.com" not in logs
    assert "pre-filled" in logs


def test_open_login_window_with_keyring(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    executor_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    fake_keyring = types.ModuleType("keyring")
    lookups: list[tuple[str, str | None]] = []

    def get_credential(service: str, username: str | None) -> Any:
        lookups.append((service, username))
        return types.SimpleNamespace(username="kr-user", password="kr-pass")

    fake_keyring.get_credential = get_credential  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    site.state.logged_in = False
    betclic = executor_settings.betclic.model_copy(update={"credentials_source": "keyring"})
    settings = executor_settings.model_copy(update={"betclic": betclic})
    assert (
        make_executor(settings=settings).open_login_window(timeout_s=1.0) is False
    )  # nobody finished the login
    assert lookups == [("cs2bot-betclic", None)]
    assert site.state.login_form == {"username": "kr-user", "password": "kr-pass"}
    assert site.state.login_submits == 0


def test_open_login_window_when_already_logged_in(
    site: MockBetclicServer, make_executor: ExecutorFactory
) -> None:
    assert make_executor().open_login_window(timeout_s=5) is True
    assert site.state.login_form == {}


# ----------------------------------------------------------------------- browser / threads


def test_relaunch_after_the_owner_closes_the_window(
    site: MockBetclicServer, make_executor: ExecutorFactory
) -> None:
    executor = make_executor()
    assert executor.is_logged_in()
    context = executor._session._context
    assert context is not None
    context.close()  # the owner closed the browser window
    assert executor.is_logged_in()  # relaunched with the same profile


def test_second_executor_on_the_same_profile_is_refused(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    first = make_executor()
    assert first.is_logged_in()
    second = make_executor()
    result = second.place(make_order())
    assert result.status is PlacementStatus.FAILED and "in use" in result.message
    assert site.state.place_calls == []
    first.close()
    assert second.place(make_order(dry_run=True)).status is PlacementStatus.DRY_RUN_OK


def test_wrong_thread_is_rejected_and_thread_bound_wrapper_works(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    from cs2bot.executor import BetclicExecutor, ExecutorThreadError, ThreadBoundExecutor
    from tests.executor.conftest import FAST_TUNING

    executor = make_executor()
    assert executor.is_logged_in()
    errors: list[BaseException] = []
    worker = threading.Thread(target=lambda: errors.append(_catch(executor.is_logged_in)))
    worker.start()
    worker.join()
    assert isinstance(errors[0], ExecutorThreadError)
    executor.close()

    wrapper = ThreadBoundExecutor(
        lambda: BetclicExecutor(
            executor_settings, halt_check=lambda: (False, None), sleep=lambda _s: None, tuning=FAST_TUNING
        )
    )
    try:
        results: list[Any] = []
        t = threading.Thread(target=lambda: results.append(wrapper.place(make_order(dry_run=True))))
        t.start()
        t.join()
        assert results[0].status is PlacementStatus.DRY_RUN_OK
        assert wrapper.get_balance() == Decimal("1234.56")  # from the main thread, same browser
    finally:
        wrapper.close()


def _catch(fn: Any) -> BaseException | None:
    try:
        fn()
    except BaseException as exc:
        return exc
    return None


def test_event_constant_matches_mock() -> None:
    assert EVENT_ID == "3002201001"


def test_definite_rejection_after_the_click_allows_a_later_retry(
    site: MockBetclicServer, make_executor: ExecutorFactory, make_order: OrderFactory
) -> None:
    site.state.balance = Decimal("5.00")
    executor = make_executor()
    order = make_order()
    assert executor.place(order).status is PlacementStatus.INSUFFICIENT_BALANCE
    site.state.balance = Decimal("100.00")  # the owner topped up
    retry = executor.place(order)  # same idempotency key: nothing was placed, so retrying is allowed
    assert retry.status is PlacementStatus.PLACED, retry.message
    assert len(site.state.place_calls) == 2 and len(site.state.bets) == 1


def test_kill_switch_flipped_during_a_pause_stops_before_the_final_click(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    flag = {"halted": False, "checks": 0}

    def halt_check() -> tuple[bool, str | None]:
        flag["checks"] += 1
        return (True, "kill switch is on") if flag["halted"] else (False, None)

    def sleep(_seconds: float) -> None:  # the dashboard flips the kill switch while the bot "thinks"
        if flag["checks"] >= 1:
            flag["halted"] = True

    settings = executor_settings.model_copy(
        update={
            "executor": executor_settings.executor.model_copy(
                update={"action_delay_min_s": 0.001, "action_delay_max_s": 0.002}
            )
        }
    )
    result = make_executor(settings=settings, halt_check=halt_check, sleep=sleep).place(make_order())
    assert result.status is PlacementStatus.KILL_SWITCH
    assert site.state.place_calls == [] and site.state.slip == []


def test_health_check_reports_configuration_problems(
    site: MockBetclicServer, make_executor: ExecutorFactory
) -> None:
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    data["urls"]["cs2_listing"] = "https://elsewhere.example.com/e-sport"
    health = make_executor(selectors=SelectorSet.model_validate(data)).health_check()
    assert health.ok is False and "configuration" in health.message


def _slip_odds_change_executor(
    site: MockBetclicServer, make_executor: ExecutorFactory, executor_settings: Settings, new_odds: str
) -> Any:
    """Executor whose first pause after the selection reached the slip changes the odds live."""
    import time as _time

    triggered = {"done": False}

    def sleep(_seconds: float) -> None:
        if not triggered["done"] and site.state.slip == ["S1001"]:
            triggered["done"] = True
            site.set_odds("S1001", new_odds)
            _time.sleep(0.8)  # let the page's live update arrive

    settings = executor_settings.model_copy(
        update={
            "executor": executor_settings.executor.model_copy(
                update={"action_delay_min_s": 0.001, "action_delay_max_s": 0.002}
            )
        }
    )
    return make_executor(settings=settings, sleep=sleep)


def test_silent_odds_drop_in_the_slip_stops_before_the_click(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    executor = _slip_odds_change_executor(site, make_executor, executor_settings, "1,70")
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.ODDS_MOVED, result.message
    assert "before confirming" in result.message
    assert site.state.place_calls == [] and site.state.slip == []


def test_silent_odds_rise_in_the_slip_is_reported_as_placed_odds(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    make_order: OrderFactory,
    executor_settings: Settings,
) -> None:
    executor = _slip_odds_change_executor(site, make_executor, executor_settings, "1,90")
    result = executor.place(make_order(min_acceptable_odds=Decimal("1.80")))
    assert result.status is PlacementStatus.PLACED, result.message
    assert result.placed_odds == Decimal("1.90")
    assert site.state.bets[0].odds == "1,90"
