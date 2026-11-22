# executor: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/executor -q   (browser tests use PLAYWRIGHT_CHROMIUM_EXECUTABLE, default /opt/pw-browsers/chromium, and are skipped if it is missing; '-m "not browser"' runs only the 165 unit tests)` - 222 passed, 0 failed (lint/type clean: True)

## Summary

P9 `cs2bot.executor` is built: it places single pre-match bets on Betclic.pl through a visible Chromium using a dedicated persistent profile. The whole flow passes against a local mock "Betclic-like" site. The real betclic.pl DOM could not be checked (geo-blocked), so every selector in config/betclic_selectors.yaml is a best guess marked `verified: false`. The owner must run `executor health` and a DRY_RUN placement on their PC before betting real money.

What is in the package:
- **Contracts.** `BetExecutor` (place / get_balance / health_check / close) and `AccountExecutor`, which adds is_logged_in, open_login_window and fetch_bet_history. Also `BookmakerBetRecord` / `BookmakerBetStatus` (history rows) and an error hierarchy.
- **BetclicExecutor.** Runs the 11-step flow from the task:
  - halt check, then open the event (by URL or found on the CS2 listing by id), then check login, then clear stale slip selections;
  - find market and selection by exact normalised name, never guessing (ids only break ties; an id that sits on a different name counts as a conflict);
  - read the Polish odds; refuse if below `min_acceptable_odds`;
  - click the selection and verify the slip holds exactly that one selection;
  - type the stake as "12,50" and read it back;
  - dry run stops here: screenshot, clear the slip, never click the final button;
  - just before the click: re-read the slip, then halt check again, after the random pause;
  - click "Postaw zakład". Odds-change dialog: accept only at or above the minimum, otherwise decline. Parse the receipt (bet id, new balance). Known errors map to their status. Anything unclear after the click returns UNCERTAIN and is never retried.
- **Extra safety beyond the brief:**
  - real bets need both `order.dry_run` and `settings.executor.dry_run` to be false;
  - a receipt left over from an earlier bet is closed first, and if it can't be closed the bot refuses to click, so an old bet id is never reported for the new bet;
  - an idempotency key that was PLACED or UNCERTAIN in this process is refused (as UNCERTAIN) if it comes again; definite rejections can be retried;
  - minimum gap between bets (default 20 s);
  - the bot never opens non-Betclic URLs with the logged-in profile;
  - SELECTOR_ERROR saves a screenshot plus the page HTML so selectors can be fixed;
  - "matched fallback" warnings are logged once per element.
- **Profile lock.** A lock file created exclusively, holding PID, host, a token and a heartbeat thread. It counts as stale when the heartbeat is older than a set age, or immediately if it is on this machine and the PID is dead. Release only deletes our own file. The Windows PID check uses ctypes.
- **Login.** `open_login_window` pre-fills credentials from env or keyring (keyring imported lazily) but never submits the form or automates 2FA, then waits for the owner. Credentials are never logged (a test checks this).
- **Other methods.** `fetch_bet_history` parses "Moje zakłady": id, Warsaw dates, odds, stake, status open/won/lost/void/cashout, payout, with "Pokaż więcej" paging. `health_check` covers browser start, login, balance, and key selectors on the listing and on one event page. `get_balance` parses "1 234,56 zł".
- **Selectors.** Pydantic-validated `SelectorSet` loaded from YAML: 54 elements, each with ordered fallbacks (css / role+name / text / xpath, Polish texts). It also holds message phrases and bet-id/history regexes, plus a behaviour flag for whether accepting new odds already submits the bet. The YAML header explains how to update selectors with Chrome DevTools.
- **Helpers.**
  - `FakeExecutor` (scripted results, records every call, simulates balance and history);
  - `ThreadBoundExecutor`, needed because Playwright's sync API is tied to one thread and cannot run inside asyncio;
  - `create_betclic_executor()` builds the production executor with the DB halt check in one call;
  - `db_halt_check` treats any DB failure as "halted";
  - Polish number/date/text utilities and screenshot naming.

Tests (222):
- Unit tests cover parsing, formatting, selector validation, matching, the lock, the fake executor, the halt check and pacing, launch options via a fake Playwright, and the thread wrapper.
- 57 browser tests run headless with executable_path /opt/pw-browsers/chromium against a mock site on 127.0.0.1. Every click on the final button reaches the mock server, so tests can assert it was never clicked (dry run, halt, odds moved).
- Scenarios covered: dry run; placement; stake formatting; stale slip; odds moved before the click and silently in the slip; odds dialog accepted, declined, repeated, and acknowledge-mode; missing and ambiguous market/selection; suspended market; insufficient balance; stake limit; unknown message; login required; halt switched on between steps and during a pause; no confirmation → UNCERTAIN, no retry; idempotency; leftover receipt; broken selectors; history parsing; login prefill (env and keyring); relaunch after the window is closed; profile lock; thread confinement; screenshots.

## Public API

Everything below can be imported from `cs2bot.executor`. Domain types `BetOrder`, `PlacementResult`, `PlacementStatus` and `ExecutorHealth` come from `cs2bot.domain.betting` and are unchanged.

## Recommended wiring (integration engineer)
```python
from cs2bot.executor import create_betclic_executor

executor = create_betclic_executor(settings, session_factory)  # -> ThreadBoundExecutor, browser starts lazily
result: PlacementResult = executor.place(order)                # from any thread; calls run one at a time
executor.close()                                               # on shutdown: closes Chromium, releases the profile lock
```
```python
create_betclic_executor(settings: Settings, session_factory: sessionmaker[Session], *,
    selectors: SelectorSet | None = None, tuning: ExecutorTuning | None = None,
    playwright_factory: Callable[[], Playwright] | None = None) -> ThreadBoundExecutor
```
- The halt check is `db_halt_check(session_factory)`: kill switch or pause in `system_state`, read with a fresh session on every call. If the DB cannot be read, trading counts as halted.
- From async code (FastAPI), call it as `await asyncio.to_thread(executor.place, order)`. Never call `BetclicExecutor` directly inside an asyncio loop.

## Protocols (`cs2bot.executor.base`)
```python
HaltCheck = Callable[[], tuple[bool, str | None]]   # (halted, reason)

class BetExecutor(Protocol):            # runtime_checkable
    def place(self, order: BetOrder) -> PlacementResult: ...
    def get_balance(self) -> Decimal | None: ...
    def health_check(self) -> ExecutorHealth: ...
    def close(self) -> None: ...

class AccountExecutor(BetExecutor, Protocol):   # runtime_checkable
    def is_logged_in(self) -> bool: ...
    def open_login_window(self, timeout_s: float = 600.0) -> bool: ...
    def fetch_bet_history(self, since: datetime) -> list[BookmakerBetRecord]: ...
```
`BetclicExecutor`, `FakeExecutor` and `ThreadBoundExecutor` all implement `AccountExecutor`.

```python
class BookmakerBetStatus(StrEnum): OPEN="open", WON="won", LOST="lost", VOID="void", CASHOUT="cashout", UNKNOWN="unknown"

class BookmakerBetRecord(BaseModel, frozen=True):
    external_bet_id: str | None          # Betclic bet id (None if not readable)
    placed_at: datetime | None = None    # UTC-aware; naive datetimes are rejected
    event_name: str = ""
    market_name: str = ""
    selection_name: str = ""
    odds: Decimal | None = None
    stake: Decimal | None = None
    status: BookmakerBetStatus = UNKNOWN
    payout: Decimal | None = None        # returned amount; None while OPEN; Decimal(0) for LOST if not shown
    raw_text: str = ""                   # row text, for debugging
```
Errors (all are subclasses of `ExecutorError(RuntimeError)`):
- `ExecutorClosedError`: the executor was used after `close()`.
- `ExecutorThreadError`: Playwright was used from a thread other than the one that started the browser.
- `BrowserStartError`: Chromium did not start.
- `LoginRequiredError`
- `ProfileLockedError(path: Path, holder: LockInfo | None)`: another process holds the profile.
- `SelectorConfigError`: the selector YAML is missing or invalid.
- `SelectorNotFoundError(key, tried, *, state, timeout_s)`

## BetclicExecutor (`cs2bot.executor.betclic`)
```python
BetclicExecutor(settings: Settings, *, selectors: SelectorSet | None = None, halt_check: HaltCheck,
    playwright_factory: Callable[[], Playwright] | None = None, sleep: Callable[[float], None] = time.sleep,
    tuning: ExecutorTuning | None = None, rng: random.Random | None = None)
```
Settings it uses:
- `settings.betclic`: `base_url`, `cs2_listing_path`, `browser_profile_dir`, `chromium_executable`, `proxy_url`, `credentials_source`, `username`, `password`, `keyring_service`, `min_stake`, `selectors_file`.
- `settings.executor`: `enabled`, `dry_run`, `headless`, `action_delay_min_s`, `action_delay_max_s`, `navigation_timeout_s`, `screenshot_dir`.

Launch: `launch_persistent_context(user_data_dir=…, headless=…, executable_path=…, locale="pl-PL", timezone_id="Europe/Warsaw", viewport=1440x900, proxy=…)`.

Thread rule: the object is NOT thread-safe; use one thread or the `ThreadBoundExecutor` wrapper. A call from a different thread raises `ExecutorThreadError`. After `close()`, calls raise `ExecutorClosedError`. It also works as a context manager.

Methods:
- `place(order: BetOrder) -> PlacementResult`. The bet is real only if BOTH `order.dry_run` and `settings.executor.dry_run` are False. Statuses:
  - `PLACED`: `external_bet_id` (None if the receipt shows no id; reconcile in that case), `placed_odds`, `balance_after` (None if the header did not update yet), `screenshot_path`.
  - `DRY_RUN_OK`: `placed_odds` = the odds the bet would have had. The final button was never clicked.
  - `UNCERTAIN`: the final button was clicked but no confirmation or error was detected, or an unrecognised message appeared after the click. Also returned (without clicking) when an idempotency key that already got PLACED/UNCERTAIN in this process is placed again. NEVER retry; reconcile with `fetch_bet_history`.
  - No money at risk for all other statuses: `KILL_SWITCH`, `LOGIN_REQUIRED`, `ODDS_MOVED`, `MARKET_UNAVAILABLE`, `INSUFFICIENT_BALANCE`, `REJECTED_BY_BOOKMAKER` (stake limits / rejected), `SELECTOR_ERROR` (a selector did not resolve, or the market/selection is ambiguous; also saves `<screenshot>.html`), `FAILED` (invalid order, executor disabled, browser or lock problem, foreign URL).
  - Definite rejections that happen after the click may be retried later with the same key.
  - Validation: stake > 0 with at most 2 decimals and ≥ `settings.betclic.min_stake`; `min_acceptable_odds > 1`; names present; `event_url`, or `external_event_id` matching `[A-Za-z0-9_.:-]+`.
- `get_balance() -> Decimal | None`: loads the home page and parses the header ("1 234,56 zł" → `Decimal("1234.56")`). Returns None if logged out, unreadable, or the browser/lock fails.
- `is_logged_in() -> bool`: loads the home page and checks the header state.
- `open_login_window(timeout_s: float = 600.0) -> bool`: visible browser on `urls.login`; clicks "Zaloguj się". With `credentials_source` "env" or "keyring" (lazy `import keyring`; `get_password(service, username)` if a username is configured, else `get_credential(service, None)`) it pre-fills the fields but never submits. Returns True when the logged-in indicator appears, False on timeout. If the owner closes the window, it re-checks the saved session.
- `health_check() -> ExecutorHealth`: `ok` requires the browser running, logged in, balance readable, and all checked selectors resolving. `selectors_ok` keys: `login_state`, `header_balance`, `nav_my_bets`, `event_link`, `market_container`, `market_title`, `selection_button`, `selection_odds`. Failures appear in `message`. A lock or browser failure gives `ok=False` and never raises.
- `fetch_bet_history(since: datetime) -> list[BookmakerBetRecord]`: rows from "Moje zakłady" placed at or after `since` (rows with an unknown date are kept). Raises `LoginRequiredError`, `SelectorNotFoundError`, `BrowserStartError`, `ProfileLockedError`.
- `close() -> None`: idempotent.
- Properties: `screenshot_dir: Path`, `listing_url: str`.
- `LoginState(StrEnum)`: LOGGED_IN / LOGGED_OUT / UNKNOWN.

Screenshots: one per attempt that reaches the browser, in `settings.executor.screenshot_dir`, named `YYYYMMDDTHHMMSSZ_bet<bet_id>_<idempotency-key>_<status>.png`.

## FakeExecutor (`cs2bot.executor.fake`)
```python
FakeExecutor(script: Iterable[PlacementResult | PlacementStatus | Callable[[BetOrder], PlacementResult]] = (), *,
    balance: Decimal | None = Decimal("1000.00"), logged_in: bool = True, halt_check: HaltCheck | None = None,
    honour_dry_run: bool = True, health: ExecutorHealth | None = None,
    history: Iterable[BookmakerBetRecord] = (), login_succeeds: bool = True)
```
- Attributes: `script` (deque), `balance`, `logged_in`, `history`, `calls: list[tuple[str, Any]]`, `orders`, `results`, `closed`.
- Methods: `queue(*items)`, `settle(external_bet_id, status, payout=None)` (credits the payout), plus the full `AccountExecutor` API.
- Default (no script), checked in order: halt check → KILL_SWITCH; logged out → LOGIN_REQUIRED; `order.dry_run` → DRY_RUN_OK; stake > balance → INSUFFICIENT_BALANCE; otherwise PLACED (id `FAKE-n`, balance reduced, an OPEN record added to history).

## ThreadBoundExecutor (`cs2bot.executor.threaded`)
```python
ThreadBoundExecutor(factory: Callable[[], AccountExecutor], *, thread_name: str = "betclic-executor",
    call_timeout_s: float | None = None)
```
- Runs every call on one worker thread; the wrapped executor is created on its first call. Exceptions are passed back to the caller.
- `started: bool` property; `close()` is idempotent; works as a context manager.
- A long `open_login_window` blocks queued calls until it returns.

## Safety (`cs2bot.executor.safety`)
- `db_halt_check(session_factory: sessionmaker[Session]) -> HaltCheck`
- `never_halted() -> tuple[bool, str | None]`
- `HumanPacer(min_s: float, max_s: float, *, sleep=time.sleep, rng: random.Random | None = None, typing_delay_ms: tuple[int, int] = (60, 180))` with `.pause(scale: float = 1.0) -> float`, `.sleep(seconds: float)`, `.typing_delay_ms() -> float`.

## Selectors (`cs2bot.executor.selectors`, file `config/betclic_selectors.yaml`)
- `load_selectors(path: str | Path | None = None, *, settings: Settings | None = None) -> SelectorSet`. Defaults to `settings.betclic.selectors_file`, resolved with `resolve_path`; raises `SelectorConfigError`.
- `SelectorSet` (pydantic) fields:
  - `version`
  - `urls: SiteUrls(login="/", cs2_listing=None, bet_history=None)`
  - `behaviour: SelectorBehaviour(odds_change_accept_places_bet=True, dismiss_cookie_banner=True, dismiss_popups=True)`
  - `attributes: dict[str, list[str]]`, `texts: dict[str, list[str]]` (normalised at load), `patterns: dict[str, list[str]]`
  - `elements: dict[str, ElementSpec]`
  - Class constants `REQUIRED_ELEMENTS` and `REQUIRED_TEXTS`.
- `SelectorSet` methods:
  - `resolve(root: Page | Locator, key: str, timeout_s: float, *, state: Literal["attached", "visible"] = "visible", params: Mapping[str, str] | None = None, poll_s: float = 0.15) -> ResolvedElement`: polls all specs in priority order until one matches; raises `SelectorNotFoundError` listing every spec tried.
  - `try_resolve(...) -> ResolvedElement | None`, `resolve_all(..., limit=None) -> list[Locator]`, `probe(root, key, *, state="visible", params=None) -> ResolvedElement | None` (one non-waiting pass).
  - `classify(text, categories) -> str | None`, `compiled_patterns(key) -> list[re.Pattern]`, `phrases(key)`, `id_attributes(key)`, `has(key)`, `element(key)`, `from_yaml(path)`.
- Module helper: `resolve(root, selectors: SelectorSet, key, timeout_s, *, state="visible", params=None) -> ResolvedElement`.
- `LocatorSpec`: exactly one of `css`, `role` (+ `name`), `text` or `xpath`; optional `exact`, `has_text`, `note`. `"/regex/i"` marks a regex. `{event_id}` placeholders are filled from `params`. Methods `.build(root, params) -> Locator` and `.describe() -> str`.
- `ElementSpec(description, scope, verified, optional, locators)`.
- `ResolvedElement(key, locator, spec, index, count)` with `.first`, `.used_fallback`, `.all(limit=None)`.

## Text utilities (`cs2bot.executor.textutils`)
- `parse_polish_decimal(text: str | None) -> Decimal`: "1,85", "1 234,56 zł", "1.234,56". Raises ValueError for zero numbers or more than one number.
- `try_parse_polish_decimal(text) -> Decimal | None`; `find_polish_decimals(text) -> list[Decimal]`.
- `format_polish_decimal(value: Decimal | int | str, places: int = 2, *, thousands_sep: str = "") -> str`: `Decimal("12.5")` → "12,50".
- `normalise_text(text: str | None) -> str`: casefold, strip accents (ł → l), unify spaces and dashes, drop brackets, decimal comma → dot, keep +/- signs.
- `parse_polish_datetime(text: str | None, *, now: datetime | None = None, tz: ZoneInfo = WARSAW) -> datetime | None` (returns UTC).
- `screenshot_filename(bet_id: int | str, idempotency_key: str, status: str, at: datetime | None = None, *, max_key_len: int = 60) -> str`.
- Also: `classify_text`, `names_compatible`, `contains_phrase`, `first_regex_group`, `sanitize_filename_part`.

## Lock (`cs2bot.executor.lock`)
- `ProfileLock(profile_dir: Path, *, stale_after_s: float = 600.0, heartbeat_s: float | None = 30.0, filename: str = "cs2bot-executor.lock", clock: Callable[[], datetime] = utcnow)` with `.acquire()`, `.release()`, `.refresh()`, `.read_info() -> LockInfo | None`, `.held`, `.lost`; works as a context manager.
- `LockInfo(pid, host, token, created_at, heartbeat_at)`; `pid_alive(pid: int) -> bool`.

## Tuning (`cs2bot.executor.tuning`)
`ExecutorTuning` is a frozen dataclass of knobs missing from `ExecutorSettings`. Defaults:
- viewport 1440x900
- action_timeout_s 15, element_timeout_s 10, optional_timeout_s 1.5
- login_check_timeout_s 8, login_logged_out_stable_s 1.5, login_poll_s 1
- slip_update_timeout_s 8, confirmation_timeout_s 45, balance_update_wait_s 5
- poll_interval_s 0.2, slip_settle_s 0.3
- max_odds_change_rounds 2, min_seconds_between_bets 20, typing_delay 60–180 ms
- history_max_load_more 10, health_check_event_page True, save_html_on_selector_error True
- lock_stale_after_s 600, lock_heartbeat_s 30

## Session (`cs2bot.executor.session`)
- `build_launch_options(settings, tuning, profile_dir) -> dict[str, Any]`
- `parse_proxy_url(url) -> dict[str, str]` (server / username / password; the URL is never echoed in errors)
- `BrowserSession` (used internally)
- Constants `BROWSER_LOCALE="pl-PL"`, `BROWSER_TIMEZONE="Europe/Warsaw"`

## CLI / integration guidance
- `cs2bot executor login` → `open_login_window()`. If `cs2bot up` already holds the profile it raises `ProfileLockedError`; in that case have the dashboard call `open_login_window` on the running wrapper instead.
- `cs2bot executor health` → `health_check()`; store `health.model_dump(mode="json")` under `db.state.EXECUTOR_HEALTH`.
- `cs2bot executor balance` → `get_balance()`.
- Suggested mapping from result to `bets.status`:
  - PLACED → PLACED (write the ledger stake)
  - DRY_RUN_OK → DRY_RUN
  - UNCERTAIN → keep PLACING and reconcile with `fetch_bet_history` (match by `external_bet_id`, else by selection + stake + `placed_at` ≥ attempt time)
  - LOGIN_REQUIRED → `state.set_paused(..., reason="Betclic login required")`
  - SELECTOR_ERROR → pause + dashboard alert
  - anything else → FAILED

## Known limitations

- All selectors in config/betclic_selectors.yaml are unverified guesses (betclic.pl is geo-blocked here); the mock site was built to match them, so passing tests prove the logic, not the live DOM. Before real bets the owner must run `cs2bot executor health` and one DRY_RUN placement on the PC, fixing selectors by following the DevTools guide in the YAML header.
- How Betclic's odds-change dialog behaves is unknown, so behaviour.odds_change_accept_places_bet defaults to true (accepting submits the bet). If the real site needs a second click, the result is UNCERTAIN (reconciliation finds no bet), never a double bet; set it to false once observed.
- Session cookies without an expiry may be dropped when Chromium restarts. The bot relies on staying alive inside `cs2bot up` and on the owner ticking 'remember me' at the manual login. Cookies are not exported to disk on purpose: that would store an unencrypted session token.
- Bet-history parsing is best effort: dates are read as Warsaw local time, rows without a readable id come back with external_bet_id=None, and status comes from the status element or the configured phrases (UNKNOWN otherwise).
- Message classification after the click (texts.*) can only be as good as the configured Polish phrases. Unknown messages count as UNCERTAIN, which is safe but needs reconciliation.
- The Windows branch of pid_alive (ctypes OpenProcess/GetExitCodeProcess) is only reviewed and type-declared; tests run on Linux, so it has not been executed here.
- If DEBUG=pw:api is set, Playwright's own debug log would print values passed to fill(), including a pre-filled password. Do not enable it together with credentials_source env/keyring.
- Playwright's sync API cannot run inside an asyncio event loop and is tied to one thread. Use ThreadBoundExecutor / create_betclic_executor; a long open_login_window blocks other queued executor calls until it returns.
- The cookie banner is only dismissed via 'reject / necessary only' buttons; the bot never accepts optional cookies for the owner (the owner handles the banner during the first manual login).
- Attempts that never reach the browser (invalid order, duplicate key, executor disabled, profile lock or browser start failure) produce no screenshot. Every attempt that reaches the browser does.
- Browser tests launch Chromium once per test: about 95 s for the 222-test directory on this machine.

## Foundation change requests

- Add the ExecutorTuning knobs to cs2bot.config.ExecutorSettings so they can be configured from .env: lock_stale_after_s, lock_heartbeat_s, element_timeout_s, optional_timeout_s, confirmation_timeout_s, slip_update_timeout_s, min_seconds_between_bets, max_odds_change_rounds, viewport_width/viewport_height, save_html_on_selector_error, health_check_event_page. BetclicExecutor(tuning=...) could then be built from settings.
- Move BookmakerBetRecord and BookmakerBetStatus from cs2bot.executor.base into cs2bot.domain.betting, so settlement/reconciliation code depends on the domain rather than the executor package. The executor would re-export them.
- docs/ARCHITECTURE.md P9: document that BetExecutor includes close(), plus AccountExecutor (is_logged_in, open_login_window, fetch_bet_history), ThreadBoundExecutor, create_betclic_executor(settings, session_factory), and that real bets require both order.dry_run and settings.executor.dry_run to be false.
- Document a PlacementStatus -> BetStatus mapping in cs2bot.domain.betting (e.g. UNCERTAIN keeps bets.status=PLACING until reconciled; DRY_RUN_OK -> DRY_RUN), so the integration and dashboard agree on it.
- config.BetclicSettings.cs2_listing_path comment still says 'confirm on the VPS'; the runtime is now the owner's PC.

## New dependency requests

- (none)
