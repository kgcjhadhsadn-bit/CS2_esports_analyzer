"""Betclic.pl bet placement through a visible browser with a dedicated, persistent profile.

The owner logs in by hand once (incl. SMS / 2FA) via :meth:`BetclicExecutor.open_login_window`; the
profile keeps the session. :meth:`BetclicExecutor.place` then places single pre-match bets:

0. the idempotency key was already clicked (this process or the placement journal) -> ``UNCERTAIN``,
   before anything else (and again once the profile lock is held); then the order is validated
   (``FAILED``; started event -> ``MARKET_UNAVAILABLE``)
1. halt check (kill switch / pause)                -> ``KILL_SWITCH``
2. open the event page (``order.event_url`` or find the event on the CS2 listing by its id)
3. logged in?                                      -> ``LOGIN_REQUIRED``
4. clear stale bet-slip selections
5. find the market and the selection by normalised name (ids only disambiguate; never guess); an event
   showing the live marker is refused (pre-match only)
6. read the displayed odds (``"1,85"``); below ``min_acceptable_odds`` (or implausibly far above the
   requested odds) -> ``ODDS_MOVED``; after the human-like pause the chosen elements are read AGAIN
   (they are addressed by position, and a live page may re-render) before the click
7. click the selection; the slip must hold exactly that one selection (hidden ones count), showing the
   ordered selection and market, with acceptable odds; a receipt (or a lower balance) right after that
   click means a quick-bet mode placed a bet -> ``UNCERTAIN``
8. type the stake Polish-style (``"12,50"``) and verify the field; the potential win shown in the slip
   must be consistent with stake x odds (required unless ``behaviour.require_potential_win`` is off);
   locate the one, enabled final button
9. dry run -> screenshot, clear the slip, ``DRY_RUN_OK`` (the final button is located, never clicked)
10. make sure the button is clickable, then re-read the slip (still one selection, odds still
    acceptable, stake field still equal to the order stake, potential win, no message, no receipt),
    verify the profile lock, write the write-ahead journal record, then the halt check again,
    immediately before the click
11. click "Postaw zakład"; handle the odds-changed dialog (accept only at >= min odds, read again right
    before the accept click, at most once per dialog); wait for the receipt (bet id, new balance) ->
    ``PLACED`` - a receipt counts only with a confirmation phrase or a readable bet id (an empty /
    unrecognised one is inconclusive); known errors -> their status, unless a receipt or a lower
    balance contradicts them;
    anything unclear after the click -> ``UNCERTAIN`` (never retried; reconcile via
    :meth:`BetclicExecutor.fetch_bet_history`).

Fail safe and loud: every lookup goes through ``config/betclic_selectors.yaml``; a selector that does not
resolve aborts with ``SELECTOR_ERROR`` (plus screenshot and page HTML) before any money is at risk.
Real bets need BOTH ``order.dry_run`` and ``settings.executor.dry_run`` to be false. Each attempt is
screenshotted into ``settings.executor.screenshot_dir``. No stealth / anti-detection tricks: a real,
visible Chromium with random human-like pauses and a minimum gap between bets.

Never twice: a key whose final button was clicked with a ``PLACED`` / ``UNCERTAIN`` outcome is refused
from then on - also after a restart or crash, through the write-ahead
:class:`~cs2bot.executor.journal.PlacementJournal` kept in the browser-profile directory. "Definitely
not placed" is only ever answered when nothing on the page contradicts it: the caller may stake the same
money again under a new key, which no per-key memory can prevent.

Not thread-safe: use one thread (or :class:`~cs2bot.executor.threaded.ThreadBoundExecutor`).
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import ElementHandle, Locator, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from cs2bot.config import Settings
from cs2bot.domain.betting import BetOrder, ExecutorHealth, PlacementResult, PlacementStatus
from cs2bot.domain.control import LoginAttempt, LoginStatus
from cs2bot.executor.base import (
    EXECUTOR_UNAVAILABLE_PREFIX,
    BookmakerBetRecord,
    BookmakerBetStatus,
    BrowserStartError,
    ExecutorClosedError,
    ExecutorError,
    ExecutorThreadError,
    HaltCheck,
    LoginRequiredError,
)
from cs2bot.executor.journal import JOURNAL_FILENAME, JournalError, PlacementJournal
from cs2bot.executor.lock import ProfileLockedError
from cs2bot.executor.matching import Candidate, MatchKind, pick_candidate
from cs2bot.executor.safety import HumanPacer
from cs2bot.executor.selectors import ResolvedElement, SelectorNotFoundError, SelectorSet, load_selectors
from cs2bot.executor.session import BrowserSession, PlaywrightFactory, effective_headless
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
from cs2bot.utils.time import WARSAW, ensure_utc, utcnow

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
#: Classification order of a login-form message after an automatic submit (``texts`` section).
LOGIN_MESSAGE_ORDER: tuple[tuple[str, LoginStatus], ...] = (
    ("login_account_locked", LoginStatus.ACCOUNT_LOCKED),
    ("login_bad_credentials", LoginStatus.BAD_CREDENTIALS),
    ("login_2fa", LoginStatus.TWO_FA_REQUIRED),
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


@dataclass(frozen=True)
class _AcceptedDialog:
    """An odds-change dialog whose accept button was clicked (see ``_is_new_odds_dialog``)."""

    button: ElementHandle | None  # the element that was clicked (None: no handle could be taken)
    odds: Decimal  # the price that was accepted


@dataclass
class _Attempt:
    order: BetOrder
    dry_run: bool
    journaled: bool = False  # the write-ahead "click" record exists (set right before the final click)
    place_clicked: bool = False
    odds: Decimal | None = None  # latest known odds of the selection
    pre_balance: Decimal | None = None  # header balance before the slip was touched (None = unreadable)


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
        self._base_scheme = urlsplit(self._base_url).scheme.lower()
        self._base_host = _host_key(urlsplit(self._base_url).netloc)
        self._attempted: dict[str, PlacementStatus] = {}  # keys whose final click may have placed a bet
        self._journal = PlacementJournal(
            self._session.profile_dir / JOURNAL_FILENAME, retention_days=self.tuning.journal_retention_days
        )
        # Compacted only once this process holds the profile lock (see place()): a second process that
        # merely constructs an executor must never rewrite the journal the running bot appends to.
        self._journal_compacted = False
        self._last_attempt_end: float | None = None
        self._closed = False
        # Set from ANY thread (abort_login) to end a waiting open_login_window, e.g. at shutdown.
        self._login_abort = threading.Event()

    # ================================================================== public API

    @property
    def screenshot_dir(self) -> Path:
        return resolve_path(self.settings.executor.screenshot_dir)

    @property
    def listing_url(self) -> str:
        return self._safe_url(self.selectors.urls.cs2_listing or self.settings.betclic.cs2_listing_path)

    def place(self, order: BetOrder) -> PlacementResult:
        """Place one single bet (see the module docstring for the exact flow and statuses).

        Raises only :class:`ExecutorClosedError` / :class:`ExecutorThreadError` (programming errors);
        every other problem is a result. An unexpected exception is ``FAILED`` while the final button
        was not clicked and ``UNCERTAIN`` (key blocked, never retried) once it may have been.
        """
        self._ensure_open()
        self._session.check_thread()
        attempt = _Attempt(order=order, dry_run=order.dry_run or self.settings.executor.dry_run)
        try:
            return self._place(attempt)
        except (ExecutorClosedError, ExecutorThreadError):
            raise
        except Exception as exc:  # last line of defence: the caller must always get an answer
            log.exception("unexpected error while placing bet %s", order.bet_id)
            if attempt.place_clicked:
                self._attempted[order.idempotency_key] = PlacementStatus.UNCERTAIN
                return self._result(
                    attempt,
                    PlacementStatus.UNCERTAIN,
                    f"unexpected error after the place click ({_short_error(exc)}) - do NOT retry; "
                    "reconcile via bet history",
                    take_screenshot=False,
                )
            return self._result(
                attempt,
                PlacementStatus.FAILED,
                f"unexpected error before the place click ({_short_error(exc)}); nothing was clicked",
                take_screenshot=False,
            )

    def _place(self, attempt: _Attempt) -> PlacementResult:
        order = attempt.order
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
        # First of all: a key whose final button was already clicked is answered UNCERTAIN, whatever
        # else is wrong with the order by now (executor disabled, event started, trading halted). Any
        # "no bet" status for such a key would tell the caller that a bet which may exist does not.
        refusal = self._refuse_if_already_clicked(attempt)
        if refusal is not None:
            return refusal
        problem = self._validate_order(order)
        if problem is not None:
            return self._result(attempt, PlacementStatus.FAILED, problem, take_screenshot=False)
        started = _event_started(order)
        if started is not None:
            return self._result(attempt, PlacementStatus.MARKET_UNAVAILABLE, started, take_screenshot=False)
        halted, reason = self._halted()
        if halted:  # checked before the pacing gap: a halted bot answers at once
            return self._result(attempt, PlacementStatus.KILL_SWITCH, f"trading halted: {reason}")
        self._respect_min_gap()
        try:
            try:
                page = self._session.ensure_page()
                self._session.ensure_lock()
            except (BrowserStartError, ProfileLockedError) as exc:
                return self._result(
                    attempt,
                    PlacementStatus.FAILED,
                    f"{EXECUTOR_UNAVAILABLE_PREFIX}{exc}",
                    take_screenshot=False,
                )
            except (OSError, PlaywrightError) as exc:  # e.g. the lock file cannot be created (Windows)
                return self._result(
                    attempt,
                    PlacementStatus.FAILED,
                    f"{EXECUTOR_UNAVAILABLE_PREFIX}could not start the browser / take the profile lock: "
                    f"{_short_error(exc)}",
                    take_screenshot=False,
                )
            if not self._journal_compacted:  # we hold the profile lock: we are the journal's only writer
                self._journal_compacted = True
                self._journal.compact()  # best effort, never raises
            # Again, now that we hold the profile lock: the first check ran before it was taken (and
            # before the pacing gap), so another process that held the profile meanwhile (``cs2bot bet
            # run`` next to ``cs2bot up``) may have clicked this key in between.
            refusal = self._refuse_if_already_clicked(attempt)
            if refusal is not None:
                return refusal
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
        except (BrowserStartError, ProfileLockedError, PlaywrightError, OSError) as exc:
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
        if effective_headless(self.settings):
            log.warning(
                "the browser runs headless: the login window is invisible (set "
                "CS2BOT_EXECUTOR__HEADLESS=false; on a server use Xvfb / VNC, see docs/OPERATIONS.md)"
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
            if self._login_abort.is_set():
                log.warning("waiting for the Betclic login was aborted (shutdown)")
                return False
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

    def abort_login(self) -> None:
        """Thread-safe: a waiting :meth:`open_login_window` returns ``False`` at its next poll.

        The only method that may be called from another thread: :class:`ThreadBoundExecutor` calls it on
        ``close()`` so that shutting down does not wait up to ``timeout_s`` for a login window. Once
        called it stays in force (the executor is about to be closed).
        """
        self._login_abort.set()

    def auto_login(self, timeout_s: float | None = None) -> LoginAttempt:
        """Fill AND submit the Betclic login form with the saved credentials (cloud autonomy).

        Only with ``betclic.credentials_source`` env/keyring (``NO_CREDENTIALS`` otherwise). Outcomes:
        ``ALREADY_LOGGED_IN`` / ``LOGGED_IN``; ``TWO_FA_REQUIRED`` when Betclic asks for an SMS / 2FA code
        (the bot stops right there: no code is ever read, typed or bypassed); ``BAD_CREDENTIALS`` /
        ``ACCOUNT_LOCKED`` from the form's error message (the caller stops retrying);
        ``FORM_NOT_FOUND`` when the fields cannot be found; ``FAILED`` for anything else (no outcome
        within ``timeout_s``, default ``executor.login_submit_timeout_s``, or a browser error).
        Credentials are never logged, and no screenshot is taken of the filled form.
        """
        self._ensure_open()
        wait_s = self.settings.executor.login_submit_timeout_s if timeout_s is None else timeout_s
        secrets: tuple[str, ...] = ()
        submitted = False
        try:
            page = self._session.ensure_page()
            self._goto(page, self._safe_url(self.selectors.urls.login))
            if self._login_state(page) is LoginState.LOGGED_IN:
                return self._login_attempt(LoginStatus.ALREADY_LOGGED_IN, "already logged in")
            credentials = self._load_credentials()
            if credentials is None:
                return self._login_attempt(
                    LoginStatus.NO_CREDENTIALS,
                    "no Betclic credentials configured (betclic.credentials_source env/keyring with "
                    "username and password)",
                )
            username, password, source = credentials
            secrets = (password.get_secret_value(), username)
            t = self.tuning
            if self.selectors.probe(page, "login_username_input") is None:
                button = self.selectors.try_resolve(page, "login_button", t.element_timeout_s)
                if button is not None:
                    self._pacer.pause(0.5)
                    button.first.click()
            user_field = self.selectors.try_resolve(page, "login_username_input", t.element_timeout_s)
            pass_field = self.selectors.try_resolve(page, "login_password_input", t.optional_timeout_s)
            if user_field is None or pass_field is None:
                return self._login_attempt(
                    LoginStatus.FORM_NOT_FOUND,
                    "login form fields not found (selectors 'login_username_input' / 'login_password_input')",
                )
            self._pacer.pause(0.3)
            user_field.first.fill(username)
            self._pacer.pause(0.3)
            pass_field.first.fill(password.get_secret_value())
            self._pacer.pause(0.4)
            submit = self.selectors.try_resolve(page, "login_submit_button", t.optional_timeout_s)
            submitted = True  # counted from here on, even if the click itself fails (conservative)
            if submit is not None:
                submit.first.click()
            else:
                pass_field.first.press("Enter")
            log.info("submitted the Betclic login form (credentials from %s)", source)
            attempt = self._await_login_outcome(page, wait_s)
        except Exception as exc:  # any failure is an outcome here, never an exception
            # Playwright's call log (``fill("<value>")``) can carry the typed password; _short_error keeps
            # the first line only and _scrub removes the credentials from whatever is left
            attempt = self._login_attempt(LoginStatus.FAILED, f"browser error: {_short_error(exc)}")
        attempt = attempt.model_copy(
            update={"message": _scrub(attempt.message, secrets), "submitted": submitted}
        )
        log.log(
            logging.INFO if attempt.status.logged_in else logging.WARNING,
            "Betclic auto-login: %s (%s)",
            attempt.status.value,
            attempt.message,
        )
        return attempt

    def _await_login_outcome(self, page: Page, timeout_s: float) -> LoginAttempt:
        """Poll the page after a login submit: logged in, an SMS / 2FA step or an error message."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if self.selectors.probe(page, "logged_in_indicator") is not None:
                return self._login_attempt(LoginStatus.LOGGED_IN, "logged in")
            if self.selectors.probe(page, "login_2fa_step") is not None:
                return self._login_attempt(
                    LoginStatus.TWO_FA_REQUIRED,
                    "Betclic asks for an SMS / 2FA code - the bot stops here; log in once by hand",
                )
            error = self.selectors.probe(page, "login_error")
            if error is not None:
                text = " ".join(t for t in (_soft_text(el) for el in error.all(limit=5)) if t).strip()
                if text:
                    for category, status in LOGIN_MESSAGE_ORDER:
                        if self.selectors.classify(text, [category]) is not None:
                            return self._login_attempt(status, f"Betclic says: {text[:200]}")
                    return self._login_attempt(LoginStatus.FAILED, f"login error: {text[:200]}")
            if time.monotonic() >= deadline:
                return self._login_attempt(
                    LoginStatus.FAILED, f"no login outcome within {timeout_s:.0f}s after submitting the form"
                )
            page.wait_for_timeout(self.tuning.poll_interval_s * 1000)

    @staticmethod
    def _login_attempt(status: LoginStatus, message: str) -> LoginAttempt:
        return LoginAttempt(status=status, message=message, at=utcnow())

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
        except (OSError, PlaywrightError) as exc:  # e.g. the lock file cannot be created
            return ExecutorHealth(ok=False, message=f"browser: {_short_error(exc)}", checked_at=utcnow())
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
        """Bets from "Moje zakłady" that may have been placed at/after ``since``.

        The history shows the placement time to the minute ("22.11.2026 18:30") or only the day, and the
        parsed value is the START of that minute / day. A bet placed at 18:30:40 must still be returned
        for ``since = 18:30:20`` (reconciling an ``UNCERTAIN`` placement asks exactly that), so a row is
        kept when its minute (or day) ENDS at/after ``since``. Rows with an unknown date are kept too:
        returning a row too many is harmless, missing the bet would make it look "not placed".

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
            dates = [_latest_possible(r.placed_at) for r in records if r.placed_at is not None]
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
        return [r for r in records if r.placed_at is None or _latest_possible(r.placed_at) >= since_utc]

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
        pre_balance = attempt.pre_balance = self._read_balance(page, t.optional_timeout_s)
        self._dismiss_stale_receipt(page, strict=False)
        self._clear_slip(page, strict=True)

        market = self._find_market(page, order)
        if self.selectors.probe(page, "event_live_indicator") is not None:
            # Pre-match bets only (PLAN.md): a match that already started is priced by a model that
            # knows nothing about the score.
            raise _Abort(
                PlacementStatus.MARKET_UNAVAILABLE,
                "the event is live (in play): only pre-match bets are placed "
                "(selector 'event_live_indicator')",
            )
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
        receipt_before = self.selectors.probe(page, "bet_receipt") is not None
        # Clicking an outcome normally only fills the slip, but with a quick-bet account mode it places a
        # bet by itself; the last halt check may be a pacing gap and a page load old.
        halted, reason = self._halted()
        if halted:
            raise _Abort(PlacementStatus.KILL_SWITCH, f"trading halted before clicking the outcome: {reason}")
        selection.click()
        try:
            slip_odds = self._verify_slip_single(page, order, displayed)
        except _Abort:
            self._raise_if_selection_click_placed_a_bet(page, attempt, receipt_before, check_balance=True)
            raise
        # (a receipt showing up right after an outcome was clicked is never normal, slip or no slip)
        self._raise_if_selection_click_placed_a_bet(page, attempt, receipt_before, check_balance=False)
        attempt.odds = slip_odds
        self._require_acceptable_odds(slip_odds, order, "bet-slip odds")
        self._enter_stake(page, order.stake)
        self._raise_if_slip_message(page)
        self._check_potential_win(page, order, slip_odds, wait=True)

        # The final button is LOCATED for dry runs as well - the smoke test must reveal a broken
        # 'slip_place_button' selector before real money is used - but only the real flow below clicks it.
        short_of_funds = pre_balance is not None and order.stake > pre_balance
        try:
            button = self._resolve_single(page, "slip_place_button")
        except SelectorNotFoundError:
            if short_of_funds:  # e.g. the site swaps the button for a "deposit" button
                raise _Abort(
                    PlacementStatus.INSUFFICIENT_BALANCE,
                    f"no place-bet button is offered and the stake {order.stake} exceeds the balance "
                    f"{pre_balance}",
                ) from None
            raise
        if not button.is_enabled():
            self._raise_if_slip_message(page)
            if short_of_funds:
                raise _Abort(
                    PlacementStatus.INSUFFICIENT_BALANCE,
                    f"the place-bet button is disabled and the stake {order.stake} exceeds the balance "
                    f"{pre_balance}",
                )
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR, "the place-bet button is disabled and no error is shown"
            )
        if attempt.dry_run:
            shot = self._screenshot(attempt, PlacementStatus.DRY_RUN_OK)
            self._clear_slip(page, strict=False)
            return self._result(
                attempt,
                PlacementStatus.DRY_RUN_OK,
                f"dry run: selection found at odds {slip_odds}, stake {format_polish_decimal(order.stake)} "
                f"entered and verified, place-bet button located; the place-bet button was NOT clicked",
                placed_odds=slip_odds,
                screenshot_path=shot,
                take_screenshot=False,
            )

        self._dismiss_stale_receipt(page, strict=True)
        self._pacer.pause()
        # A message or an odds-change dialog that appeared during the pause would also make the
        # actionability check below fail (a dialog covering or disabling the button): report it as what
        # it is, not as a broken selector.
        self._raise_if_slip_message(page)
        # Actionability first: it may wait for the button to stop moving, and nothing slow may sit
        # between the final reads below and the click.
        self._ensure_clickable(page, button)
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
        self._recheck_slip_selection(page, order)
        self._verify_stake_field(page, order.stake)
        self._check_potential_win(page, order, slip_odds, wait=False)
        self._raise_if_slip_message(page)
        if self.selectors.probe(page, "bet_receipt") is not None:  # appeared during the pause
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                "a bet receipt is displayed before the final click; refusing to click 'Postaw zakład' "
                "(it could be mistaken for this bet's confirmation)",
                dump_html=True,
            )
        started = _event_started(order)  # the pauses above (or a queue before them) may have run late
        if started is not None:
            raise _Abort(PlacementStatus.MARKET_UNAVAILABLE, started)
        self._journal_click(attempt)  # write-ahead: durable BEFORE the click, or no click at all
        halted, reason = self._halted()  # immediately before the click (after the human-like pause)
        if halted:
            raise _Abort(PlacementStatus.KILL_SWITCH, f"trading halted right before confirming: {reason}")
        attempt.place_clicked = True  # set BEFORE the click: a failing click must count as "maybe placed"
        log.info("clicking 'Postaw zakład' for bet_id=%s key=%s", order.bet_id, order.idempotency_key)
        self._click_place_button(button, attempt)
        return self._await_outcome(page, attempt, slip_odds, pre_balance)

    def _raise_if_selection_click_placed_a_bet(
        self, page: Page, attempt: _Attempt, receipt_before: bool, *, check_balance: bool
    ) -> None:
        """Make sure the click on the outcome did not place a bet by itself.

        Some bookmaker sites offer a quick-bet / one-click mode in which clicking an outcome places a bet
        at once with a preset stake - the bet slip never shows it. Whether Betclic does is unknown, so
        the visible signs are checked: a receipt that was not there before the click and, when the
        clicked selection did not become the slip's single item (``check_balance``), a header balance
        below its value from before the click. Either one turns the attempt into ``UNCERTAIN`` (also in
        dry-run mode: money may have moved) and blocks the key like a final click would.
        """
        doubt: str | None = None
        try:
            if not receipt_before and self.selectors.probe(page, "bet_receipt") is not None:
                doubt = "a bet receipt appeared"
            elif check_balance and attempt.pre_balance is not None:
                current = self._read_balance(page, 0)
                if current is not None and current < attempt.pre_balance:
                    doubt = f"the account balance dropped from {attempt.pre_balance} to {current}"
        except PlaywrightError as exc:
            log.warning("could not check for a bet placed by the selection click: %s", _short_error(exc))
        if doubt is None:
            return
        key = attempt.order.idempotency_key
        try:
            self._journal.record_click(key, attempt.order.bet_id)
            attempt.journaled = True
        except JournalError as exc:
            log.error("%s - the UNCERTAIN attempt for key %r is only remembered in memory", exc, key)
        self._attempted[key] = PlacementStatus.UNCERTAIN
        attempt.place_clicked = True  # reported and remembered like a click on the final button
        raise _Abort(
            PlacementStatus.UNCERTAIN,
            f"right after clicking the selection {doubt}, although the bot never confirmed a bet: a "
            "quick-bet / one-click mode may have placed a bet with a preset stake. Switch that mode off "
            "in the Betclic account; do NOT retry - reconcile via bet history",
            clear_slip=False,
            dump_html=True,
        ) from None

    def _click_place_button(self, button: Locator, attempt: _Attempt) -> None:
        """The only place where "Postaw zakład" is clicked.

        Never reached in dry-run mode; the guards below make that (and the write-ahead journal record)
        a hard invariant instead of a property of the calling code.
        """
        if attempt.dry_run or not attempt.journaled or not attempt.place_clicked:
            raise RuntimeError("BUG: the place-bet button may only be clicked by a journaled real attempt")
        button.click()

    def _ensure_clickable(self, page: Page, button: Locator) -> None:
        """Actionability check WITHOUT clicking (Playwright ``trial``): visible, stable, enabled, not
        covered. A button that cannot be clicked is a definite "nothing happened", not an UNCERTAIN.

        If the check fails because the bookmaker put a message or an odds-change dialog in the way,
        that answer (e.g. ``ODDS_MOVED``) is reported instead of ``SELECTOR_ERROR``. A promo popup or
        cookie banner that appeared meanwhile is dismissed once (``popup_close`` /
        ``cookie_banner_dismiss``, never the place button) and the check repeated: a transient popup
        must not pause the bot with a SELECTOR_ERROR.
        """
        timeout_s = min(self.tuning.action_timeout_s, self.tuning.element_timeout_s)
        try:
            button.click(trial=True, timeout=timeout_s * 1000)
            return
        except PlaywrightError as exc:
            self._raise_if_slip_message(page)
            log.info("the place-bet button is not clickable (%s); dismissing overlays", _short_error(exc))
        self._dismiss_overlays(page)
        try:
            button.click(trial=True, timeout=timeout_s * 1000)
        except PlaywrightError as exc:
            self._raise_if_slip_message(page)
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"the place-bet button is not clickable (covered by a popup, moving or disabled): "
                f"{_short_error(exc)}; nothing was clicked",
                dump_html=True,
            ) from None

    def _journal_click(self, attempt: _Attempt) -> None:
        """Record the imminent final click durably; without the record the button is not clicked.

        The profile lock is verified first: the journal is only trustworthy while this process is its
        single writer (a process that lost the lock - e.g. broken as stale after the PC slept - must not
        click while another one may be placing with the same profile and journal).
        """
        order = attempt.order
        try:
            lock_ok = self._session.lock.verify()
        except OSError as exc:  # pragma: no cover - verify() swallows its own I/O errors
            log.warning("could not verify the profile lock: %s", _short_error(exc))
            lock_ok = False
        if not lock_ok:
            raise _Abort(
                PlacementStatus.FAILED,
                f"the browser-profile lock {self._session.lock.path} is no longer held by this process; "
                "refusing to click 'Postaw zakład' (another process may be using the profile)",
                clear_slip=False,  # hands off the page: it may not be ours alone any more
            )
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
        # Set by an accept click: (that dialog's accept button, the odds accepted). The SAME dialog still
        # on screen (a slow server, no spinner) must never be accepted a second time - that would send
        # the bet twice. See _is_new_odds_dialog for how a new dialog is told from a lingering one.
        accepted: _AcceptedDialog | None = None
        lingering = False
        while True:
            receipt = self.selectors.probe(page, "bet_receipt")
            # (None = it vanished between being found and being read - the page is re-rendering; the
            # next pass looks again. A read that fails must not end a placement that went through.)
            receipt_text = _soft_text(receipt.first) if receipt is not None else None
            if receipt is not None and receipt_text is not None:
                if self._receipt_confirms(receipt.first, receipt_text):
                    return self._on_receipt(page, attempt, receipt.first, receipt_text, odds, pre_balance)
                # "przyjęty, oczekuje na akceptację": the bookmaker may still refuse it; the receipt box
                # carries a rejection; or it is still rendering (empty, a spinner). Keep waiting for the
                # final word; without one the attempt ends UNCERTAIN (reconciled via bet history) -
                # never PLACED, never a definite "no bet".
                if receipt_text:
                    unknown_message = receipt_text
            accept, dialog_open = self._odds_dialog(page)
            if not dialog_open:
                accepted = None  # the dialog we accepted is gone: a later one is a new one
            lingering = dialog_open and accepted is not None and not self._is_new_odds_dialog(page, accepted)
            if dialog_open and not lingering:
                if accept is None:
                    # Several buttons fit the accept locator and none of them sits inside the notice
                    # (e.g. a cookie banner's "Akceptuj wszystkie"): never guess which one to press.
                    self._decline_odds_change(page)
                    raise _Abort(
                        PlacementStatus.ODDS_MOVED,
                        "Betclic reported changed odds but the accept button is ambiguous (several "
                        "buttons match 'odds_changed_accept' outside the notice); nothing was accepted - "
                        "make the locator more specific in config/betclic_selectors.yaml",
                        dump_html=True,
                    )
                rounds += 1
                accepted = self._handle_odds_change(page, attempt, accept, rounds)
                odds = accepted.odds
                reclick_pending = not accept_places
                deadline = time.monotonic() + t.confirmation_timeout_s
                unknown_message = ""
                odds_text_since = None
                continue
            category, text, message = self._slip_message(page)
            if category == "bet_accepted" and message is not None:
                return self._on_receipt(page, attempt, message, text, odds, pre_balance)
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
                if lingering:
                    raise _Abort(
                        PlacementStatus.UNCERTAIN,
                        "the odds-change dialog is still displayed after the new odds were accepted (the "
                        "accept button was clicked once and is never clicked again) and no confirmation "
                        f"appeared within {t.confirmation_timeout_s:g}s - do NOT retry; reconcile via bet "
                        "history",
                        clear_slip=False,
                    )
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

    def _receipt_confirms(self, receipt: Locator, text: str) -> bool:
        """Whether a displayed ``bet_receipt`` confirms the bet (``PLACED`` debits the ledger).

        The receipt locators include plain containers (``.betslip_receipt``, ``.betslip_confirmation``)
        that may be displayed before the bookmaker has answered (empty, a spinner, "Ładowanie") or carry
        a refusal. So a receipt confirms only on POSITIVE evidence:

        * a confirmation phrase (``texts.bet_accepted``) - it wins over incidental error words ("Zakład
          przyjęty - kurs uległ zmianie": the bet was taken at the changed odds) but not over a
          "pending approval" text;
        * otherwise a readable bet id (``receipt_bet_id`` or ``patterns.bet_id``) and no known error
          phrase.

        Anything else (empty text, an unknown text without a bet id, an error phrase) is inconclusive:
        the caller keeps waiting and, without a final word, reports ``UNCERTAIN``.
        """
        if not normalise_text(text):
            return False
        if self.selectors.classify(text, ("pending",)) is not None:
            return False
        if self.selectors.classify(text, ("bet_accepted",)) is not None:
            return True
        errors = tuple(c for c in MESSAGE_CATEGORY_ORDER if c in CATEGORY_STATUS)
        if self.selectors.classify(text, errors) is not None:
            return False
        return self._receipt_bet_id(receipt, text) is not None

    def _receipt_bet_id(self, receipt: Locator, text: str) -> str | None:
        """The Betclic bet id shown on a receipt (``receipt_bet_id`` element, else ``patterns.bet_id``)."""
        bet_id: str | None = None
        try:
            id_el = self.selectors.probe(receipt, "receipt_bet_id", state="attached")
            if id_el is not None:
                bet_id = self._clean_bet_id(_soft_text(id_el.first) or "")
        except PlaywrightError:  # the receipt is re-rendering: the patterns below still apply
            bet_id = None
        return bet_id or first_regex_group(text, self.selectors.compiled_patterns("bet_id"))

    def _handle_odds_change(
        self, page: Page, attempt: _Attempt, accept: ResolvedElement, rounds: int
    ) -> _AcceptedDialog:
        """Accept the odds-change dialog if (and only if) the new price is acceptable.

        Returns what was accepted (the button element and the odds). Declines and aborts with
        ``ODDS_MOVED`` when the new price is unreadable, below the minimum, implausibly high or changed
        too often, and with ``KILL_SWITCH`` when trading was halted meanwhile. The price is read twice:
        when the dialog is found and again after the human-like pause, immediately before the accept
        click (it may have dropped further while the dialog was open).
        """
        order = attempt.order
        t = self.tuning

        def acceptable_new_odds() -> Decimal:
            value = self._read_new_odds(page, attempt.odds)
            if value is None:
                self._decline_odds_change(page)
                raise _Abort(
                    PlacementStatus.ODDS_MOVED,
                    "Betclic reported changed odds that could not be read; declined",
                )
            problem = self._odds_problem(value, order)
            if problem is not None:
                self._decline_odds_change(page)
                raise _Abort(PlacementStatus.ODDS_MOVED, f"odds changed: the new odds {problem}; declined")
            return value

        new_odds = acceptable_new_odds()
        if rounds > t.max_odds_change_rounds:
            self._decline_odds_change(page)
            raise _Abort(PlacementStatus.ODDS_MOVED, f"odds changed {rounds} times in a row; declined")
        self._pacer.pause(0.5)
        halted, reason = self._halted()
        if halted:
            self._decline_odds_change(page)
            raise _Abort(PlacementStatus.KILL_SWITCH, f"trading halted while accepting new odds: {reason}")
        latest = acceptable_new_odds()  # again, right before the click
        if latest != new_odds:
            log.info("the offered odds moved from %s to %s while the dialog was open", new_odds, latest)
            new_odds = latest
        log.info(
            "accepting changed odds %s -> %s (minimum %s)", attempt.odds, new_odds, order.min_acceptable_odds
        )
        button: ElementHandle | None = None
        try:  # remember WHICH element we click: it tells a lingering dialog from a new one afterwards
            button = accept.first.element_handle(timeout=t.optional_timeout_s * 1000 + 500)
        except PlaywrightError as exc:
            log.warning("could not keep a handle on the accept button: %s", _short_error(exc))
        accept.first.click()
        attempt.odds = new_odds
        accepted = _AcceptedDialog(button=button, odds=new_odds)
        deadline = time.monotonic() + t.slip_update_timeout_s
        while True:  # give the page a moment to react before the main loop looks at it again
            _, still_open = self._odds_dialog(page)
            if (
                not still_open
                or self._is_new_odds_dialog(page, accepted)
                or self.selectors.probe(page, "bet_receipt") is not None
                or self.selectors.probe(page, "slip_error") is not None
                or time.monotonic() >= deadline
            ):
                break
            page.wait_for_timeout(t.poll_interval_s * 1000)
        return accepted

    def _is_new_odds_dialog(self, page: Page, accepted: _AcceptedDialog) -> bool:
        """An odds-change dialog is on screen and one was accepted before: is this a NEW one?

        Clicking "accept" on the dialog we already accepted (its request still in flight) could send
        the bet a second time, so a dialog only counts as new when BOTH hold:

        * the button element we clicked is no longer displayed (the dialog was closed or re-rendered) -
          a page that keeps the very same element is treated as the same dialog, whatever it shows;
        * the price it offers can be read and differs from the price we accepted (a re-rendered copy of
          the accepted dialog offers the same price).

        Anything else is "the accepted dialog is lingering": no click; without a confirmation the
        attempt ends ``UNCERTAIN``. (A dialog seen after the page showed none at all is always new;
        the caller handles that.)
        """
        if accepted.button is None:
            return False
        try:
            if accepted.button.evaluate("el => el.isConnected") and accepted.button.is_visible():
                return False
        except PlaywrightError:  # the element (or its whole document) is gone
            pass
        offered = self._read_new_odds(page, accepted.odds)
        return offered is not None and offered != accepted.odds

    def _odds_dialog(self, page: Page) -> tuple[ResolvedElement | None, bool]:
        """``(accept button, dialog_open)`` of the odds-change dialog.

        The dialog counts as open while the notice is displayed together with an accept button. ``(None,
        True)`` means it is open but its accept button cannot be told apart (see :meth:`_dialog_button`).
        A notice without any accept button is not a dialog (an inline "odds changed" text).
        """
        if self.selectors.probe(page, "odds_changed_notice") is None:
            return None, False
        accept, ambiguous = self._dialog_button(page, "odds_changed_accept")
        return accept, accept is not None or ambiguous

    def _dialog_button(self, page: Page, key: str) -> tuple[ResolvedElement | None, bool]:
        """``(button, ambiguous)`` for the accept / decline button of the odds-change dialog.

        Other parts of a bookmaker page use the same words ("Akceptuj wszystkie" on a cookie banner, an
        "accept odds changes" setting, "Anuluj" in any modal), so the bot never clicks "the first match":

        1. the button INSIDE the notice element wins;
        2. otherwise (the notice locator may hit only the text element) it is searched inside the bet
           slip, or on the whole page when ``slip_container`` does not resolve - and used only if exactly
           one visible element matches. Several matches are ``(None, True)``.
        """
        notice = self.selectors.probe(page, "odds_changed_notice")
        if notice is not None:
            for root in notice.all(limit=3):
                scoped = self.selectors.probe(root, key)
                if scoped is not None and scoped.count == 1:
                    return scoped, False
        scope: Page | Locator = page
        container = self.selectors.probe(page, "slip_container")
        if container is not None and container.count == 1:
            scope = container.first
        found = self.selectors.probe(scope, key)
        if found is None:
            return None, False
        if found.count == 1:
            return found, False
        return None, True

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
                # Every match counts, the LOWEST wins: a dialog may show "1,85 -> 1,70" with both prices
                # in elements the locator matches (the shipped fallback is any ".oddValue" inside the
                # notice), and the old price first.
                values = [
                    value
                    for value in (try_parse_polish_decimal(_soft_text(el)) for el in element.all(limit=5))
                    if value is not None and value > 1
                ]
                if values:
                    from_notice, explicit = min(values), True
            if from_notice is None:
                numbers = [n for n in find_polish_decimals(_soft_text(notice.first)) if n > 1]
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
        """Best effort: click the dialog's decline button (never a look-alike elsewhere on the page)."""
        try:
            decline, ambiguous = self._dialog_button(page, "odds_changed_decline")
            if decline is not None:
                decline.first.click()
            elif ambiguous:
                log.warning(
                    "several buttons match 'odds_changed_decline' outside the odds-change notice; none "
                    "was clicked (the slip is cleared instead)"
                )
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
        # still the ordered selection (a SELECTOR_ERROR here ends as UNCERTAIN: we are after the click)
        self._recheck_slip_selection(page, order)
        halted, reason = self._halted()
        if halted:
            raise _Abort(
                PlacementStatus.KILL_SWITCH, f"trading halted before re-confirming new odds: {reason}"
            )
        if not button.first.is_enabled():
            return False
        log.info("re-clicking 'Postaw zakład' after accepting new odds (acknowledge-mode dialog)")
        self._click_place_button(button.first, attempt)
        return True

    def _on_receipt(
        self,
        page: Page,
        attempt: _Attempt,
        root: Locator,
        receipt_text: str,
        odds: Decimal,
        pre_balance: Decimal | None,
    ) -> PlacementResult:
        """The bet is confirmed: collect the details.

        ``root`` is the receipt (or the confirming message) and ``receipt_text`` its text as read when
        it was recognised. The details are read tolerantly: a receipt that closes itself while they are
        collected still is a confirmed bet (reported without the parts that could not be read).
        """
        bet_id = self._receipt_bet_id(root, receipt_text)
        placed_odds = odds
        odds_el = self.selectors.probe(root, "receipt_odds", state="attached")
        if odds_el is not None:
            shown = try_parse_polish_decimal(_soft_text(odds_el.first))
            if shown is not None and shown > 1:
                placed_odds = shown
        balance_after: Decimal | None = None
        balance_el = self.selectors.probe(root, "receipt_balance", state="attached")
        if balance_el is not None:
            balance_after = try_parse_polish_decimal(_soft_text(balance_el.first))
        if balance_after is None:
            balance_after = self._wait_balance_change(page, pre_balance)
        shot = self._screenshot(attempt, PlacementStatus.PLACED)
        close = self.selectors.probe(page, "receipt_close")
        if close is not None:
            try:
                self._housekeeping_click(page, close.first, "receipt_close")
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
            text = _soft_text(candidate)
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
        if self._housekeeping_click(page, toggle.first, "slip_open_button"):
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
            if self._housekeeping_click(page, clear.first, "slip_clear_button"):
                confirm = self.selectors.try_resolve(page, "slip_clear_confirm", t.optional_timeout_s)
                if confirm is not None:
                    self._housekeeping_click(page, confirm.first, "slip_clear_confirm")
        else:
            for _ in range(count + 3):
                items = self.selectors.probe(page, "slip_item")
                if items is None:
                    break
                remove = self.selectors.probe(items.first, "slip_item_remove")
                if remove is None:
                    break
                self._pacer.pause(0.3)
                if not self._housekeeping_click(page, remove.first, "slip_item_remove"):
                    break
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
            self._housekeeping_click(page, close.first, "receipt_close")
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
        value = try_parse_polish_decimal(_soft_text(odds_el.first))
        return value if value is not None and value > 1 else None

    def _verify_slip_single(self, page: Page, order: BetOrder, displayed: Decimal) -> Decimal:
        """Exactly one slip selection, the one we clicked, with readable odds (returned)."""
        t = self.tuning
        self._open_slip_if_collapsed(page)
        deadline = time.monotonic() + t.slip_update_timeout_s
        while self._slip_count(page) < 1:
            if time.monotonic() >= deadline:
                # The bookmaker may have refused the selection ("Zakład zawieszony"): that is its
                # answer (MARKET_UNAVAILABLE ...), not a broken selector.
                self._raise_if_slip_message(page)
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
        12% stake tax). ``wait`` allows the value to catch up with the stake just typed.

        With ``behaviour.require_potential_win`` (the default) a potential win that cannot be read - the
        element does not resolve, or its text holds no single amount - stops the attempt with
        ``SELECTOR_ERROR``: an unverifiable stake is never sent. With the flag off the check is skipped
        in those cases (a warning is logged when the text is unreadable).
        """
        t = self.tuning
        required = self.selectors.behaviour.require_potential_win

        def unverifiable(why: str) -> None:
            if not required:
                return
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"{why} (selector 'slip_potential_win'): the bot cannot verify that Betclic understood "
                f"the typed stake {format_polish_decimal(order.stake)!r}, so nothing is placed. Fix the "
                "locator in config/betclic_selectors.yaml; only if the slip really shows no potential "
                "win, and after checking by hand how a typed stake is read, set "
                "behaviour.require_potential_win: false",
                dump_html=True,
            )

        timeout_s = (t.element_timeout_s if required else t.optional_timeout_s) if wait else 0.0
        found = self.selectors.try_resolve(page, "slip_potential_win", timeout_s)
        if found is None:
            unverifiable("the potential win is not shown in the bet slip")
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
                unverifiable("the potential win disappeared from the bet slip")
                return
            found = again
        if value is None:
            unverifiable(f"cannot read one amount from the potential-win text {text!r}")
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
            "wrong element, fix it in config/betclic_selectors.yaml (the accepted range is "
            "ExecutorTuning.potential_win_min_ratio .. potential_win_max_ratio of stake x odds)",
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
            text = _soft_text(element)
            if not text:  # empty, or gone again before it could be read
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
        """BEFORE the final click: any bet-slip message stops the attempt (nothing was clicked).

        So does an odds-change notice / dialog (``ODDS_MOVED``): while Betclic announces another price,
        the odds read from the slip are not necessarily the ones a click on "Postaw zakład" would get
        (the click may even count as accepting the new price). The engine re-prices with fresh odds.
        """
        category, text, _ = self._slip_message(page)
        if not text:
            self._raise_if_odds_notice(page)
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

    def _raise_if_odds_notice(self, page: Page) -> None:
        """BEFORE the final click: an odds-change notice or dialog on screen -> ``ODDS_MOVED``."""
        notice = self.selectors.probe(page, "odds_changed_notice")
        if notice is None:
            return
        shown = (_soft_text(notice.first) or "")[:200]
        raise _Abort(
            PlacementStatus.ODDS_MOVED,
            f"Betclic reports an odds change before the bet was confirmed ({shown!r}); nothing was clicked "
            "- re-price with fresh odds. (If this text is displayed permanently, make 'odds_changed_notice' "
            "more specific in config/betclic_selectors.yaml.)",
        )

    def _recheck_slip_selection(self, page: Page, order: BetOrder) -> None:
        """Right before the final click: the slip's single item still names the ordered selection (and,
        when shown, the ordered market) - the count and the odds alone do not tell WHICH outcome it is."""
        items = self.selectors.probe(page, "slip_item")
        label = (
            self.selectors.probe(items.first, "slip_item_selection", state="attached")
            if items is not None and items.count == 1
            else None
        )
        shown = _soft_text(label.first) if label is not None else None
        if not shown or not names_compatible(normalise_text(shown), normalise_text(order.selection_raw_name)):
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"right before confirming, the bet slip shows selection {shown!r} instead of "
                f"{order.selection_raw_name!r}; nothing was clicked",
                dump_html=True,
            )
        if not self.selectors.behaviour.require_slip_market_match or items is None:
            return
        market_el = self.selectors.probe(items.first, "slip_item_market", state="attached")
        shown_market = _soft_text(market_el.first) if market_el is not None else None
        if shown_market and not names_compatible(
            normalise_text(shown_market), normalise_text(order.market_raw_name)
        ):
            raise _Abort(
                PlacementStatus.SELECTOR_ERROR,
                f"right before confirming, the bet slip shows market {shown_market!r} instead of "
                f"{order.market_raw_name!r}; nothing was clicked",
                dump_html=True,
            )

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
        if status_text:
            status = self._classify_history_status(status_text)
        else:  # no status label: the whole row text, where buttons and promos may use the same words
            status = self._classify_history_status(raw, whole_row=True)
        payout = amount("history_item_payout", "history_payout")
        if status in (BookmakerBetStatus.OPEN, BookmakerBetStatus.UNKNOWN):
            # "możliwa wygrana" / a cash-out offer is a potential, not an actual payout
            payout = None
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

    def _classify_history_status(self, text: str, *, whole_row: bool = False) -> BookmakerBetStatus:
        """Status of a bet-history row from its status label (first phrase in ``HISTORY_STATUS_ORDER``).

        ``whole_row``: the label could not be located and the whole row text is classified. That text
        also holds buttons and notes - an OPEN bet offers "Cash out 9,80 zł", a promo mentions "zwrot" -
        so the row is classified only when the phrases of exactly ONE status occur, else ``UNKNOWN``
        (never guess: a cash-out or void reported for an open bet would settle it wrongly).
        """
        matches = [status for key, status in HISTORY_STATUS_ORDER if self.selectors.classify(text, (key,))]
        if not matches:
            return BookmakerBetStatus.UNKNOWN
        if whole_row and len(set(matches)) > 1:
            log.debug("bet-history row matches several statuses %s; reported as unknown", matches)
            return BookmakerBetStatus.UNKNOWN
        return matches[0]

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
                    self._housekeeping_click(page, found.first, key)
            except PlaywrightError as exc:
                log.info("could not dismiss overlay '%s': %s", key, _short_error(exc))

    def _is_place_button(self, page: Page, candidate: Locator) -> bool:
        """Whether ``candidate`` is, contains or sits inside a final "Postaw zakład" button.

        Fail safe: ``True`` when it cannot be told (the element is not clicked then).
        """
        try:
            if "postaw" in normalise_text(_soft_text(candidate) or ""):
                return True
            place = self.selectors.probe(page, "slip_place_button", state="attached")
            if place is None:
                return False
            handles: list[ElementHandle] = []
            try:
                for el in place.all(limit=5):
                    handles.append(el.element_handle(timeout=1_000))
                return bool(
                    candidate.evaluate(
                        "(el, others) => others.some(o => o === el || el.contains(o) || o.contains(el))",
                        handles,
                        timeout=2_000,
                    )
                )
            finally:
                for handle in handles:
                    with contextlib.suppress(PlaywrightError):
                        handle.dispose()
        except PlaywrightError:
            return True

    def _housekeeping_click(self, page: Page, candidate: Locator, key: str) -> bool:
        """Click a housekeeping element (clear the slip, close a receipt / popup / cookie banner).

        These lookups use unverified, partly page-wide locators ("[role=dialog] button" with "Tak /
        Potwierdź", "Zamknij", "OK" ...). Whatever the YAML says, such a click must never land on the
        final "Postaw zakład" button - least of all in a dry run, which clears a slip that holds the
        selection and the stake. Returns whether it clicked.
        """
        if self._is_place_button(page, candidate):
            log.error(
                "refusing to click '%s': the element is (or contains) the place-bet button - make the "
                "locator more specific in config/betclic_selectors.yaml",
                key,
            )
            return False
        candidate.click()
        return True

    def _safe_url(self, url: str) -> str:
        """Absolute Betclic URL; refuses other hosts (the logged-in profile never leaves Betclic)."""
        if not url:
            raise ValueError("empty URL")
        absolute = self._base_url + url if url.startswith("/") else url
        parts = urlsplit(absolute)
        # Same scheme as the configured site (never https -> http: the session cookie would travel in
        # clear text) and the same host and port.
        if (
            parts.scheme not in ("http", "https")
            or parts.scheme != self._base_scheme
            or _host_key(parts.netloc) != self._base_host
        ):
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
        for name in ("stake", "requested_odds", "min_acceptable_odds"):
            if not getattr(order, name).is_finite():  # NaN / Infinity (comparisons below would raise)
                return f"{name} is not a finite number ({getattr(order, name)})"
        if order.stake <= 0:
            return f"stake must be positive (got {order.stake})"
        try:
            whole_grosze = order.stake == order.stake.quantize(Decimal("0.01"))
        except ArithmeticError:  # InvalidOperation: too many digits for the context (e.g. 1E+30)
            return f"stake {order.stake} is not a valid amount"
        if not whole_grosze:
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
        elif attempt.place_clicked and status in POST_CLICK_DEFINITE:
            doubt = self._post_click_doubt(attempt)
            if doubt is not None:
                log.error(
                    "bet_id=%s: the page reported %s after the place click but %s",
                    attempt.order.bet_id,
                    status.value,
                    doubt,
                )
                message = (
                    f"{message} [reported as {status.value}, but {doubt}: the bet may have been placed - "
                    "do NOT retry; reconcile via bet history]"
                )
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

    def _post_click_doubt(self, attempt: _Attempt) -> str | None:
        """Evidence that a bet exists although the page showed a rejection after the final click.

        A rejection is read from free text matched against configured Polish phrases, on a page nobody
        could verify (an "odds changed" notice may also accompany a bet that was taken at updated odds;
        "niedostępny" may be about cash-out). Reporting such a bet as "definitely not placed" lets the
        caller stake the same money again. So before a post-click rejection is returned, the page is
        watched for ``balance_update_wait_s``: a receipt appearing after all, or the header balance having
        dropped below its value from before the click, turns the result into ``UNCERTAIN``.

        ``None`` = nothing contradicts the rejection (also when the balance cannot be read: the check is
        an extra safety net, it never blocks a real rejection).
        """
        page = self._session.current_page()
        if page is None:
            return None
        before = attempt.pre_balance
        deadline = time.monotonic() + self.tuning.balance_update_wait_s
        try:
            while True:
                if self.selectors.probe(page, "bet_receipt") is not None:
                    return "a bet receipt is displayed"
                if before is not None:
                    current = self._read_balance(page, 0)
                    if current is not None and current < before:
                        return f"the account balance dropped from {before} to {current}"
                if time.monotonic() >= deadline:
                    return None
                page.wait_for_timeout(self.tuning.poll_interval_s * 1000)
        except PlaywrightError as exc:
            log.warning("could not cross-check the rejection against the balance: %s", _short_error(exc))
            return None

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


def _event_started(order: BetOrder) -> str | None:
    """Why the order must not be placed any more: its event has started (pre-match bets only).

    ``BetOrder`` does not carry the kick-off time yet (see the foundation change request in
    docs/dev/reports/executor.md); the check takes effect as soon as an order has a timezone-aware
    ``event_starts_at``. It matters because an order can wait a long time before it is placed: calls are
    queued behind each other, and a manual login window alone may block the queue for ten minutes. An
    ``event_starts_at`` that is not an aware datetime refuses the order (fail safe).
    """
    starts_at = getattr(order, "event_starts_at", None)
    if starts_at is None:
        return None
    if not isinstance(starts_at, datetime) or starts_at.tzinfo is None:
        return "the order's event_starts_at is not a timezone-aware datetime; refusing to place it"
    if utcnow() >= starts_at:
        return (
            f"the event started at {ensure_utc(starts_at):%Y-%m-%d %H:%M:%S}Z: only pre-match bets are placed"
        )
    return None


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


def _latest_possible(placed_at: datetime) -> datetime:
    """The latest instant a bet shown with ``placed_at`` in the history may really have been placed.

    The history displays minutes (or only the day): a parsed time with zero seconds stands for the whole
    minute, local (Warsaw) midnight for the whole day. Times with seconds are taken as they are.
    """
    local = placed_at.astimezone(WARSAW)
    if (local.hour, local.minute, local.second) == (0, 0, 0):
        # The next LOCAL midnight (aware + timedelta is wall-clock arithmetic): the October day on which
        # the clocks go back has 25 hours, and "+24 h in UTC" would end it an hour early.
        return (local + timedelta(days=1)).astimezone(UTC)
    if local.second == 0:
        return placed_at + timedelta(minutes=1)
    return placed_at


def _text(locator: Locator, timeout_ms: float = 5_000) -> str:
    return (locator.inner_text(timeout=timeout_ms) or "").strip()


def _soft_text(locator: Locator, timeout_ms: float = 750) -> str | None:
    """Text of an element that was just found, or ``None`` if it is gone again.

    For polling a page that re-renders under us (messages, the receipt, the odds-change notice, the
    header balance): between "found" and "read" the element may vanish, and Playwright would then wait
    for it and raise. In a polling loop that only means "not there (any more)" - the next pass decides.
    """
    try:
        return (locator.inner_text(timeout=timeout_ms) or "").strip()
    except PlaywrightError:
        return None


def _joined_text(found: ResolvedElement, limit: int = 5) -> str:
    return " ".join(t for t in (_text(el) for el in found.all(limit=limit)) if t).strip()


def _attribute_values(locator: Locator, names: list[str]) -> tuple[str, ...]:
    values: list[str] = []
    for name in names:
        value = locator.get_attribute(name, timeout=2_000)
        if value:
            values.append(value)
    return tuple(values)


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """``text`` with every non-trivial secret (password, username) replaced by ``<redacted>``."""
    for secret in sorted((x for x in secrets if x and len(x) >= 3), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first[:300]}" if first else type(exc).__name__
