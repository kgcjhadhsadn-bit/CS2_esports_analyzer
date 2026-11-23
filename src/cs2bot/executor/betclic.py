"""Betclic.pl bet placement through a visible browser with a dedicated, persistent profile.

The owner logs in by hand once (incl. SMS / 2FA) via :meth:`BetclicExecutor.open_login_window`; the
profile keeps the session. :meth:`BetclicExecutor.place` then places single pre-match bets:

0. the idempotency key was already clicked (this process or the placement journal) -> ``UNCERTAIN``
1. halt check (kill switch / pause)                -> ``KILL_SWITCH``
2. open the event page (``order.event_url`` or find the event on the CS2 listing by its id)
3. logged in?                                      -> ``LOGIN_REQUIRED``
4. clear stale bet-slip selections
5. find the market and the selection by normalised name (ids only disambiguate; never guess)
6. read the displayed odds (``"1,85"``); below ``min_acceptable_odds`` (or implausibly far above the
   requested odds) -> ``ODDS_MOVED``; after the human-like pause the chosen elements are read AGAIN
   (they are addressed by position, and a live page may re-render) before the click
7. click the selection; the slip must hold exactly that one selection (hidden ones count), showing the
   ordered selection and market, with acceptable odds
8. type the stake Polish-style (``"12,50"``) and verify the field; when the slip shows a potential win
   it must be consistent with stake x odds
9. dry run -> screenshot, clear the slip, ``DRY_RUN_OK`` (the final button is never clicked)
10. re-read the slip (still one selection, odds still acceptable, stake field still equal to the order
    stake), make sure the button is clickable, write the write-ahead journal record, then the halt
    check again, immediately before the click
11. click "Postaw zakład"; handle the odds-changed dialog (accept only at >= min odds); wait for the
    receipt (bet id, new balance) -> ``PLACED``; known errors -> their status; anything unclear after the
    click -> ``UNCERTAIN`` (never retried; reconcile via :meth:`BetclicExecutor.fetch_bet_history`).

Fail safe and loud: every lookup goes through ``config/betclic_selectors.yaml``; a selector that does not
resolve aborts with ``SELECTOR_ERROR`` (plus screenshot and page HTML) before any money is at risk.
Real bets need BOTH ``order.dry_run`` and ``settings.executor.dry_run`` to be false. Each attempt is
screenshotted into ``settings.executor.screenshot_dir``. No stealth / anti-detection tricks: a real,
visible Chromium with random human-like pauses and a minimum gap between bets.

Never twice: a key whose final button was clicked with a ``PLACED`` / ``UNCERTAIN`` outcome is refused
from then on - also after a restart or crash, through the write-ahead
:class:`~cs2bot.executor.journal.PlacementJournal` kept in the browser-profile directory.

Not thread-safe: use one thread (or :class:`~cs2bot.executor.threaded.ThreadBoundExecutor`).
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from cs2bot.config import Settings
from cs2bot.domain.betting import BetOrder, ExecutorHealth, PlacementResult, PlacementStatus
from cs2bot.executor.base import (
    BookmakerBetRecord,
    BookmakerBetStatus,
    BrowserStartError,
    ExecutorClosedError,
    ExecutorError,
    HaltCheck,
    LoginRequiredError,
)
from cs2bot.executor.journal import JOURNAL_FILENAME, JournalError, PlacementJournal
from cs2bot.executor.lock import ProfileLockedError
from cs2bot.executor.matching import Candidate, MatchKind, pick_candidate
from cs2bot.executor.safety import HumanPacer
from cs2bot.executor.selectors import ResolvedElement, SelectorNotFoundError, SelectorSet, load_selectors
from cs2bot.executor.session import BrowserSession, PlaywrightFactory
from cs2bot.executor.textutils import (
    find_polish_decimals,
    first_regex_group,
    format_polish_decimal,
    names_compatible,
    normalise_text,
    parse_polish_datetime,
    parse_polish_decimal,
    screenshot_filename,
    try_parse_polish_decimal,
)
from cs2bot.executor.tuning import ExecutorTuning
from cs2bot.utils.logging import get_logger
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import ensure_utc, utcnow

log = get_logger(__name__)

# Classification order of bet-slip messages (``texts`` section): the first matching category wins.
# "ignore" = harmless notices, "pending" = keep waiting, "bet_accepted" = confirmation.
MESSAGE_CATEGORY_ORDER: tuple[str, ...] = (
    "ignore",
    "pending",
    "bet_accepted",
    "insufficient_balance",
    "stake_limit",
    "odds_changed",
    "market_suspended",
    "login_required",
    "rejected",
)
CATEGORY_STATUS: dict[str, PlacementStatus] = {
    "insufficient_balance": PlacementStatus.INSUFFICIENT_BALANCE,
    "stake_limit": PlacementStatus.REJECTED_BY_BOOKMAKER,
    "odds_changed": PlacementStatus.ODDS_MOVED,
    "market_suspended": PlacementStatus.MARKET_UNAVAILABLE,
    "login_required": PlacementStatus.LOGIN_REQUIRED,
    "rejected": PlacementStatus.REJECTED_BY_BOOKMAKER,
}
HISTORY_STATUS_ORDER: tuple[tuple[str, BookmakerBetStatus], ...] = (
    ("history_status_cashout", BookmakerBetStatus.CASHOUT),
    ("history_status_void", BookmakerBetStatus.VOID),
    ("history_status_lost", BookmakerBetStatus.LOST),
    ("history_status_won", BookmakerBetStatus.WON),
    ("history_status_open", BookmakerBetStatus.OPEN),
)
# After the final click only these statuses mean "definitely no bet"; anything else that is not PLACED
# is reported as UNCERTAIN (never retried automatically).
POST_CLICK_DEFINITE: frozenset[PlacementStatus] = frozenset(
    {
        PlacementStatus.INSUFFICIENT_BALANCE,
        PlacementStatus.REJECTED_BY_BOOKMAKER,
        PlacementStatus.ODDS_MOVED,
        PlacementStatus.MARKET_UNAVAILABLE,
        PlacementStatus.LOGIN_REQUIRED,
        PlacementStatus.KILL_SWITCH,
    }
)
DEFAULT_LOCKED_CLASSES = ("is-locked", "is-disabled", "is-suspended", "disabled", "locked")
_BET_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{3,}")
_SAFE_EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class LoginState(StrEnum):
    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"


class _Abort(Exception):
    """Internal: stop the placement flow with a result status."""

    def __init__(
        self,
        status: PlacementStatus,
        message: str,
        *,
        clear_slip: bool = True,
        dump_html: bool = False,
        placed_odds: Decimal | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.clear_slip = clear_slip
        self.dump_html = dump_html
        self.placed_odds = placed_odds


@dataclass
class _Attempt:
    order: BetOrder
    dry_run: bool
    journaled: bool = False  # the write-ahead "click" record exists (set right before the final click)
    place_clicked: bool = False
    odds: Decimal | None = None  # latest known odds of the selection


class BetclicExecutor:
    """Places single pre-match bets on the owner's Betclic.pl account (see module docstring)."""

    def __init__(
        self,
        settings: Settings,
        *,
        selectors: SelectorSet | None = None,
        halt_check: HaltCheck,
        playwright_factory: PlaywrightFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
        tuning: ExecutorTuning | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.tuning = tuning if tuning is not None else ExecutorTuning.from_settings(settings)
        self.selectors = selectors if selectors is not None else load_selectors(settings=settings)
        self._halt_check = halt_check
        self._pacer = HumanPacer(
            settings.executor.action_delay_min_s,
            settings.executor.action_delay_max_s,
            sleep=sleep,
            rng=rng,
            typing_delay_ms=(self.tuning.typing_delay_min_ms, self.tuning.typing_delay_max_ms),
        )
        self._session = BrowserSession(settings, tuning=self.tuning, playwright_factory=playwright_factory)
        self._base_url = settings.betclic.base_url.rstrip("/")
        self._base_host = _host_key(urlsplit(self._base_url).netloc)
        self._attempted: dict[str, PlacementStatus] = {}  # keys whose final click may have placed a bet
        self._journal = PlacementJournal(
            self._session.profile_dir / JOURNAL_FILENAME, retention_days=self.tuning.journal_retention_days
        )
        self._journal.compact()  # best effort, never raises
        self._last_attempt_end: float | None = None
        self._closed = False

    # ================================================================== public API

    @property
    def screenshot_dir(self) -> Path:
        return resolve_path(self.settings.executor.screenshot_dir)

    @property
    def listing_url(self) -> str:
        return self._safe_url(self.selectors.urls.cs2_listing or self.settings.betclic.cs2_listing_path)

    def place(self, order: BetOrder) -> PlacementResult:
        """Place one single bet (see the module docstring for the exact flow and statuses)."""
        self._ensure_open()
        self._session.check_thread()
        attempt = _Attempt(order=order, dry_run=order.dry_run or self.settings.executor.dry_run)
        log.info(
            "placement start: bet_id=%s key=%s event=%s market=%r selection=%r stake=%s min_odds=%s "
            "dry_run=%s",
            order.bet_id,
            order.idempotency_key,
            order.external_event_id,
            order.market_raw_name,
            order.selection_raw_name,
            order.stake,
            order.min_acceptable_odds,
            attempt.dry_run,
        )
        problem = self._validate_order(order)
        if problem is not None:
            return self._result(attempt, PlacementStatus.FAILED, problem, take_screenshot=False)
        refusal = self._refuse_if_already_clicked(attempt)
        if refusal is not None:
            return refusal
        halted, reason = self._halted()
        if halted:  # checked before the pacing gap: a halted bot answers at once
            return self._result(attempt, PlacementStatus.KILL_SWITCH, f"trading halted: {reason}")
        self._respect_min_gap()
        try:
            try:
                page = self._session.ensure_page()
                self._session.ensure_lock()
            except (BrowserStartError, ProfileLockedError) as exc:
                return self._result(attempt, PlacementStatus.FAILED, str(exc), take_screenshot=False)
            try:
                return self._place_on_page(page, attempt)
            except _Abort as abort:
                return self._handle_abort(attempt, abort)
            except SelectorNotFoundError as exc:
                if attempt.place_clicked:
                    return self._handle_abort(
                        attempt, _Abort(PlacementStatus.UNCERTAIN, str(exc), clear_slip=False, dump_html=True)
                    )
                return self._handle_abort(
                    attempt, _Abort(PlacementStatus.SELECTOR_ERROR, str(exc), dump_html=True)
                )
            except PlaywrightError as exc:  # includes Playwright's TimeoutError
                detail = _short_error(exc)
                if attempt.place_clicked:
                    return self._handle_abort(
                        attempt,
                        _Abort(
                            PlacementStatus.UNCERTAIN,
                            f"browser error after the place click ({detail}); reconcile via bet history",
                            clear_slip=False,
                        ),
                    )
                return self._handle_abort(
                    attempt, _Abort(PlacementStatus.FAILED, f"browser error before the place click: {detail}")
                )
            except Exception as exc:
                log.exception("unexpected error while placing bet %s", order.bet_id)
                status = PlacementStatus.UNCERTAIN if attempt.place_clicked else PlacementStatus.FAILED
                return self._handle_abort(
                    attempt,
                    _Abort(
                        status,
                        f"unexpected error: {type(exc).__name__}",
                        clear_slip=not attempt.place_clicked,
                    ),
                )
        finally:
            self._last_attempt_end = time.monotonic()

    def get_balance(self) -> Decimal | None:
        """Header balance (``"1 234,56 zł"`` -> ``Decimal("1234.56")``); ``None`` if logged out/unreadable."""
        self._ensure_open()
        try:
            page = self._session.ensure_page()
            self._goto(page, self._base_url + "/")
            if self._login_state(page) is not LoginState.LOGGED_IN:
                return None
            return self._read_balance(page, self.tuning.element_timeout_s)
        except (BrowserStartError, ProfileLockedError, PlaywrightError) as exc:
            log.warning("could not read the Betclic balance: %s", _short_error(exc))
            return None

    def is_logged_in(self) -> bool:
        """Load the Betclic home page and check the header for the logged-in state."""
        self._ensure_open()
        page = self._session.ensure_page()
        self._goto(page, self._base_url + "/")
        return self._login_state(page) is LoginState.LOGGED_IN

    def open_login_window(self, timeout_s: float = 600.0) -> bool:
        """Open the (visible) browser on the login page and wait until the owner has logged in.

        With ``credentials_source`` "env"/"keyring" the username/password fields are pre-filled but NOT
        submitted; the owner clicks "Zaloguj się" and completes SMS/2FA by hand. Returns ``True`` once
        the logged-in indicator appears, ``False`` on timeout. Credentials are never logged.
        """
        self._ensure_open()
        if self.settings.executor.headless:
            log.warning(
                "executor.headless is true: the login window is invisible "
                "(set CS2BOT_EXECUTOR__HEADLESS=false)"
            )
        page = self._session.ensure_page()
        self._goto(page, self._safe_url(self.selectors.urls.login))
        if self._login_state(page) is LoginState.LOGGED_IN:
            log.info("Betclic: already logged in")
            return True
        if self.selectors.probe(page, "login_username_input") is None:
            button = self.selectors.try_resolve(page, "login_button", self.tuning.element_timeout_s)
            if button is not None:
                self._pacer.pause(0.5)
                button.first.click()
            else:
                log.warning("login button not found - open the login form by hand")
        credentials = self._load_credentials()
        if credentials is not None:
            self._prefill_login(page, *credentials)
        log.info("waiting up to %.0fs for the owner to log in to Betclic (incl. SMS / 2FA)", timeout_s)
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            current = self._session.current_page()
            if current is None:
                log.info("browser window closed during login - checking the saved session")
                page = self._session.ensure_page()
                self._goto(page, self._base_url + "/")
                return self._login_state(page) is LoginState.LOGGED_IN
            try:
                if self.selectors.probe(current, "logged_in_indicator") is not None:
                    log.info("Betclic login detected")
                    return True
                current.wait_for_timeout(self.tuning.login_poll_s * 1000)
            except PlaywrightError:  # navigation in progress or the tab was closed
                time.sleep(min(self.tuning.login_poll_s, 0.5))
        log.warning("Betclic login not completed within %.0fs", timeout_s)
        return False

    def health_check(self) -> ExecutorHealth:
        """Browser starts, logged in, balance readable, key selectors resolvable (listing + one event)."""
        self._ensure_open()
        t = self.tuning
        problems: list[str] = []
        selectors_ok: dict[str, bool] = {}
        logged_in: bool | None = None
        balance: Decimal | None = None
        try:
            page = self._session.ensure_page()
        except (BrowserStartError, ProfileLockedError) as exc:
            return ExecutorHealth(ok=False, message=f"browser: {exc}", checked_at=utcnow())
        try:
            self._goto(page, self.listing_url)
            state = self._login_state(page)
            selectors_ok["login_state"] = state is not LoginState.UNKNOWN
            if state is LoginState.UNKNOWN:
                problems.append("login state unknown (selectors 'logged_in_indicator' / 'login_button')")
            else:
                logged_in = state is LoginState.LOGGED_IN
                if not logged_in:
                    problems.append("not logged in - run 'cs2bot executor login'")
            if logged_in:
                balance = self._read_balance(page, t.element_timeout_s)
                selectors_ok["header_balance"] = balance is not None
                if balance is None:
                    problems.append("balance not readable (selector 'header_balance')")
                nav = self.selectors.try_resolve(page, "nav_my_bets", t.optional_timeout_s, state="attached")
                selectors_ok["nav_my_bets"] = nav is not None
                if nav is None:
                    problems.append("'Moje zakłady' link not found (selector 'nav_my_bets')")
            link = self.selectors.try_resolve(page, "event_link", t.element_timeout_s, state="attached")
            selectors_ok["event_link"] = link is not None
            if link is None:
                problems.append(
                    "no CS2 event links on the listing (selector 'event_link', or no events listed)"
                )
            elif t.health_check_event_page:
                self._health_check_event_page(page, link, selectors_ok, problems)
        except PlaywrightError as exc:
            problems.append(f"browser error: {_short_error(exc)}")
        except (ValueError, ExecutorError) as exc:  # e.g. a misconfigured listing URL
            problems.append(f"configuration: {exc}")
        try:
            self._journal.entries()
        except JournalError as exc:  # real bets are refused while the journal cannot be read
            problems.append(str(exc))
        ok = not problems and logged_in is True and balance is not None
        return ExecutorHealth(
            ok=ok,
            logged_in=logged_in,
            balance=balance,
            selectors_ok=selectors_ok,
            message="; ".join(problems) or "ok",
            checked_at=utcnow(),
        )

    def fetch_bet_history(self, since: datetime) -> list[BookmakerBetRecord]:
        """Bets from "Moje zakłady" placed at/after ``since`` (rows with an unknown date are kept).

        Best effort and selector-driven. Raises :class:`LoginRequiredError` when logged out and
        :class:`SelectorNotFoundError` when the history rows cannot be located.
        """
        self._ensure_open()
        since_utc = ensure_utc(since)
        t = self.tuning
        page = self._session.ensure_page()
        self._open_history(page)
        tab = self.selectors.try_resolve(page, "history_tab_all", t.optional_timeout_s)
        if tab is not None:
            self._pacer.pause(0.5)
            tab.first.click()
        deadline = time.monotonic() + t.element_timeout_s
        while self.selectors.probe(page, "history_item") is None:
            if self.selectors.probe(page, "history_empty") is not None:
                return []
            if time.monotonic() >= deadline:
                self.selectors.resolve(page, "history_item", 0)  # raises with the list of locators tried
            page.wait_for_timeout(t.poll_interval_s * 1000)
        now = utcnow()
        records: list[BookmakerBetRecord] = []
        parsed = 0
        for _ in range(t.history_max_load_more + 1):
            rows = self.selectors.resolve(page, "history_item", t.element_timeout_s).all()
            for row in rows[parsed:]:
                try:
                    record = self._parse_history_row(row, now)
                except (PlaywrightError, ValueError) as exc:  # one bad row must not lose the others
                    log.warning("skipping unreadable bet-history row: %s", _short_error(exc))
                    continue
                if record is not None:
                    records.append(record)
            parsed = len(rows)
            dates = [r.placed_at for r in records if r.placed_at is not None]
            if dates and min(dates) < since_utc:
                break
            more = self.selectors.probe(page, "history_load_more")
            if more is None:
                break
            self._pacer.pause(0.5)
            more.first.click()
            deadline = time.monotonic() + t.slip_update_timeout_s
            grew = False
            while time.monotonic() < deadline and not grew:
                now_rows = self.selectors.probe(page, "history_item")
                grew = now_rows is not None and now_rows.count > parsed
                if not grew:
                    page.wait_for_timeout(t.poll_interval_s * 1000)
            if not grew:  # nothing more was loaded: do not keep clicking
                log.info("bet history: 'load more' added no rows; stopping at %d row(s)", parsed)
                break
        return [r for r in records if r.placed_at is None or r.placed_at >= since_utc]

    def close(self) -> None:
        """Close the browser and release the profile lock (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._session.close()

    def __enter__(self) -> BetclicExecutor:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ================================================================== placement flow

    def _place_on_page(self, page: Page, attempt: _Attempt) -> PlacementResult:
        order = attempt.order
        t = self.tuning
        self._open_event(page, order)
        self._require_logged_in(page)
        pre_balance = self._read_balance(page, t.optional_timeout_s)
        self._dismiss_stale_receipt(page, strict=False)
        self._clear_slip(page, strict=True)

        market = self._find_market(page, order)
        selection = self._find_selection(market, order)
        if self._is_locked(selection):
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE, "the selection is suspended / locked on the event page"
            )
        displayed = self._read_selection_odds(selection)
        attempt.odds = displayed
        self._require_acceptable_odds(displayed, order, "displayed odds")

        self._pacer.pause()
        # ``market`` / ``selection`` address elements by POSITION. While we paused, a live page may have
        # re-rendered (a market added, removed or suspended above ours), so the same position can now be
        # another market's outcome with the same name. Read everything again right before the click.
        displayed = self._confirm_pick(market, selection, order)
        attempt.odds = displayed
        self._require_acceptable_odds(displayed, order, "displayed odds")
        selection.click()
        slip_odds = self._verify_slip_single(page, order, displayed)
        attempt.odds = slip_odds
        self._require_acceptable_odds(slip_odds, order, "bet-slip odds")
        self._enter_stake(page, order.stake)
        self._raise_if_slip_message(page)
        self._check_potential_win(page, order, slip_odds, wait=True)

        if attempt.dry_run:
            shot = self._screenshot(attempt, PlacementStatus.DRY_RUN_OK)
            self._clear_slip(page, strict=False)
            return self._result(
                attempt,
                PlacementStatus.DRY_RUN_OK,
                f"dry run: selection found at odds {slip_odds}, stake {format_polish_decimal(order.stake)} "
                f"entered and verified; the place-bet button was NOT clicked",
                placed_odds=slip_odds,
                screenshot_path=shot,
                take_screenshot=False,
            )

        self._dismiss_stale_receipt(page, strict=True)
        button = self._resolve_single(page, "slip_place_button")
        if not button.is_enabled():
            self._raise_if_slip_message(page)
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR, "the place-bet button is disabled and no error is shown"
            )
        self._pacer.pause()
        # The slip may have changed silently while we typed/paused (odds update without a dialog, a
        # re-render resetting the stake, a late stale selection): verify all of it again.
        current = self._read_slip_odds(page)
        if current is None:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                "the bet slip changed before confirming (expected one selection with readable odds)",
                dump_html=True,
            )
        self._require_acceptable_odds(current, order, "bet-slip odds right before confirming")
        if current != slip_odds:
            log.info("bet-slip odds changed from %s to %s before confirming", slip_odds, current)
            slip_odds = attempt.odds = current
        self._verify_stake_field(page, order.stake)
        self._check_potential_win(page, order, slip_odds, wait=False)
        self._raise_if_slip_message(page)
        self._ensure_clickable(button)
        self._journal_click(attempt)  # write-ahead: durable BEFORE the click, or no click at all
        halted, reason = self._halted()  # immediately before the click (after the human-like pause)
        if halted:
            raise _Abort(PlacementStatus.KILL_SWITCH, f"trading halted right before confirming: {reason}")
        attempt.place_clicked = True  # set BEFORE the click: a failing click must count as "maybe placed"
        log.info("clicking 'Postaw zakład' for bet_id=%s key=%s", order.bet_id, order.idempotency_key)
        self._click_place_button(button)
        return self._await_outcome(page, attempt, slip_odds, pre_balance)

    def _click_place_button(self, button: Locator) -> None:
        """The only place where "Postaw zakład" is clicked (never reached in dry-run mode)."""
        button.click()

    def _ensure_clickable(self, button: Locator) -> None:
        """Actionability check WITHOUT clicking (Playwright ``trial``): visible, stable, enabled, not
        covered. A button that cannot be clicked is a definite "nothing happened", not an UNCERTAIN."""
        timeout_s = min(self.tuning.action_timeout_s, self.tuning.element_timeout_s)
        try:
            button.click(trial=True, timeout=timeout_s * 1000)
        except PlaywrightError as exc:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"the place-bet button is not clickable (covered by a popup, moving or disabled): "
                f"{_short_error(exc)}; nothing was clicked",
                dump_html=True,
            ) from None

    def _journal_click(self, attempt: _Attempt) -> None:
        """Record the imminent final click durably; without the record the button is not clicked."""
        order = attempt.order
        try:
            self._journal.record_click(order.idempotency_key, order.bet_id)
        except JournalError as exc:
            raise _Abort(
                PlacementStatus.FAILED,
                f"{exc}; refusing to click 'Postaw zakład' without a durable record of the attempt",
            ) from None
        attempt.journaled = True
        self._attempted[order.idempotency_key] = PlacementStatus.UNCERTAIN

    def _refuse_if_already_clicked(self, attempt: _Attempt) -> PlacementResult | None:
        """``UNCERTAIN`` (without touching the page) if the final button was already clicked for this key.

        Checks this process's memory and the placement journal (earlier runs, crashes). An unreadable
        journal refuses real bets (``FAILED``). Dry runs never click the final button, so they are never
        refused here.
        """
        if attempt.dry_run:
            return None
        key = attempt.order.idempotency_key
        previous = self._attempted.get(key)
        detail = f"previous result {previous.value}" if previous is not None else None
        if detail is None:
            try:
                entry = self._journal.blocking_entry(key)
            except JournalError as exc:
                return self._result(
                    attempt,
                    PlacementStatus.FAILED,
                    f"{exc}; refusing to place real bets without the placement journal",
                    take_screenshot=False,
                )
            if entry is None:
                return None
            outcome = entry.status or "unknown - the bot stopped right after the click"
            detail = f"journal: clicked at {entry.at:%Y-%m-%d %H:%M:%S}Z, result {outcome}"
        return self._result(
            attempt,
            PlacementStatus.UNCERTAIN,
            f"refusing to place twice: the place button was already clicked for idempotency key "
            f"{key!r} ({detail}); reconcile via bet history",
            take_screenshot=False,
        )

    def _odds_problem(self, odds: Decimal, order: BetOrder) -> str | None:
        """Why ``odds`` must not be taken: below the minimum, or implausibly far above the request."""
        if odds < order.min_acceptable_odds:
            return (
                f"{odds} are below the minimum acceptable {order.min_acceptable_odds} "
                f"(requested {order.requested_odds})"
            )
        factor = self.tuning.max_odds_rise_factor
        if factor is not None and odds > order.requested_odds * Decimal(str(factor)):
            return (
                f"{odds} are more than {factor:g}x above the requested {order.requested_odds}: the price "
                "moved too far to trust the prediction (news, or the wrong outcome) - re-price first"
            )
        return None

    def _require_acceptable_odds(self, odds: Decimal, order: BetOrder, where: str) -> None:
        problem = self._odds_problem(odds, order)
        if problem is not None:
            raise _Abort(PlacementStatus.ODDS_MOVED, f"{where} {problem}")

    def _await_outcome(
        self, page: Page, attempt: _Attempt, odds: Decimal, pre_balance: Decimal | None
    ) -> PlacementResult:
        t = self.tuning
        accept_places = self.selectors.behaviour.odds_change_accept_places_bet
        deadline = time.monotonic() + t.confirmation_timeout_s
        rounds = 0
        reclick_pending = False
        unknown_message = ""
        odds_text_since: float | None = None  # an "odds changed" text is shown but no dialog (yet)
        while True:
            receipt = self.selectors.probe(page, "bet_receipt")
            if receipt is not None:
                receipt_text = _text(receipt.first)
                if self.selectors.classify(receipt_text, ("pending",)) is None:
                    return self._on_receipt(page, attempt, receipt.first, odds, pre_balance)
                # "przyjęty, oczekuje na akceptację": the bookmaker may still refuse it. Keep waiting for
                # the final word; without one the attempt ends UNCERTAIN (reconciled via bet history).
                unknown_message = receipt_text
            if self.selectors.probe(page, "odds_changed_notice") is not None:
                accept = self.selectors.probe(page, "odds_changed_accept")
                if accept is not None:
                    rounds += 1
                    odds = self._handle_odds_change(page, attempt, accept, rounds)
                    reclick_pending = not accept_places
                    deadline = time.monotonic() + t.confirmation_timeout_s
                    unknown_message = ""
                    odds_text_since = None
                    continue
            category, text, message = self._slip_message(page)
            if category == "bet_accepted" and message is not None:
                return self._on_receipt(page, attempt, message, odds, pre_balance)
            if category == "odds_changed" and rounds > 0:
                # We already accepted new odds, which may have submitted the bet: an "odds changed"
                # text still on screen is probably the old notice lingering while the bet is processed.
                # It must not be read as a rejection (the bet would exist unknown to the bot). A new
                # dialog with an accept button is handled above; otherwise keep waiting.
                unknown_message = text
            elif category == "odds_changed":
                # The page is probed piece by piece: the dialog with its accept button may render an
                # instant after this text. Look again for a moment before giving the bet up.
                if odds_text_since is None:
                    odds_text_since = time.monotonic()
                if time.monotonic() - odds_text_since >= t.optional_timeout_s:
                    raise _Abort(CATEGORY_STATUS[category], f"Betclic: {text}")
            elif category in CATEGORY_STATUS:
                raise _Abort(CATEGORY_STATUS[category], f"Betclic: {text}")
            elif category is None and text:
                # Not a known error and not (yet) a confirmation: keep waiting - a receipt or a known
                # message may follow. If nothing does, the attempt ends as UNCERTAIN below.
                unknown_message = text
            if reclick_pending and self._reclick_place(page, attempt):
                reclick_pending = False
                deadline = time.monotonic() + t.confirmation_timeout_s
                continue
            if time.monotonic() >= deadline:
                if unknown_message:
                    raise _Abort(
                        PlacementStatus.UNCERTAIN,
                        f"inconclusive Betclic message after the place click: {unknown_message!r} and no "
                        f"confirmation within {t.confirmation_timeout_s:g}s - do NOT retry; reconcile via "
                        "bet history",
                        clear_slip=False,
                    )
                raise _Abort(
                    PlacementStatus.UNCERTAIN,
                    f"the place-bet button was clicked but neither a confirmation nor an error appeared "
                    f"within {t.confirmation_timeout_s:g}s - do NOT retry; reconcile via 'Moje zakłady'",
                    clear_slip=False,
                )
            page.wait_for_timeout(t.poll_interval_s * 1000)

    def _handle_odds_change(
        self, page: Page, attempt: _Attempt, accept: ResolvedElement, rounds: int
    ) -> Decimal:
        order = attempt.order
        t = self.tuning
        new_odds = self._read_new_odds(page, attempt.odds)
        if new_odds is None:
            self._decline_odds_change(page)
            raise _Abort(
                PlacementStatus.ODDS_MOVED, "Betclic reported changed odds that could not be read; declined"
            )
        problem = self._odds_problem(new_odds, order)
        if problem is not None:
            self._decline_odds_change(page)
            raise _Abort(PlacementStatus.ODDS_MOVED, f"odds changed: the new odds {problem}; declined")
        if rounds > t.max_odds_change_rounds:
            self._decline_odds_change(page)
            raise _Abort(PlacementStatus.ODDS_MOVED, f"odds changed {rounds} times in a row; declined")
        self._pacer.pause(0.5)
        halted, reason = self._halted()
        if halted:
            self._decline_odds_change(page)
            raise _Abort(PlacementStatus.KILL_SWITCH, f"trading halted while accepting new odds: {reason}")
        log.info(
            "accepting changed odds %s -> %s (minimum %s)", attempt.odds, new_odds, order.min_acceptable_odds
        )
        accept.first.click()
        attempt.odds = new_odds
        deadline = time.monotonic() + t.slip_update_timeout_s
        while time.monotonic() < deadline:
            if (
                self.selectors.probe(page, "bet_receipt") is not None
                or self.selectors.probe(page, "slip_error") is not None
                or self.selectors.probe(page, "odds_changed_accept") is None
            ):
                break
            page.wait_for_timeout(t.poll_interval_s * 1000)
        return new_odds

    def _read_new_odds(self, page: Page, old_odds: Decimal | None) -> Decimal | None:
        """The price the bet would get if the odds-change dialog were accepted (``None`` = unknown).

        Two independent sources are read: the dialog (its ``odds_changed_new_odds`` element, else the
        single number / the number that is not the old price in its text) and the bet-slip item. The
        dialog text is free-form, so a number in it may not be the price at all (a stake, a date):

        * both readable -> the LOWER one (never accept on the strength of the more optimistic source);
        * only the slip -> used if it differs from the old price (else nothing tells us the new price);
        * only the dialog -> used only when it came from the explicit ``odds_changed_new_odds`` element.
        """
        from_notice: Decimal | None = None
        explicit = False
        notice = self.selectors.probe(page, "odds_changed_notice")
        if notice is not None:
            element = self.selectors.probe(notice.first, "odds_changed_new_odds", state="attached")
            if element is not None:
                value = try_parse_polish_decimal(_text(element.first))
                if value is not None and value > 1:
                    from_notice, explicit = value, True
            if from_notice is None:
                numbers = [n for n in find_polish_decimals(_text(notice.first)) if n > 1]
                if len(numbers) == 1:
                    from_notice = numbers[0]
                elif len(numbers) == 2 and old_odds is not None and old_odds in numbers:
                    from_notice = numbers[1] if numbers[0] == old_odds else numbers[0]
        slip_odds = self._read_slip_odds(page)
        if from_notice is not None and slip_odds is not None:
            return min(from_notice, slip_odds)
        if slip_odds is not None:
            return slip_odds if old_odds is not None and slip_odds != old_odds else None
        return from_notice if explicit else None

    def _decline_odds_change(self, page: Page) -> None:
        try:
            decline = self.selectors.probe(page, "odds_changed_decline")
            if decline is not None:
                decline.first.click()
        except PlaywrightError as exc:
            log.warning("could not click the decline button of the odds-change dialog: %s", _short_error(exc))

    def _reclick_place(self, page: Page, attempt: _Attempt) -> bool:
        """Acknowledge-mode only: click "Postaw zakład" once more after accepting new odds.

        The accept click may have submitted the bet after all (the site behaves differently from
        ``behaviour.odds_change_accept_places_bet: false``), so after a pause everything is checked
        again: a receipt or any message -> back to the main loop; a slip that no longer holds the one
        selection with the ordered stake -> ``UNCERTAIN`` (the bet may be in), never a second click.
        """
        order = attempt.order
        if self.selectors.probe(page, "odds_changed_notice") is not None:
            return False
        button = self.selectors.probe(page, "slip_place_button")
        if button is None or button.count != 1 or not button.first.is_enabled():
            return False
        self._pacer.pause(0.5)
        page.wait_for_timeout(self.tuning.slip_settle_s * 1000)
        if self.selectors.probe(page, "bet_receipt") is not None:
            return False
        category, text, _ = self._slip_message(page)
        if category == "pending" or (text and category != "odds_changed"):
            return False  # (a lingering "odds changed" text is expected: we have just acknowledged it)
        current = self._read_slip_odds(page)
        if current is None:
            raise _Abort(
                PlacementStatus.UNCERTAIN,
                "the bet slip changed after accepting new odds (expected one selection with readable odds); "
                "not clicking again - reconcile via bet history",
                clear_slip=False,
            )
        stake_field = self.selectors.probe(page, "slip_stake_input")
        shown: str | None = None
        if stake_field is not None and stake_field.count == 1:
            shown = stake_field.first.input_value()
        if shown is None or try_parse_polish_decimal(shown) != order.stake:
            raise _Abort(
                PlacementStatus.UNCERTAIN,
                f"the stake field shows {shown!r} after accepting new odds (expected "
                f"{format_polish_decimal(order.stake)!r}): the bet may already be placed; not clicking "
                "again - reconcile via bet history",
                clear_slip=False,
            )
        problem = self._odds_problem(current, order)
        if problem is not None:
            raise _Abort(PlacementStatus.ODDS_MOVED, f"bet-slip odds after accepting new odds {problem}")
        halted, reason = self._halted()
        if halted:
            raise _Abort(
                PlacementStatus.KILL_SWITCH, f"trading halted before re-confirming new odds: {reason}"
            )
        if not button.first.is_enabled():
            return False
        log.info("re-clicking 'Postaw zakład' after accepting new odds (acknowledge-mode dialog)")
        self._click_place_button(button.first)
        return True

    def _on_receipt(
        self,
        page: Page,
        attempt: _Attempt,
        root: Locator,
        odds: Decimal,
        pre_balance: Decimal | None,
    ) -> PlacementResult:
        """The bet is confirmed (``root`` = the receipt or the confirming message): collect the details."""
        receipt_text = _text(root)
        bet_id: str | None = None
        id_el = self.selectors.probe(root, "receipt_bet_id", state="attached")
        if id_el is not None:
            bet_id = self._clean_bet_id(_text(id_el.first))
        if not bet_id:
            bet_id = first_regex_group(receipt_text, self.selectors.compiled_patterns("bet_id"))
        placed_odds = odds
        odds_el = self.selectors.probe(root, "receipt_odds", state="attached")
        if odds_el is not None:
            shown = try_parse_polish_decimal(_text(odds_el.first))
            if shown is not None and shown > 1:
                placed_odds = shown
        balance_after: Decimal | None = None
        balance_el = self.selectors.probe(root, "receipt_balance", state="attached")
        if balance_el is not None:
            balance_after = try_parse_polish_decimal(_text(balance_el.first))
        if balance_after is None:
            balance_after = self._wait_balance_change(page, pre_balance)
        shot = self._screenshot(attempt, PlacementStatus.PLACED)
        close = self.selectors.probe(page, "receipt_close")
        if close is not None:
            try:
                close.first.click()
                # give the page a moment to really close it (the next bet must not find it)
                deadline = time.monotonic() + self.tuning.optional_timeout_s
                while self.selectors.probe(page, "bet_receipt") is not None and time.monotonic() < deadline:
                    page.wait_for_timeout(self.tuning.poll_interval_s * 1000)
            except PlaywrightError as exc:
                log.info("could not close the bet receipt: %s", _short_error(exc))
        order = attempt.order
        message = f"bet placed at odds {placed_odds}"
        message += f", Betclic bet id {bet_id}" if bet_id else ", Betclic bet id not found on the receipt"
        if balance_after is None:
            message += ", new balance not shown yet"
        elif pre_balance is not None and pre_balance - balance_after != order.stake:
            # Cannot be undone, but must be loud: a misread stake or another bet on the account.
            warning = (
                f"the balance changed by {pre_balance - balance_after} but the stake was {order.stake} - "
                "check this bet in 'Moje zakłady'"
            )
            log.error("bet_id=%s: %s", order.bet_id, warning)
            message += f"; WARNING: {warning}"
        if placed_odds < order.min_acceptable_odds:
            log.error(
                "bet_id=%s was accepted at odds %s, below the minimum acceptable %s",
                order.bet_id,
                placed_odds,
                order.min_acceptable_odds,
            )
            message += f"; WARNING: accepted below the minimum acceptable odds {order.min_acceptable_odds}"
        return self._result(
            attempt,
            PlacementStatus.PLACED,
            message,
            placed_odds=placed_odds,
            external_bet_id=bet_id,
            balance_after=balance_after,
            screenshot_path=shot,
            take_screenshot=False,
        )

    # ================================================================== flow helpers

    def _open_event(self, page: Page, order: BetOrder) -> None:
        t = self.tuning
        if order.event_url:
            try:
                url = self._safe_url(order.event_url)
            except ValueError as exc:
                raise _Abort(PlacementStatus.FAILED, str(exc), clear_slip=False) from None
            self._goto_event(page, url, order)
        else:
            self._goto(page, self.listing_url)
            params = {"event_id": order.external_event_id}
            link = self.selectors.try_resolve(
                page, "event_link_by_id", t.optional_timeout_s, state="attached", params=params
            )
            if link is None:
                # Wait for the listing to render (any event link) before concluding "not listed".
                if (
                    self.selectors.try_resolve(page, "event_link", t.element_timeout_s, state="attached")
                    is None
                ):
                    raise _Abort(
                        PlacementStatus.SELECTOR_ERROR,
                        f"no event links found on the CS2 listing {self.listing_url} (selector 'event_link')",
                        clear_slip=False,
                        dump_html=True,
                    )
                link = self.selectors.try_resolve(
                    page, "event_link_by_id", t.optional_timeout_s, state="attached", params=params
                )
            if link is None:
                raise _Abort(
                    PlacementStatus.MARKET_UNAVAILABLE,
                    f"event {order.external_event_id} not found on the CS2 listing {self.listing_url}",
                    clear_slip=False,
                )
            href = link.first.get_attribute("href")
            if not href:
                raise _Abort(PlacementStatus.SELECTOR_ERROR, "the event link has no href", clear_slip=False)
            try:
                url = self._safe_url(urljoin(page.url, href))
            except ValueError as exc:
                raise _Abort(PlacementStatus.FAILED, str(exc), clear_slip=False) from None
            self._goto_event(page, url, order)
        if self.selectors.probe(page, "event_unavailable") is not None:
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE, "Betclic shows the event as unavailable", clear_slip=False
            )

    def _goto_event(self, page: Page, url: str, order: BetOrder) -> None:
        """Open the event page; if the URL names the event id, the page we land on must name it too.

        Betclic redirects finished / removed events (to the listing, the home page or another event).
        Markets and selections are matched by name only ("Zwycięzca meczu" exists on every event), so a
        redirect must never be mistaken for the ordered event.
        """
        expected = _url_mentions_id(url, order.external_event_id)
        self._goto(page, url)
        if expected and not _url_mentions_id(page.url, order.external_event_id):
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE,
                f"the event page redirected to {page.url} (event finished or removed?)",
                clear_slip=False,
            )

    def _require_logged_in(self, page: Page) -> None:
        state = self._login_state(page)
        if state is LoginState.LOGGED_OUT:
            raise _Abort(
                PlacementStatus.LOGIN_REQUIRED,
                "not logged in to Betclic (session expired?) - log in again via the dashboard or "
                "'cs2bot executor login'",
                clear_slip=False,
            )
        if state is LoginState.UNKNOWN:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                "could not determine the login state: neither 'logged_in_indicator' nor 'login_button' "
                "resolved",
                clear_slip=False,
                dump_html=True,
            )

    def _login_state(self, page: Page, timeout_s: float | None = None) -> LoginState:
        """Logged-in indicator wins; "Zaloguj się" must stay visible for a moment (SPA hydration)."""
        t = self.tuning
        deadline = time.monotonic() + (t.login_check_timeout_s if timeout_s is None else timeout_s)
        logged_out_since: float | None = None
        while True:
            if self.selectors.probe(page, "logged_in_indicator") is not None:
                return LoginState.LOGGED_IN
            now = time.monotonic()
            if self.selectors.probe(page, "login_button") is not None:
                if logged_out_since is None:
                    logged_out_since = now
                if now - logged_out_since >= t.login_logged_out_stable_s:
                    return LoginState.LOGGED_OUT
            else:
                logged_out_since = None
            if now >= deadline:
                return LoginState.LOGGED_OUT if logged_out_since is not None else LoginState.UNKNOWN
            page.wait_for_timeout(t.poll_interval_s * 1000)

    def _read_balance(self, page: Page, timeout_s: float) -> Decimal | None:
        found = self.selectors.try_resolve(page, "header_balance", timeout_s)
        if found is None:
            return None
        for candidate in found.all(limit=5):
            text = _text(candidate)
            value = try_parse_polish_decimal(text)
            if value is not None:
                return value
            log.debug("unparsable balance text %r", text)
        return None

    def _wait_balance_change(self, page: Page, before: Decimal | None) -> Decimal | None:
        if before is None:
            return None
        deadline = time.monotonic() + self.tuning.balance_update_wait_s
        while True:
            current = self._read_balance(page, 0)
            if current is not None and current != before:
                return current
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(self.tuning.poll_interval_s * 1000)

    def _open_slip_if_collapsed(self, page: Page) -> None:
        if (
            not self.selectors.has("slip_container")
            or self.selectors.probe(page, "slip_container") is not None
        ):
            return
        toggle = self.selectors.probe(page, "slip_open_button")
        if toggle is None:
            return
        self._pacer.pause(0.5)
        toggle.first.click()
        self.selectors.try_resolve(page, "slip_container", self.tuning.slip_update_timeout_s)

    def _slip_count(self, page: Page) -> int:
        """Visible bet-slip selections."""
        items = self.selectors.probe(page, "slip_item")
        return items.count if items is not None else 0

    def _slip_total(self, page: Page) -> int:
        """Bet-slip selections including hidden ones (``behaviour.count_hidden_slip_items``).

        A selection that sits in the slip's DOM but is not displayed would still be part of the bet
        (turning the single into a combination), so by default hidden ones count as well.
        """
        visible = self._slip_count(page)
        if not self.selectors.behaviour.count_hidden_slip_items:
            return visible
        attached = self.selectors.probe(page, "slip_item", state="attached")
        return max(visible, attached.count if attached is not None else 0)

    def _clear_slip(self, page: Page, *, strict: bool) -> None:
        """Remove every bet-slip selection; ``strict`` raises SELECTOR_ERROR if that fails."""
        t = self.tuning
        self._open_slip_if_collapsed(page)
        count = self._slip_total(page)
        if count == 0:
            return
        log.info("clearing %d selection(s) from the bet slip", count)
        clear = self.selectors.try_resolve(page, "slip_clear_button", t.optional_timeout_s)
        if clear is not None:
            self._pacer.pause(0.5)
            clear.first.click()
            confirm = self.selectors.try_resolve(page, "slip_clear_confirm", t.optional_timeout_s)
            if confirm is not None:
                confirm.first.click()
        else:
            for _ in range(count + 3):
                items = self.selectors.probe(page, "slip_item")
                if items is None:
                    break
                remove = self.selectors.probe(items.first, "slip_item_remove")
                if remove is None:
                    break
                self._pacer.pause(0.3)
                remove.first.click()
                page.wait_for_timeout(t.poll_interval_s * 1000)
        deadline = time.monotonic() + t.slip_update_timeout_s
        while self._slip_total(page) > 0:
            if time.monotonic() >= deadline:
                remaining = self._slip_total(page)
                hint = ""
                if self._slip_count(page) == 0:
                    hint = (
                        " - they are hidden: if the page always keeps hidden slip items, make 'slip_item' "
                        "more specific or set behaviour.count_hidden_slip_items: false in "
                        "config/betclic_selectors.yaml"
                    )
                if strict:
                    raise _Abort(
                        PlacementStatus.SELECTOR_ERROR,
                        f"could not clear the bet slip ({remaining} selection(s) remain){hint}",
                        clear_slip=False,
                        dump_html=True,
                    )
                log.error("could not clear the bet slip (%d selection(s) remain)", remaining)
                return
            page.wait_for_timeout(t.poll_interval_s * 1000)

    def _dismiss_stale_receipt(self, page: Page, *, strict: bool) -> None:
        """Close a receipt left over from an earlier bet; it must never confirm the new one."""
        if self.selectors.probe(page, "bet_receipt") is None:
            return
        close = self.selectors.probe(page, "receipt_close")
        if close is not None:
            log.info("closing a bet receipt left over from an earlier placement")
            self._pacer.pause(0.3)
            close.first.click()
            deadline = time.monotonic() + self.tuning.slip_update_timeout_s
            while self.selectors.probe(page, "bet_receipt") is not None and time.monotonic() < deadline:
                page.wait_for_timeout(self.tuning.poll_interval_s * 1000)
        if strict and self.selectors.probe(page, "bet_receipt") is not None:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                "a receipt from an earlier bet is still displayed; refusing to click 'Postaw zakład' "
                "(it could be mistaken for this bet's confirmation)",
                dump_html=True,
            )

    def _safe_clear_slip(self) -> None:
        page = self._session.current_page()
        if page is None:
            return
        try:
            self._clear_slip(page, strict=False)
        except Exception as exc:  # best-effort cleanup must never hide the attempt's result
            log.warning("could not clear the bet slip after an aborted attempt: %s", _short_error(exc))

    def _find_market(self, page: Page, order: BetOrder) -> Locator:
        t = self.tuning
        found = self.selectors.try_resolve(page, "market_container", t.element_timeout_s)
        if found is None:
            if self.selectors.probe(page, "event_unavailable") is not None:  # rendered late
                raise _Abort(
                    PlacementStatus.MARKET_UNAVAILABLE,
                    "Betclic shows the event as unavailable",
                    clear_slip=False,
                )
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                "no market containers found on the event page (selector 'market_container')",
                dump_html=True,
            )
        containers = found.all(limit=t.max_market_candidates)
        candidates = self._market_candidates(containers)
        outcome = pick_candidate(candidates, order.market_raw_name, order.market_external_id)
        if outcome.kind is MatchKind.NOT_FOUND:
            tab = self.selectors.try_resolve(page, "all_markets_tab", t.optional_timeout_s)
            if tab is not None:
                self._pacer.pause(0.5)
                tab.first.click()
                page.wait_for_timeout(t.slip_settle_s * 1000)
                found = self.selectors.try_resolve(page, "market_container", t.element_timeout_s)
                if found is not None:
                    containers = found.all(limit=t.max_market_candidates)
                    candidates = self._market_candidates(containers)
                    outcome = pick_candidate(candidates, order.market_raw_name, order.market_external_id)
        if outcome.kind is MatchKind.FOUND and outcome.index is not None:
            return containers[outcome.index]
        if outcome.kind is MatchKind.NOT_FOUND:
            seen = ", ".join(repr(c.display) for c in candidates[:20] if c.display)
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE,
                f"market {order.market_raw_name!r} is not offered on the event page "
                f"(markets seen: {seen or 'none'})",
            )
        raise _Abort(
            PlacementStatus.SELECTOR_ERROR,
            f"market {order.market_raw_name!r} cannot be identified unambiguously: {outcome.detail}",
        )

    def _market_candidates(self, containers: list[Locator]) -> list[Candidate]:
        id_attrs = self.selectors.id_attributes("market_id")
        candidates: list[Candidate] = []
        for index, container in enumerate(containers):
            title_el = self.selectors.probe(container, "market_title", state="attached")
            title = _text(title_el.first) if title_el is not None else ""
            norm = normalise_text(title)
            candidates.append(
                Candidate(
                    index=index,
                    names=(norm,) if norm else (),
                    ids=_attribute_values(container, id_attrs),
                    display=title,
                )
            )
        return candidates

    def _find_selection(self, market: Locator, order: BetOrder) -> Locator:
        t = self.tuning
        found = self.selectors.try_resolve(market, "selection_button", t.optional_timeout_s)
        if found is None:
            expander = self.selectors.probe(market, "market_expand") or self.selectors.probe(
                market, "market_title"
            )
            if expander is not None:
                self._pacer.pause(0.5)
                expander.first.click()
                found = self.selectors.try_resolve(market, "selection_button", t.element_timeout_s)
        if found is None:
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE,
                f"market {order.market_raw_name!r} shows no selections (suspended or collapsed)",
            )
        buttons = found.all(limit=t.max_selection_candidates)
        candidates = self._selection_candidates(buttons)
        outcome = pick_candidate(candidates, order.selection_raw_name, order.selection_external_id)
        if outcome.kind is MatchKind.FOUND and outcome.index is not None:
            return buttons[outcome.index]
        if outcome.kind is MatchKind.NOT_FOUND:
            seen = ", ".join(repr(c.display) for c in candidates[:20] if c.display)
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE,
                f"selection {order.selection_raw_name!r} not found in market {order.market_raw_name!r} "
                f"(selections seen: {seen or 'none'})",
            )
        raise _Abort(
            PlacementStatus.SELECTOR_ERROR,
            f"selection {order.selection_raw_name!r} cannot be identified unambiguously: {outcome.detail}",
        )

    def _selection_candidates(self, buttons: list[Locator]) -> list[Candidate]:
        id_attrs = self.selectors.id_attributes("selection_id")
        candidates: list[Candidate] = []
        for index, button in enumerate(buttons):
            label_el = self.selectors.probe(button, "selection_label", state="attached")
            label = _text(label_el.first) if label_el is not None else ""
            full = _text(button)
            odds_el = self.selectors.probe(button, "selection_odds", state="attached")
            odds_text = _text(odds_el.first) if odds_el is not None else ""
            rest = full
            if odds_text and odds_text in full:  # drop the odds once, from the end (label first, odds last)
                head, _, tail = full.rpartition(odds_text)
                rest = f"{head} {tail}"
            names = tuple(dict.fromkeys(n for n in (normalise_text(label), normalise_text(rest)) if n))
            candidates.append(
                Candidate(
                    index=index,
                    names=names,
                    ids=_attribute_values(button, id_attrs),
                    display=label or rest.strip(),
                )
            )
        return candidates

    def _is_locked(self, selection: Locator) -> bool:
        if self.selectors.probe(selection, "selection_locked", state="attached") is not None:
            return True
        if (selection.get_attribute("aria-disabled") or "").lower() == "true":
            return True
        if selection.get_attribute("disabled") is not None:
            return True
        classes = set((selection.get_attribute("class") or "").split())
        locked = self.selectors.id_attributes("locked_classes") or list(DEFAULT_LOCKED_CLASSES)
        return bool(classes & set(locked))

    def _read_selection_odds(self, selection: Locator) -> Decimal:
        found = self.selectors.try_resolve(
            selection, "selection_odds", self.tuning.optional_timeout_s, state="attached"
        )
        if found is None:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                "odds not found inside the selection (selector 'selection_odds')",
            )
        text = _text(found.first)
        if not any(ch.isdigit() for ch in text):
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE, f"no odds shown for the selection ({text!r}) - suspended?"
            )
        try:
            value = parse_polish_decimal(text)
        except ValueError:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR, f"could not parse the displayed odds {text!r}"
            ) from None
        if value <= 1:
            raise _Abort(PlacementStatus.SELECTOR_ERROR, f"implausible displayed odds {text!r}")
        return value

    def _confirm_pick(self, market: Locator, selection: Locator, order: BetOrder) -> Decimal:
        """Re-read the chosen market and selection right before the click; return the current odds.

        The locators are positional (n-th market box, n-th outcome in it). If the page re-rendered since
        the elements were chosen, the position may hold something else: abort without clicking. A
        vanished element makes the reads time out, which :meth:`place` reports as ``FAILED``.
        """
        title_el = self.selectors.probe(market, "market_title", state="attached")
        title = _text(title_el.first) if title_el is not None else ""
        candidate = self._selection_candidates([selection])[0]
        if (
            normalise_text(title) != normalise_text(order.market_raw_name)
            or normalise_text(order.selection_raw_name) not in candidate.names
        ):
            raise _Abort(
                PlacementStatus.FAILED,
                f"the event page changed while selecting: the chosen position now shows market {title!r} / "
                f"selection {candidate.display!r} instead of {order.market_raw_name!r} / "
                f"{order.selection_raw_name!r}; nothing was clicked",
            )
        if self._is_locked(selection):
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE, "the selection is suspended / locked on the event page"
            )
        return self._read_selection_odds(selection)

    def _resolve_single(self, page: Page, key: str) -> Locator:
        """The one visible element of ``key``; several matches are ambiguous -> SELECTOR_ERROR."""
        found = self.selectors.resolve(page, key, self.tuning.element_timeout_s)
        if found.count != 1:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"selector '{key}' matches {found.count} visible elements ({found.spec.describe()}); exactly "
                "one is required - make it more specific in config/betclic_selectors.yaml",
                dump_html=True,
            )
        return found.first

    def _read_slip_odds(self, page: Page) -> Decimal | None:
        """Odds of the slip's single selection; ``None`` unless the slip holds exactly one (hidden
        selections included) with readable odds."""
        items = self.selectors.probe(page, "slip_item")
        if items is None or items.count != 1 or self._slip_total(page) != 1:
            return None
        odds_el = self.selectors.probe(items.first, "slip_item_odds", state="attached")
        if odds_el is None:
            return None
        value = try_parse_polish_decimal(_text(odds_el.first))
        return value if value is not None and value > 1 else None

    def _verify_slip_single(self, page: Page, order: BetOrder, displayed: Decimal) -> Decimal:
        """Exactly one slip selection, the one we clicked, with readable odds (returned)."""
        t = self.tuning
        self._open_slip_if_collapsed(page)
        deadline = time.monotonic() + t.slip_update_timeout_s
        while self._slip_count(page) < 1:
            if time.monotonic() >= deadline:
                raise _Abort(
                    PlacementStatus.SELECTOR_ERROR,
                    "the clicked selection did not appear in the bet slip (selector 'slip_item')",
                    dump_html=True,
                )
            page.wait_for_timeout(t.poll_interval_s * 1000)
        page.wait_for_timeout(t.slip_settle_s * 1000)
        count = self._slip_count(page)
        if count != 1:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR, f"the bet slip must hold exactly one selection, found {count}"
            )
        total = self._slip_total(page)
        if total != 1:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"the bet slip holds {total} selections in the page but only 1 is visible (hidden stale "
                "selections would turn the single into a combination bet); refusing to bet. If the page "
                "keeps hidden duplicates of the slip, make 'slip_item' more specific or set "
                "behaviour.count_hidden_slip_items: false in config/betclic_selectors.yaml",
                dump_html=True,
            )
        badge = self.selectors.probe(page, "slip_count_badge")
        if badge is not None:
            numbers = find_polish_decimals(_text(badge.first))
            if len(numbers) == 1 and numbers[0] != 1:
                raise _Abort(
                    PlacementStatus.SELECTOR_ERROR,
                    f"the bet-slip counter shows {numbers[0]} selections, expected 1",
                )
        item = self.selectors.resolve(page, "slip_item", t.optional_timeout_s).first
        label_el = self.selectors.try_resolve(
            item, "slip_item_selection", t.optional_timeout_s, state="attached"
        )
        if label_el is None:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                "cannot read the bet-slip selection (selector 'slip_item_selection')",
            )
        shown = _text(label_el.first)
        if not names_compatible(normalise_text(shown), normalise_text(order.selection_raw_name)):
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"the bet slip shows selection {shown!r}, expected {order.selection_raw_name!r}",
            )
        market_el = self.selectors.probe(item, "slip_item_market", state="attached")
        shown_market = _text(market_el.first) if market_el is not None else ""
        if shown_market and not names_compatible(
            normalise_text(shown_market), normalise_text(order.market_raw_name)
        ):
            if self.selectors.behaviour.require_slip_market_match:
                raise _Abort(
                    PlacementStatus.SELECTOR_ERROR,
                    f"the bet slip shows market {shown_market!r}, expected {order.market_raw_name!r} (the "
                    "click may have landed on another market). If Betclic words the market differently in "
                    "the slip, fix 'slip_item_market' or set behaviour.require_slip_market_match: false "
                    "in config/betclic_selectors.yaml",
                    dump_html=True,
                )
            log.warning(
                "bet-slip market %r differs from the ordered market %r", shown_market, order.market_raw_name
            )
        odds_el = self.selectors.try_resolve(item, "slip_item_odds", t.optional_timeout_s, state="attached")
        if odds_el is None:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR, "cannot read the bet-slip odds (selector 'slip_item_odds')"
            )
        odds_text = _text(odds_el.first)
        slip_odds = try_parse_polish_decimal(odds_text)
        if slip_odds is None or slip_odds <= 1:
            raise _Abort(PlacementStatus.SELECTOR_ERROR, f"could not parse the bet-slip odds {odds_text!r}")
        if slip_odds != displayed:
            log.info("odds changed between the event page (%s) and the bet slip (%s)", displayed, slip_odds)
        return slip_odds

    def _enter_stake(self, page: Page, stake: Decimal) -> None:
        """Type the stake Polish-style ("12,50") into the one stake field and read it back."""
        text = format_polish_decimal(stake)
        self._pacer.pause(0.5)
        field = self._resolve_single(page, "slip_stake_input")
        field.click()
        field.press("ControlOrMeta+A")
        field.press("Backspace")
        field.press_sequentially(text, delay=self._pacer.typing_delay_ms())
        value = field.input_value()
        if try_parse_polish_decimal(value) == stake:
            return
        log.warning("stake field shows %r after typing %r; retrying with fill()", value, text)
        try:
            if (field.get_attribute("type") or "").lower() == "number":
                # <input type="number">: the DOM value is locale-independent ("12.50") by the HTML spec,
                # whatever decimal separator the browser accepts from the keyboard.
                field.fill(format(stake.quantize(Decimal("0.01")), "f"))
            else:
                field.fill(text)
        except PlaywrightError as exc:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"could not enter the stake {text!r} into the stake field: {_short_error(exc)}",
            ) from None
        value = field.input_value()
        if try_parse_polish_decimal(value) != stake:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR, f"the stake field shows {value!r} instead of {text!r}"
            )

    def _verify_stake_field(self, page: Page, stake: Decimal) -> None:
        """Right before the final click: the one stake field must still hold exactly the order stake."""
        field = self._resolve_single(page, "slip_stake_input")
        value = field.input_value()
        if try_parse_polish_decimal(value) != stake:
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"the stake field shows {value!r} right before confirming instead of "
                f"{format_polish_decimal(stake)!r} (the page changed it); nothing was clicked",
                dump_html=True,
            )

    def _check_potential_win(self, page: Page, order: BetOrder, odds: Decimal, *, wait: bool) -> None:
        """Cross-check the stake Betclic understood via the potential win shown in the slip.

        The stake field can read "12,50" while the site computes with 1250 (or 12): the field alone
        cannot reveal that, the displayed potential payout can. It must lie within
        ``potential_win_min_ratio .. potential_win_max_ratio`` of stake x odds (Poland: x0.88 after the
        12% stake tax). Optional: skipped when ``slip_potential_win`` does not resolve or shows no single
        amount. ``wait`` allows the value to catch up with the stake just typed.
        """
        t = self.tuning
        if not self.selectors.has("slip_potential_win"):
            return
        found = self.selectors.try_resolve(page, "slip_potential_win", t.optional_timeout_s if wait else 0)
        if found is None:
            return
        gross = order.stake * odds
        low = gross * Decimal(str(t.potential_win_min_ratio))
        high = gross * Decimal(str(t.potential_win_max_ratio))
        deadline = time.monotonic() + (t.slip_update_timeout_s if wait else 0.0)
        while True:
            text = _text(found.first)
            numbers = find_polish_decimals(text)
            value = numbers[0] if len(numbers) == 1 else None
            if value is not None and low <= value <= high:
                return
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(t.poll_interval_s * 1000)
            again = self.selectors.probe(page, "slip_potential_win")
            if again is None:
                return
            found = again
        if value is None:
            log.warning(
                "cannot read one amount from the potential-win text %r (selector 'slip_potential_win'); "
                "skipping the stake cross-check",
                text,
            )
            return
        raise _Abort(
            PlacementStatus.SELECTOR_ERROR,
            f"the bet slip shows a potential win of {value} for stake {order.stake} at odds {odds} "
            f"(expected {low.quantize(Decimal('0.01'))} to {high.quantize(Decimal('0.01'))}): Betclic may "
            "have read the stake differently; nothing was clicked. If 'slip_potential_win' points at the "
            "wrong element, fix or remove it in config/betclic_selectors.yaml",
            dump_html=True,
        )

    def _slip_message(self, page: Page) -> tuple[str | None, str, Locator | None]:
        """``(category, text, element)`` of the bet-slip message(s), classified with the ``texts`` section.

        Every matched element is classified on its own (a harmless notice next to a real error must not
        hide it), then combined, most decisive first:

        * a known error -> that category - unless a confirmation is displayed as well: contradictory,
          reported as an unknown message (``None``);
        * a confirmation -> ``"bet_accepted"`` (``element`` is the confirming element);
        * an unrecognised message -> ``None`` with its text;
        * only "pending" / "ignore" notices, or nothing -> empty ``text`` (nothing to act on yet).
        """
        found = self.selectors.probe(page, "slip_error")
        if found is None:
            return None, "", None
        classified: list[tuple[str | None, str, Locator]] = []
        for element in found.all(limit=5):
            text = _text(element)
            if not text:
                continue
            category = self.selectors.classify(text, MESSAGE_CATEGORY_ORDER[1:])  # everything but "ignore"
            if category is None and self.selectors.classify(text, ("ignore",)) is not None:
                continue
            classified.append((category, text, element))
        decisive = [(c, t, el) for c, t, el in classified if c != "pending"]
        if not decisive:
            return ("pending" if classified else None), "", None
        joined = " ".join(t for _, t, _ in decisive)
        kinds = {c for c, _, _ in decisive}
        accepted = next((el for c, _, el in decisive if c == "bet_accepted"), None)
        errors = [c for c in MESSAGE_CATEGORY_ORDER if c in kinds and c in CATEGORY_STATUS]
        if errors:
            return (None if accepted is not None else errors[0]), joined, decisive[0][2]
        if accepted is not None:
            return "bet_accepted", joined, accepted
        return None, joined, decisive[0][2]

    def _raise_if_slip_message(self, page: Page) -> None:
        """BEFORE the final click: any bet-slip message stops the attempt (nothing was clicked)."""
        category, text, _ = self._slip_message(page)
        if not text:
            return
        if category == "bet_accepted":
            # A confirmation that is on screen before we click belongs to an earlier bet; left there it
            # would be read as THIS bet's confirmation right after the click.
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"a confirmation message from an earlier bet is still displayed ({text!r}); refusing to "
                "continue (it could be mistaken for this bet's confirmation)",
                dump_html=True,
            )
        if category is None:
            raise _Abort(
                PlacementStatus.FAILED,
                f"Betclic bet slip shows: {text!r} (unknown message; if it is harmless add a phrase to "
                "texts.ignore in config/betclic_selectors.yaml)",
            )
        raise _Abort(CATEGORY_STATUS[category], f"Betclic: {text}")

    # ================================================================== history

    def _open_history(self, page: Page) -> None:
        t = self.tuning
        self._goto(page, self._base_url + "/")
        state = self._login_state(page)
        if state is LoginState.LOGGED_OUT:
            raise LoginRequiredError("not logged in to Betclic - log in again (open_login_window)")
        if state is LoginState.UNKNOWN:
            raise ExecutorError("could not determine the Betclic login state (selectors need an update)")
        if self.selectors.urls.bet_history:
            self._goto(page, self._safe_url(self.selectors.urls.bet_history))
            return
        nav = self.selectors.resolve(page, "nav_my_bets", t.element_timeout_s)
        self._pacer.pause(0.5)
        nav.first.click()
        with contextlib.suppress(PlaywrightTimeoutError):
            page.wait_for_load_state(
                "domcontentloaded", timeout=self.settings.executor.navigation_timeout_s * 1000
            )

    def _parse_history_row(self, row: Locator, now: datetime) -> BookmakerBetRecord | None:
        raw = _text(row)
        if not raw:
            return None

        def field(key: str) -> str:
            el = self.selectors.probe(row, key, state="attached")
            return _text(el.first) if el is not None else ""

        def amount(key: str, pattern_key: str) -> Decimal | None:
            text = field(key)
            if text:
                numbers = find_polish_decimals(text)
                if numbers:
                    return numbers[-1]
            found = first_regex_group(raw, self.selectors.compiled_patterns(pattern_key))
            return try_parse_polish_decimal(found) if found else None

        bet_id_text = field("history_item_bet_id")
        bet_id = self._clean_bet_id(bet_id_text) if bet_id_text else None
        if not bet_id:
            bet_id = first_regex_group(raw, self.selectors.compiled_patterns("bet_id"))
        date_text = field("history_item_date") or first_regex_group(
            raw, self.selectors.compiled_patterns("history_date")
        )
        placed_at = parse_polish_datetime(date_text, now=now) if date_text else None
        status_text = field("history_item_status")
        status = self._classify_history_status(status_text or raw)
        payout = amount("history_item_payout", "history_payout")
        if status is BookmakerBetStatus.OPEN:
            payout = None  # "możliwa wygrana" is a potential, not an actual payout
        elif status is BookmakerBetStatus.LOST and payout is None:
            payout = Decimal("0")
        return BookmakerBetRecord(
            external_bet_id=bet_id,
            placed_at=placed_at,
            event_name=field("history_item_event"),
            market_name=field("history_item_market"),
            selection_name=field("history_item_selection"),
            odds=amount("history_item_odds", "history_odds"),
            stake=amount("history_item_stake", "history_stake"),
            status=status,
            payout=payout,
            raw_text=raw[:1000],
        )

    def _classify_history_status(self, text: str) -> BookmakerBetStatus:
        for key, status in HISTORY_STATUS_ORDER:
            if self.selectors.classify(text, (key,)) is not None:
                return status
        return BookmakerBetStatus.UNKNOWN

    def _clean_bet_id(self, text: str) -> str | None:
        found = first_regex_group(text, self.selectors.compiled_patterns("bet_id"))
        if found:
            return found
        tokens = [tok for tok in _BET_ID_TOKEN.findall(text) if any(ch.isdigit() for ch in tok)]
        return tokens[-1] if tokens else None

    # ================================================================== misc helpers

    def _health_check_event_page(
        self, page: Page, link: ResolvedElement, selectors_ok: dict[str, bool], problems: list[str]
    ) -> None:
        t = self.tuning
        href = link.first.get_attribute("href")
        if not href:
            selectors_ok["event_link"] = False
            problems.append("event link without href (selector 'event_link')")
            return
        try:
            url = self._safe_url(urljoin(page.url, href))
        except ValueError as exc:
            problems.append(str(exc))
            return
        self._goto(page, url)
        market = self.selectors.try_resolve(page, "market_container", t.element_timeout_s)
        selectors_ok["market_container"] = market is not None
        if market is None:
            problems.append("no markets on the event page (selector 'market_container')")
            return
        first_market = market.first
        title = self.selectors.probe(first_market, "market_title", state="attached")
        selectors_ok["market_title"] = title is not None
        selection = self.selectors.try_resolve(first_market, "selection_button", t.optional_timeout_s)
        selectors_ok["selection_button"] = selection is not None
        odds = (
            self.selectors.probe(selection.first, "selection_odds", state="attached")
            if selection is not None
            else None
        )
        selectors_ok["selection_odds"] = odds is not None
        for key in ("market_title", "selection_button", "selection_odds"):
            if not selectors_ok[key]:
                problems.append(f"selector '{key}' did not resolve on {url}")

    def _goto(self, page: Page, url: str) -> None:
        self._pacer.pause(0.5)
        page.goto(url, wait_until="domcontentloaded")
        with contextlib.suppress(PlaywrightTimeoutError):
            page.wait_for_load_state(
                "load", timeout=min(self.settings.executor.navigation_timeout_s, 15.0) * 1000
            )
        self._dismiss_overlays(page)

    def _dismiss_overlays(self, page: Page) -> None:
        behaviour = self.selectors.behaviour
        for enabled, key in (
            (behaviour.dismiss_cookie_banner, "cookie_banner_dismiss"),
            (behaviour.dismiss_popups, "popup_close"),
        ):
            if not enabled:
                continue
            try:
                found = self.selectors.probe(page, key)
                if found is not None:
                    log.info("dismissing overlay '%s'", key)
                    found.first.click()
            except PlaywrightError as exc:
                log.info("could not dismiss overlay '%s': %s", key, _short_error(exc))

    def _safe_url(self, url: str) -> str:
        """Absolute Betclic URL; refuses other hosts (the logged-in profile never leaves Betclic)."""
        if not url:
            raise ValueError("empty URL")
        absolute = self._base_url + url if url.startswith("/") else url
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or _host_key(parts.netloc) != self._base_host:
            raise ValueError(f"refusing to open a non-Betclic URL: {url!r} (base {self._base_url})")
        return absolute

    def _halted(self) -> tuple[bool, str | None]:
        try:
            halted, reason = self._halt_check()
        except Exception as exc:  # fail safe
            log.error("halt check raised %s - treating trading as halted", type(exc).__name__)
            return True, f"halt check failed ({type(exc).__name__})"
        return bool(halted), reason

    def _respect_min_gap(self) -> None:
        if self._last_attempt_end is None:
            return
        wait = self.tuning.min_seconds_between_bets - (time.monotonic() - self._last_attempt_end)
        if wait > 0:
            log.info("pacing: waiting %.1fs before the next placement", wait)
            self._pacer.sleep(wait)

    def _validate_order(self, order: BetOrder) -> str | None:
        if not self.settings.executor.enabled:
            return "executor disabled (settings.executor.enabled = false)"
        if order.stake <= 0:
            return f"stake must be positive (got {order.stake})"
        if order.stake != order.stake.quantize(Decimal("0.01")):
            return f"stake {order.stake} has more than 2 decimal places"
        if order.stake < self.settings.betclic.min_stake:
            return f"stake {order.stake} is below the Betclic minimum {self.settings.betclic.min_stake}"
        if not order.idempotency_key.strip():
            return "the order has no idempotency_key"
        if order.min_acceptable_odds <= 1:
            return f"min_acceptable_odds must be > 1 (got {order.min_acceptable_odds})"
        if order.requested_odds <= 1:
            return f"requested_odds must be > 1 (got {order.requested_odds})"
        if not order.market_raw_name.strip() or not order.selection_raw_name.strip():
            return "market and selection names are required"
        if not order.event_url and not order.external_event_id:
            return "the order has neither an event_url nor an external_event_id"
        if not order.event_url and not _SAFE_EVENT_ID.fullmatch(order.external_event_id):
            return f"unsupported characters in external_event_id {order.external_event_id!r}"
        return None

    def _ensure_open(self) -> None:
        if self._closed:
            raise ExecutorClosedError("this BetclicExecutor was closed")

    def _handle_abort(self, attempt: _Attempt, abort: _Abort) -> PlacementResult:
        status, message, clear_slip = abort.status, abort.message, abort.clear_slip
        definite = status in POST_CLICK_DEFINITE or status is PlacementStatus.PLACED
        if attempt.place_clicked and not definite:
            # After the click only an explicit bookmaker rejection means "no bet"; everything else
            # (a selector that broke, an internal check) leaves the outcome open.
            if status is not PlacementStatus.UNCERTAIN:
                message = f"{message} [after the place click: outcome unknown - reconcile via bet history]"
            status, clear_slip = PlacementStatus.UNCERTAIN, False
        shot = self._screenshot(attempt, status, dump_html=abort.dump_html)
        if clear_slip:
            self._safe_clear_slip()
        return self._result(
            attempt,
            status,
            message,
            placed_odds=abort.placed_odds,
            screenshot_path=shot,
            take_screenshot=False,
        )

    def _screenshot(
        self, attempt: _Attempt, status: PlacementStatus, *, dump_html: bool = False
    ) -> str | None:
        """Screenshot of the working tab (never launches a browser just for this)."""
        page = self._session.current_page()
        if page is None:
            return None
        try:
            directory = self.screenshot_dir
            directory.mkdir(parents=True, exist_ok=True)
            name = screenshot_filename(
                attempt.order.bet_id,
                attempt.order.idempotency_key,
                status.value,
                max_key_len=self.tuning.max_screenshot_key_len,
            )
            path = directory / name
            n = 1
            while path.exists():
                path = directory / name.replace(".png", f"_{n}.png")
                n += 1
            page.screenshot(path=str(path), timeout=10_000)
            if dump_html and self.tuning.save_html_on_selector_error:
                path.with_suffix(".html").write_text(page.content(), encoding="utf-8")
            return str(path)
        except (PlaywrightError, OSError) as exc:
            log.warning("screenshot failed: %s", _short_error(exc))
            return None

    def _result(
        self,
        attempt: _Attempt,
        status: PlacementStatus,
        message: str,
        *,
        placed_odds: Decimal | None = None,
        external_bet_id: str | None = None,
        balance_after: Decimal | None = None,
        screenshot_path: str | None = None,
        take_screenshot: bool = True,
    ) -> PlacementResult:
        if take_screenshot and screenshot_path is None:
            screenshot_path = self._screenshot(attempt, status)
        if attempt.journaled:
            key = attempt.order.idempotency_key
            if attempt.place_clicked and status in (PlacementStatus.PLACED, PlacementStatus.UNCERTAIN):
                # money may be at risk: never click again for this key
                self._attempted[key] = status
            else:  # never clicked after all, or a definite rejection: the bet may be retried
                self._attempted.pop(key, None)
            try:
                self._journal.record_result(key, attempt.order.bet_id, status)
            except JournalError as exc:  # the "click" record stays last: blocked after a restart (safe)
                log.error("%s - key %r stays blocked in the journal", exc, key)
        level = (
            logging.INFO
            if status in (PlacementStatus.PLACED, PlacementStatus.DRY_RUN_OK)
            else logging.WARNING
        )
        if status in (PlacementStatus.UNCERTAIN, PlacementStatus.SELECTOR_ERROR):
            level = logging.ERROR
        log.log(
            level,
            "placement result: bet_id=%s key=%s status=%s %s",
            attempt.order.bet_id,
            attempt.order.idempotency_key,
            status.value,
            message,
        )
        return PlacementResult(
            status=status,
            placed_odds=placed_odds,
            external_bet_id=external_bet_id,
            balance_after=balance_after,
            screenshot_path=screenshot_path,
            message=message,
            at=utcnow(),
        )

    def _load_credentials(self) -> tuple[str, SecretStr, str] | None:
        """(username, password, source) from env settings or the OS keyring; never logged."""
        betclic = self.settings.betclic
        source = betclic.credentials_source
        if source == "none":
            return None
        if source == "env":
            if betclic.username is None or betclic.password is None:
                log.warning("credentials_source=env but CS2BOT_BETCLIC__USERNAME / PASSWORD are not both set")
                return None
            return betclic.username.get_secret_value(), betclic.password, "env"
        try:
            keyring = importlib.import_module("keyring")
        except ImportError:
            log.warning("credentials_source=keyring but the keyring package is not installed")
            return None
        username = betclic.username.get_secret_value() if betclic.username is not None else None
        try:
            if username:
                password = keyring.get_password(betclic.keyring_service, username)
                if not password:
                    log.warning("no password stored in the keyring for service %r", betclic.keyring_service)
                    return None
                return username, SecretStr(str(password)), "keyring"
            credential = keyring.get_credential(betclic.keyring_service, None)
        except Exception as exc:
            log.warning("keyring lookup failed (%s)", type(exc).__name__)
            return None
        if credential is None or not credential.username or not credential.password:
            log.warning("no credential stored in the keyring for service %r", betclic.keyring_service)
            return None
        return str(credential.username), SecretStr(str(credential.password)), "keyring"

    def _prefill_login(self, page: Page, username: str, password: SecretStr, source: str) -> None:
        t = self.tuning
        try:
            user_field = self.selectors.try_resolve(page, "login_username_input", t.element_timeout_s)
            pass_field = self.selectors.try_resolve(page, "login_password_input", t.optional_timeout_s)
            if user_field is None or pass_field is None:
                log.warning("login form fields not found - type the credentials by hand")
                return
            self._pacer.pause(0.3)
            user_field.first.fill(username)
            self._pacer.pause(0.3)
            pass_field.first.fill(password.get_secret_value())
            log.info(
                "pre-filled the Betclic login form from %s (not submitted: finish the login and 2FA by hand)",
                source,
            )
        except PlaywrightError as exc:
            log.warning("could not pre-fill the login form (%s)", type(exc).__name__)


# ====================================================================== module helpers


def _host_key(netloc: str) -> str:
    host = netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _url_mentions_id(url: str, event_id: str) -> bool:
    """Whether ``url`` contains ``event_id`` as a whole id (numeric ids: not as part of a longer number).

    Betclic event URLs end in ``-m<event id>``, so ``3002201001`` must not be found in
    ``...-m30022010015`` nor ``300220100`` in ``...-m3002201001``.
    """
    if not event_id:
        return False
    if event_id.isdigit():
        return re.search(rf"(?<!\d){re.escape(event_id)}(?!\d)", url) is not None
    return event_id in url


def _text(locator: Locator, timeout_ms: float = 5_000) -> str:
    return (locator.inner_text(timeout=timeout_ms) or "").strip()


def _joined_text(found: ResolvedElement, limit: int = 5) -> str:
    return " ".join(t for t in (_text(el) for el in found.all(limit=limit)) if t).strip()


def _attribute_values(locator: Locator, names: list[str]) -> tuple[str, ...]:
    values: list[str] = []
    for name in names:
        value = locator.get_attribute(name, timeout=2_000)
        if value:
            values.append(value)
    return tuple(values)


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first[:300]}" if first else type(exc).__name__
