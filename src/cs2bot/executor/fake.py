"""In-memory executor for tests and integration work (no browser, no network).

Results can be scripted per call (a ``PlacementResult``, a ``PlacementStatus`` or a callable); without a
script it behaves like a well-behaved bookmaker: honours the halt check, ``LOGIN_REQUIRED`` when logged
out, ``DRY_RUN_OK`` for dry-run orders, ``INSUFFICIENT_BALANCE`` when the stake exceeds the balance, else
``PLACED`` (balance reduced, an open bet added to the history for reconciliation tests). Every call is
recorded in :attr:`FakeExecutor.calls`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from cs2bot.domain.betting import BetOrder, ExecutorHealth, PlacementResult, PlacementStatus
from cs2bot.executor.base import (
    BookmakerBetRecord,
    BookmakerBetStatus,
    ExecutorClosedError,
    HaltCheck,
    LoginRequiredError,
)
from cs2bot.utils.time import ensure_utc, utcnow

ScriptItem = PlacementResult | PlacementStatus | Callable[[BetOrder], PlacementResult]


class FakeExecutor:
    """Scriptable :class:`~cs2bot.executor.base.AccountExecutor` that records every call."""

    def __init__(
        self,
        script: Iterable[ScriptItem] = (),
        *,
        balance: Decimal | None = Decimal("1000.00"),
        logged_in: bool = True,
        halt_check: HaltCheck | None = None,
        honour_dry_run: bool = True,
        health: ExecutorHealth | None = None,
        history: Iterable[BookmakerBetRecord] = (),
        login_succeeds: bool = True,
    ) -> None:
        self.script: deque[ScriptItem] = deque(script)
        self.balance = balance
        self.logged_in = logged_in
        self.halt_check = halt_check
        self.honour_dry_run = honour_dry_run
        self.health = health
        self.history: list[BookmakerBetRecord] = list(history)
        self.login_succeeds = login_succeeds
        self.calls: list[tuple[str, Any]] = []
        self.orders: list[BetOrder] = []
        self.results: list[PlacementResult] = []
        self.closed = False
        self._seq = 0

    # ------------------------------------------------------------------ scripting

    def queue(self, *items: ScriptItem) -> None:
        """Append scripted results for the next ``place`` calls."""
        self.script.extend(items)

    def settle(self, external_bet_id: str, status: BookmakerBetStatus, payout: Decimal | None = None) -> None:
        """Mark a history bet as settled (and credit ``payout`` to the balance)."""
        for i, record in enumerate(self.history):
            if record.external_bet_id == external_bet_id:
                self.history[i] = record.model_copy(update={"status": status, "payout": payout})
                if payout is not None and self.balance is not None:
                    self.balance += payout
                return
        raise KeyError(external_bet_id)

    # ------------------------------------------------------------------ BetExecutor

    def place(self, order: BetOrder) -> PlacementResult:
        self._check_open()
        self.calls.append(("place", order))
        self.orders.append(order)
        result = self._next_result(order)
        self.results.append(result)
        return result

    def get_balance(self) -> Decimal | None:
        self._check_open()
        self.calls.append(("get_balance", None))
        return self.balance if self.logged_in else None

    def health_check(self) -> ExecutorHealth:
        self._check_open()
        self.calls.append(("health_check", None))
        if self.health is not None:
            return self.health
        return ExecutorHealth(
            ok=self.logged_in and self.balance is not None,
            logged_in=self.logged_in,
            balance=self.balance if self.logged_in else None,
            selectors_ok={},
            message="fake executor" if self.logged_in else "fake executor: not logged in",
            checked_at=utcnow(),
        )

    def close(self) -> None:
        self.calls.append(("close", None))
        self.closed = True

    # ------------------------------------------------------------------ AccountExecutor

    def is_logged_in(self) -> bool:
        self._check_open()
        self.calls.append(("is_logged_in", None))
        return self.logged_in

    def open_login_window(self, timeout_s: float = 600.0) -> bool:
        self._check_open()
        self.calls.append(("open_login_window", timeout_s))
        if self.login_succeeds:
            self.logged_in = True
        return self.logged_in

    def fetch_bet_history(self, since: datetime) -> list[BookmakerBetRecord]:
        self._check_open()
        self.calls.append(("fetch_bet_history", since))
        if not self.logged_in:
            raise LoginRequiredError("fake executor: not logged in")
        since_utc = ensure_utc(since)
        return [r for r in self.history if r.placed_at is None or r.placed_at >= since_utc]

    # ------------------------------------------------------------------ internals

    def _check_open(self) -> None:
        if self.closed:
            raise ExecutorClosedError("this FakeExecutor was closed")

    def _next_result(self, order: BetOrder) -> PlacementResult:
        if self.halt_check is not None:
            halted, reason = self.halt_check()
            if halted:
                return self._make(order, PlacementStatus.KILL_SWITCH, f"trading halted: {reason}")
        if self.script:
            item = self.script.popleft()
            if isinstance(item, PlacementResult):
                result = item
            elif isinstance(item, PlacementStatus):
                result = self._make(order, item)
            else:
                result = item(order)
            self._apply(order, result)
            return result
        if not self.logged_in:
            return self._make(order, PlacementStatus.LOGIN_REQUIRED, "fake executor: not logged in")
        if self.honour_dry_run and order.dry_run:
            return self._make(order, PlacementStatus.DRY_RUN_OK)
        if self.balance is not None and order.stake > self.balance:
            return self._make(
                order, PlacementStatus.INSUFFICIENT_BALANCE, "fake executor: stake exceeds balance"
            )
        result = self._make(order, PlacementStatus.PLACED)
        self._apply(order, result)
        return result

    def _make(self, order: BetOrder, status: PlacementStatus, message: str = "") -> PlacementResult:
        placed_odds = (
            order.requested_odds if status in (PlacementStatus.PLACED, PlacementStatus.DRY_RUN_OK) else None
        )
        external_id: str | None = None
        balance_after: Decimal | None = None
        if status is PlacementStatus.PLACED:
            self._seq += 1
            external_id = f"FAKE-{self._seq}"
            if self.balance is not None:
                balance_after = self.balance - order.stake
        return PlacementResult(
            status=status,
            placed_odds=placed_odds,
            external_bet_id=external_id,
            balance_after=balance_after,
            message=message or f"fake executor: {status.value}",
            at=utcnow(),
        )

    def _apply(self, order: BetOrder, result: PlacementResult) -> None:
        """Side effects of a PLACED result: balance and an open history record."""
        if result.status is not PlacementStatus.PLACED:
            return
        if result.balance_after is not None:
            self.balance = result.balance_after
        elif self.balance is not None:
            self.balance -= order.stake
        self.history.append(
            BookmakerBetRecord(
                external_bet_id=result.external_bet_id,
                placed_at=result.at,
                event_name=order.external_event_id,
                market_name=order.market_raw_name,
                selection_name=order.selection_raw_name,
                odds=result.placed_odds or order.requested_odds,
                stake=order.stake,
                status=BookmakerBetStatus.OPEN,
            )
        )
