"""FakeExecutor: scripted results, recorded calls, bookmaker-like defaults, protocol conformance."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cs2bot.domain.betting import BetOrder, PlacementResult, PlacementStatus
from cs2bot.executor import (
    AccountExecutor,
    BetclicExecutor,
    BetExecutor,
    BookmakerBetRecord,
    BookmakerBetStatus,
    ExecutorClosedError,
    FakeExecutor,
    LoginRequiredError,
    ThreadBoundExecutor,
)
from cs2bot.utils.time import utcnow

_seq = itertools.count(7)


def order(**overrides: object) -> BetOrder:
    """A valid order; every call gets its own idempotency key (as every real bet does)."""
    n = next(_seq)
    data: dict[str, object] = {
        "bet_id": n,
        "idempotency_key": f"k-{n}",
        "external_event_id": "3002201001",
        "market_raw_name": "Zwycięzca meczu",
        "selection_raw_name": "Natus Vincere",
        "requested_odds": Decimal("1.85"),
        "min_acceptable_odds": Decimal("1.80"),
        "stake": Decimal("10.00"),
        "dry_run": False,
    }
    data.update(overrides)
    return BetOrder(**data)  # type: ignore[arg-type]


def test_protocols_are_implemented() -> None:
    fake = FakeExecutor()
    assert isinstance(fake, BetExecutor)
    assert isinstance(fake, AccountExecutor)
    for cls in (BetclicExecutor, ThreadBoundExecutor, FakeExecutor):
        for name in ("place", "get_balance", "health_check", "close", "is_logged_in", "open_login_window"):
            assert callable(getattr(cls, name)), (cls, name)
        assert callable(cls.fetch_bet_history)


def test_default_placement_updates_balance_and_history() -> None:
    fake = FakeExecutor(balance=Decimal("100.00"))
    result = fake.place(order())
    assert result.status is PlacementStatus.PLACED
    assert result.placed_odds == Decimal("1.85")
    assert result.external_bet_id == "FAKE-1"
    assert result.balance_after == Decimal("90.00")
    assert fake.get_balance() == Decimal("90.00")
    assert fake.calls[0] == ("place", fake.orders[0])
    (record,) = fake.fetch_bet_history(utcnow() - timedelta(minutes=1))
    assert record.external_bet_id == "FAKE-1" and record.status is BookmakerBetStatus.OPEN
    assert record.stake == Decimal("10.00") and record.odds == Decimal("1.85")


def test_dry_run_orders_are_not_placed() -> None:
    fake = FakeExecutor(balance=Decimal("100.00"))
    result = fake.place(order(dry_run=True))
    assert result.status is PlacementStatus.DRY_RUN_OK and result.external_bet_id is None
    assert fake.balance == Decimal("100.00") and fake.history == []


def test_script_items_status_result_and_callable() -> None:
    explicit = PlacementResult(status=PlacementStatus.UNCERTAIN, message="x", at=utcnow())
    fake = FakeExecutor(
        [
            PlacementStatus.ODDS_MOVED,
            explicit,
            lambda o: PlacementResult(status=PlacementStatus.PLACED, placed_odds=Decimal("2.0"), at=utcnow()),
        ],
        balance=Decimal("50.00"),
    )
    assert fake.place(order()).status is PlacementStatus.ODDS_MOVED
    assert fake.place(order()) is explicit
    third = fake.place(order(stake=Decimal("5.00")))
    assert third.status is PlacementStatus.PLACED and fake.balance == Decimal("45.00")
    fake.queue(PlacementStatus.SELECTOR_ERROR)
    assert fake.place(order()).status is PlacementStatus.SELECTOR_ERROR
    assert fake.place(order()).status is PlacementStatus.PLACED  # script exhausted -> default
    assert [r.status for r in fake.results][-1] is PlacementStatus.PLACED


def test_halt_login_and_balance_rules() -> None:
    halted = FakeExecutor(halt_check=lambda: (True, "kill switch is on"))
    result = halted.place(order())
    assert result.status is PlacementStatus.KILL_SWITCH and "kill switch" in result.message
    logged_out = FakeExecutor(logged_in=False)
    assert logged_out.place(order()).status is PlacementStatus.LOGIN_REQUIRED
    assert logged_out.get_balance() is None
    assert logged_out.health_check().ok is False
    with pytest.raises(LoginRequiredError):
        logged_out.fetch_bet_history(utcnow())
    assert logged_out.open_login_window(5) is True and logged_out.is_logged_in()
    poor = FakeExecutor(balance=Decimal("5.00"))
    assert poor.place(order()).status is PlacementStatus.INSUFFICIENT_BALANCE


def test_settle_and_history_filter() -> None:
    old = BookmakerBetRecord(
        external_bet_id="OLD", placed_at=datetime(2026, 1, 1, tzinfo=UTC), status=BookmakerBetStatus.LOST
    )
    fake = FakeExecutor(balance=Decimal("100.00"), history=[old])
    placed = fake.place(order())
    assert placed.external_bet_id is not None
    fake.settle(placed.external_bet_id, BookmakerBetStatus.WON, Decimal("18.50"))
    assert fake.balance == Decimal("108.50")
    recent = fake.fetch_bet_history(datetime(2026, 6, 1, tzinfo=UTC))
    assert [r.external_bet_id for r in recent] == [placed.external_bet_id]
    assert recent[0].status is BookmakerBetStatus.WON and recent[0].payout == Decimal("18.50")
    with pytest.raises(KeyError):
        fake.settle("missing", BookmakerBetStatus.VOID)


def test_health_override_and_close() -> None:
    fake = FakeExecutor()
    assert fake.health_check().ok is True
    fake.close()
    assert fake.closed
    with pytest.raises(ExecutorClosedError):
        fake.place(order())


def test_bookmaker_bet_record_requires_aware_datetimes() -> None:
    with pytest.raises(ValueError):
        BookmakerBetRecord(external_bet_id="x", placed_at=datetime(2026, 1, 1))
    rec = BookmakerBetRecord(external_bet_id="x", placed_at=datetime(2026, 1, 1, 1, tzinfo=UTC))
    assert rec.placed_at is not None and rec.placed_at.tzinfo is not None


def test_never_places_twice_for_one_idempotency_key() -> None:
    """Same rule as the real executor: PLACED / UNCERTAIN keys are refused, rejections may be retried."""
    fake = FakeExecutor([PlacementStatus.UNCERTAIN, PlacementStatus.INSUFFICIENT_BALANCE])
    unsure = order()
    assert fake.place(unsure).status is PlacementStatus.UNCERTAIN
    again = fake.place(unsure)
    assert again.status is PlacementStatus.UNCERTAIN and "refusing to place twice" in again.message
    assert len(fake.script) == 1  # the refusal did not consume the script
    rejected = order()
    assert fake.place(rejected).status is PlacementStatus.INSUFFICIENT_BALANCE
    placed = fake.place(rejected)  # a definite rejection may be retried
    assert placed.status is PlacementStatus.PLACED and placed.external_bet_id == "FAKE-1"
    assert fake.place(rejected).status is PlacementStatus.UNCERTAIN
    assert len(fake.history) == 1 and fake.balance == Decimal("990.00")
    # dry runs never click: they neither block a key nor are they blocked by an unrelated one
    dry = order(dry_run=True)
    assert fake.place(dry).status is PlacementStatus.DRY_RUN_OK
    assert fake.place(dry).status is PlacementStatus.DRY_RUN_OK
    loose = FakeExecutor(enforce_idempotency=False)
    twice = order()
    assert [loose.place(twice).status, loose.place(twice).status] == [PlacementStatus.PLACED] * 2
