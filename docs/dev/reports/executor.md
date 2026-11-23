# executor: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/executor -q -n 2` (browser tests use PLAYWRIGHT_CHROMIUM_EXECUTABLE, default /opt/pw-browsers/chromium, and are skipped if it is missing; `-m "not browser"` runs only the 193 unit tests) - 274 passed, 0 failed (lint/type clean: True). About 1 min 25 s with `-n 2`, 2 min 25 s serially on this machine.

## Summary

P9 `cs2bot.executor` is built: it places single pre-match bets on Betclic.pl through a visible Chromium using a dedicated persistent profile. The whole flow passes against a local mock "Betclic-like" site. The real betclic.pl DOM could not be checked (geo-blocked), so every selector in config/betclic_selectors.yaml is a best guess marked `verified: false`. The owner must run `executor health` and a DRY_RUN placement on their PC before betting real money.

What is in the package:
- **Contracts.** `BetExecutor` (place / get_balance / health_check / close) and `AccountExecutor`, which adds is_logged_in, open_login_window and fetch_bet_history. Also `BookmakerBetRecord` / `BookmakerBetStatus` (history rows, defined in `cs2bot.domain.betting`) and an error hierarchy.
- **BetclicExecutor.** Runs this flow:
  - refuse a key whose final button was already clicked (memory + placement journal), halt check, then open the event (by URL or found on the CS2 listing by id; a redirect to another page is detected), then check login, then clear stale slip selections;
  - find market and selection by exact normalised name, never guessing (ids only break ties; an id that sits on a different name counts as a conflict);
  - read the Polish odds; refuse if below `min_acceptable_odds` or implausibly far above the requested odds;
  - after the human-like pause, read the chosen market title, selection name and odds again (the elements are addressed by position and a live page may re-render), then click;
  - verify the slip holds exactly that one selection (hidden ones count), showing the ordered selection and market;
  - type the stake as "12,50" and read it back; cross-check the potential win when the slip shows one;
  - dry run stops here: screenshot, clear the slip, never click the final button;
  - just before the click: re-read the slip odds and the stake field, make sure the button is clickable (trial click), write the journal record, then the halt check again;
  - click "Postaw zakład". Odds-change dialog: accept only at or above the minimum (the lower of the dialog's and the slip's price counts), otherwise decline. Parse the receipt (bet id, new balance). Known errors map to their status. Anything unclear after the click returns UNCERTAIN and is never retried.
- **Extra safety beyond the brief:**
  - real bets need both `order.dry_run` and `settings.executor.dry_run` to be false;
  - a receipt or confirmation message left over from an earlier bet is closed first, and if it can't be closed the bot refuses to click, so an old bet id is never reported for the new bet;
  - an idempotency key that was PLACED or UNCERTAIN is refused (as UNCERTAIN) if it comes again, also after a restart or crash (write-ahead placement journal in the profile directory); definite rejections can be retried;
  - minimum gap between bets (default 20 s);
  - the bot never opens non-Betclic URLs with the logged-in profile;
  - a lost profile lock stops placements (and closes the browser if another process holds the profile);
  - SELECTOR_ERROR saves a screenshot plus the page HTML so selectors can be fixed;
  - "matched fallback" warnings are logged once per element.
- **Profile lock.** A lock file created exclusively, holding PID, host, a token and a heartbeat thread. It counts as stale when the heartbeat is older than a set age, or immediately if it is on this machine and the PID is dead. Release only deletes our own file. The Windows PID check uses ctypes.
- **Login.** `open_login_window` pre-fills credentials from env or keyring (keyring imported lazily) but never submits the form or automates 2FA, then waits for the owner. Credentials are never logged (a test checks this).
- **Other methods.** `fetch_bet_history` parses "Moje zakłady": id, Warsaw dates, odds, stake, status open/won/lost/void/cashout, payout, with "Pokaż więcej" paging. `health_check` covers browser start, login, balance, key selectors on the listing and on one event page, and the placement journal. `get_balance` parses "1 234,56 zł".
- **Selectors.** Pydantic-validated `SelectorSet` loaded from YAML: 55 elements, each with ordered fallbacks (css / role+name / text / xpath, Polish texts). It also holds message phrases and bet-id/history regexes, plus behaviour flags. The YAML header explains how to update selectors with Chrome DevTools.
- **Helpers.**
  - `FakeExecutor` (scripted results, records every call, simulates balance and history, never places twice for one key);
  - `ThreadBoundExecutor`, needed because Playwright's sync API is tied to one thread and cannot run inside asyncio;
  - `create_betclic_executor()` builds the production executor with the DB halt check and the settings-driven tuning in one call;
  - `db_halt_check` treats any DB failure as "halted";
  - `PlacementJournal` (crash-safe idempotency);
  - Polish number/date/text utilities and screenshot naming.

Tests (274):
- Unit tests cover parsing, formatting, selector validation, matching, the lock, the journal, tuning from settings, the fake executor, the halt check and pacing, launch options via a fake Playwright, and the thread wrapper.
- 81 browser tests run headless with executable_path /opt/pw-browsers/chromium against a mock site on 127.0.0.1. Every click on the final button reaches the mock server, so tests can assert it was never clicked (dry run, halt, odds moved).
- Scenarios covered: dry run; placement; stake formatting; stale slip; odds moved before the click and silently in the slip; odds dialog accepted, declined, repeated, and acknowledge-mode; missing and ambiguous market/selection; suspended market; insufficient balance; stake limit; unknown message; login required; halt switched on between steps and during a pause; no confirmation → UNCERTAIN, no retry; idempotency; leftover receipt; broken selectors; history parsing; login prefill (env and keyring); relaunch after the window is closed; profile lock; thread confinement; screenshots.
- Adversarial scenarios added in review round 1 (`test_executor_browser_safety.py`): page re-render between choosing and clicking a selection; slip showing another market; hidden stale slip selection; stake changed by the page before the final click; stake misread by the site (potential-win cross-check); two stake fields; odds-dialog number contradicting the slip; lingering "odds changed" text after accepting; odds far above the request; covered place button; stale confirmation message; pending-approval receipt; harmless notice next to a real error; restart after UNCERTAIN; crash right at the click; unwritable journal; redirect to another event.

## Public API

Everything below can be imported from `cs2bot.executor`. Domain types `BetOrder`, `PlacementResult`, `PlacementStatus`, `ExecutorHealth`, `BookmakerBetRecord` and `BookmakerBetStatus` come from `cs2bot.domain.betting` and are unchanged.

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
- Tuning is `ExecutorTuning.from_settings(settings)` unless `tuning=` is passed. Inconsistent `CS2BOT_EXECUTOR__*` values raise `ValueError` here, at start-up.
- From async code (FastAPI), call it as `await asyncio.to_thread(executor.place, order)`. Never call `BetclicExecutor` directly inside an asyncio loop.
- Still set `bets.status = PLACING` (committed) before calling `place` and reconcile UNCERTAIN bets with `fetch_bet_history`; the journal is a second line of defence, not a replacement.

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

`BookmakerBetStatus` / `BookmakerBetRecord` are defined in `cs2bot.domain.betting` and re-exported from `cs2bot.executor.base` and `cs2bot.executor`:
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
- `JournalError`: the placement journal could not be read or written.

## BetclicExecutor (`cs2bot.executor.betclic`)
```python
BetclicExecutor(settings: Settings, *, selectors: SelectorSet | None = None, halt_check: HaltCheck,
    playwright_factory: Callable[[], Playwright] | None = None, sleep: Callable[[float], None] = time.sleep,
    tuning: ExecutorTuning | None = None, rng: random.Random | None = None)
```
`tuning=None` means `ExecutorTuning.from_settings(settings)`.

Settings it uses:
- `settings.betclic`: `base_url`, `cs2_listing_path`, `browser_profile_dir`, `chromium_executable`, `proxy_url`, `credentials_source`, `username`, `password`, `keyring_service`, `min_stake`, `selectors_file`.
- `settings.executor`: `enabled`, `dry_run`, `headless`, `action_delay_min_s`, `action_delay_max_s`, `navigation_timeout_s`, `screenshot_dir`, and through `ExecutorTuning.from_settings`: `element_timeout_s`, `optional_timeout_s`, `slip_update_timeout_s`, `confirmation_timeout_s`, `min_seconds_between_bets`, `max_odds_change_rounds`, `viewport_width`, `viewport_height`, `save_html_on_selector_error`, `health_check_event_page`, `lock_stale_after_s`, `lock_heartbeat_s`.

Launch: `launch_persistent_context(user_data_dir=…, headless=…, executable_path=…, locale="pl-PL", timezone_id="Europe/Warsaw", viewport=1440x900, proxy=…)`.

Thread rule: the object is NOT thread-safe; use one thread or the `ThreadBoundExecutor` wrapper. A call from a different thread raises `ExecutorThreadError`. After `close()`, calls raise `ExecutorClosedError`. It also works as a context manager.

Methods:
- `place(order: BetOrder) -> PlacementResult`. The bet is real only if BOTH `order.dry_run` and `settings.executor.dry_run` are False. It raises only `ExecutorClosedError` / `ExecutorThreadError` (programming errors); everything else is a result. Statuses:
  - `PLACED`: `external_bet_id` (None if the receipt shows no id; reconcile in that case), `placed_odds`, `balance_after` (None if the header did not update yet), `screenshot_path`. The message carries a `WARNING:` when the balance did not drop by exactly the stake or the accepted odds are below the minimum.
  - `DRY_RUN_OK`: `placed_odds` = the odds the bet would have had. The final button was never clicked.
  - `UNCERTAIN`: the final button was clicked but no confirmation or definite rejection was detected within `confirmation_timeout_s` (including an unrecognised message, a receipt that says "pending approval", or an "odds changed" text that stays after the new odds were accepted). Also returned, without touching the page, when the key's final button was already clicked with a PLACED/UNCERTAIN outcome in this process or according to the placement journal (earlier run, crash). NEVER retry; reconcile with `fetch_bet_history`.
  - No money at risk for all other statuses: `KILL_SWITCH`, `LOGIN_REQUIRED`, `ODDS_MOVED` (below the minimum, or more than `max_odds_rise_factor` x above the requested odds: re-price), `MARKET_UNAVAILABLE`, `INSUFFICIENT_BALANCE`, `REJECTED_BY_BOOKMAKER` (stake limits / rejected), `SELECTOR_ERROR` (a selector did not resolve; the market/selection is ambiguous; several stake fields or place buttons; the slip shows another market, a hidden extra selection, a changed stake or an implausible potential win; the place button is covered; a stale confirmation is displayed; also saves `<screenshot>.html`), `FAILED` (invalid order, executor disabled, browser or lock problem, foreign URL, the page changed while selecting, unknown slip message before the click, unreadable/unwritable placement journal).
  - After the final click only explicit bookmaker rejections, a declined odds change and a halt before re-confirming count as "no bet"; any other problem after the click is reported as UNCERTAIN.
  - Definite rejections that happen after the click may be retried later with the same key.
  - Dry-run orders are never refused for an already clicked key (they never click).
  - Validation: non-blank `idempotency_key`; stake > 0 with at most 2 decimals and ≥ `settings.betclic.min_stake`; `min_acceptable_odds > 1`; `requested_odds > 1`; names present; `event_url`, or `external_event_id` matching `[A-Za-z0-9_.:-]+`.
- `get_balance() -> Decimal | None`: loads the home page and parses the header ("1 234,56 zł" → `Decimal("1234.56")`). Returns None if logged out, unreadable, or the browser/lock fails.
- `is_logged_in() -> bool`: loads the home page and checks the header state. Raises `BrowserStartError` / `ProfileLockedError` / Playwright errors if the browser cannot be used.
- `open_login_window(timeout_s: float = 600.0) -> bool`: visible browser on `urls.login`; clicks "Zaloguj się". With `credentials_source` "env" or "keyring" (lazy `import keyring`; `get_password(service, username)` if a username is configured, else `get_credential(service, None)`) it pre-fills the fields but never submits. Returns True when the logged-in indicator appears, False on timeout. If the owner closes the window, it re-checks the saved session.
- `health_check() -> ExecutorHealth`: `ok` requires the browser running, logged in, balance readable, all checked selectors resolving and a readable placement journal. `selectors_ok` keys: `login_state`, `header_balance`, `nav_my_bets`, `event_link`, `market_container`, `market_title`, `selection_button`, `selection_odds`. Failures appear in `message`. A lock or browser failure gives `ok=False` and never raises.
- `fetch_bet_history(since: datetime) -> list[BookmakerBetRecord]`: rows from "Moje zakłady" placed at or after `since` (rows with an unknown date are kept; an unreadable row is skipped). Raises `LoginRequiredError`, `SelectorNotFoundError`, `BrowserStartError`, `ProfileLockedError`.
- `close() -> None`: idempotent.
- Properties: `screenshot_dir: Path`, `listing_url: str`.
- `LoginState(StrEnum)`: LOGGED_IN / LOGGED_OUT / UNKNOWN.

Screenshots: one per attempt that reaches the browser, in `settings.executor.screenshot_dir`, named `YYYYMMDDTHHMMSSZ_bet<bet_id>_<idempotency-key>_<status>.png`.

## Placement journal (`cs2bot.executor.journal`)
- File `<settings.betclic.browser_profile_dir>/cs2bot-placements.jsonl` (`JOURNAL_FILENAME`), JSON lines `{"event": "click"|"result", "key", "bet_id", "at", "status"?}`. No credentials, stakes or account data.
- A `click` record is appended and fsync-ed immediately before the final click (if it cannot be written the button is not clicked: `FAILED`). A `result` record follows when the outcome is known.
- A key is blocked while its latest record is a `click` (the process died around the click) or a result of `placed` / `uncertain`. Definite rejections unblock it. Records older than `journal_retention_days` (60) are ignored and removed at start-up. Malformed lines are skipped.
- `PlacementJournal(path: Path, *, retention_days: float = 60.0, clock=utcnow)` with `.entries() -> list[JournalEntry]`, `.latest(key)`, `.blocking_entry(key) -> JournalEntry | None`, `.record_click(key, bet_id)`, `.record_result(key, bet_id, status: PlacementStatus)`, `.compact() -> int`. Read/write problems raise `JournalError` (`compact` never raises).
- `JournalEntry(event, key, bet_id, at, status=None)` with `.blocks`.
- Deleting the browser profile directory deletes the journal with it.

## FakeExecutor (`cs2bot.executor.fake`)
```python
FakeExecutor(script: Iterable[PlacementResult | PlacementStatus | Callable[[BetOrder], PlacementResult]] = (), *,
    balance: Decimal | None = Decimal("1000.00"), logged_in: bool = True, halt_check: HaltCheck | None = None,
    honour_dry_run: bool = True, health: ExecutorHealth | None = None,
    history: Iterable[BookmakerBetRecord] = (), login_succeeds: bool = True, enforce_idempotency: bool = True)
```
- Attributes: `script` (deque), `balance`, `logged_in`, `history`, `calls: list[tuple[str, Any]]`, `orders`, `results`, `closed`, `clicked_keys: dict[str, PlacementStatus]`.
- Methods: `queue(*items)`, `settle(external_bet_id, status, payout=None)` (credits the payout), plus the full `AccountExecutor` API.
- Like the real executor it never places twice: once a non-dry-run placement for a key ended PLACED or UNCERTAIN, another `place` with that key answers UNCERTAIN ("refusing to place twice") without consuming the script. Use a distinct `idempotency_key` per order in tests, or pass `enforce_idempotency=False`.
- Default (no script), checked in order: already clicked key → UNCERTAIN; halt check → KILL_SWITCH; logged out → LOGIN_REQUIRED; `order.dry_run` → DRY_RUN_OK; stake > balance → INSUFFICIENT_BALANCE; otherwise PLACED (id `FAKE-n`, balance reduced, an OPEN record added to history).

## ThreadBoundExecutor (`cs2bot.executor.threaded`)
```python
ThreadBoundExecutor(factory: Callable[[], AccountExecutor], *, thread_name: str = "betclic-executor",
    call_timeout_s: float | None = None)
```
- Runs every call on one worker thread; the wrapped executor is created on its first call. Exceptions are passed back to the caller.
- `place` always waits for its result: `call_timeout_s` applies only to `get_balance`, `health_check`, `is_logged_in` and `fetch_bet_history`.
- If the wrapped executor cannot be created (broken selector file), `place` returns a `FAILED` result (nothing was clicked) and the next call tries again; the other methods raise the error.
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
  - `behaviour: SelectorBehaviour(odds_change_accept_places_bet=True, dismiss_cookie_banner=True, dismiss_popups=True, require_slip_market_match=True, count_hidden_slip_items=True)`
  - `attributes: dict[str, list[str]]`, `texts: dict[str, list[str]]` (normalised at load), `patterns: dict[str, list[str]]`
  - `elements: dict[str, ElementSpec]`
  - Class constants `REQUIRED_ELEMENTS` and `REQUIRED_TEXTS`.
- Behaviour flags added in review round 1:
  - `require_slip_market_match`: a market name shown in the slip that does not fit the ordered market aborts the bet (false: warning only).
  - `count_hidden_slip_items`: slip selections that are in the DOM but hidden count as stale selections (false: only visible ones count).
- `texts.ignore`: phrases of harmless slip notices; such messages are skipped. Messages are classified element by element in the order pending, bet_accepted, insufficient_balance, stake_limit, odds_changed, market_suspended, login_required, rejected.
- Optional element `slip_potential_win`: potential payout in the slip, used to cross-check the stake.
- `slip_stake_input` and `slip_place_button` must match exactly one visible element.
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
- `ProfileLock(profile_dir: Path, *, stale_after_s: float = 600.0, heartbeat_s: float | None = 30.0, filename: str = "cs2bot-executor.lock", clock: Callable[[], datetime] = utcnow)` with `.acquire()`, `.release()`, `.refresh()`, `.verify() -> bool`, `.read_info() -> LockInfo | None`, `.held`, `.lost`; works as a context manager.
- `verify()` re-reads the lock file now: True only if we hold the lock and the file is still ours. `acquire()` on a lost lock takes it again from scratch (raises `ProfileLockedError` if another process holds it).
- `LockInfo(pid, host, token, created_at, heartbeat_at)`; `pid_alive(pid: int) -> bool`.

## Tuning (`cs2bot.executor.tuning`)
`ExecutorTuning` is a frozen dataclass.
- `ExecutorTuning.from_settings(settings: Settings, **overrides) -> ExecutorTuning` copies the fields named in `SETTINGS_FIELDS` from `settings.executor`; overrides win; inconsistent values raise `ValueError`, unknown override names `TypeError`.
- Defaults:
  - viewport 1440x900
  - action_timeout_s 15, element_timeout_s 10, optional_timeout_s 1.5
  - login_check_timeout_s 8, login_logged_out_stable_s 1.5, login_poll_s 1
  - slip_update_timeout_s 8, confirmation_timeout_s 45, balance_update_wait_s 5
  - poll_interval_s 0.2, slip_settle_s 0.3
  - max_odds_change_rounds 2, min_seconds_between_bets 20, typing_delay 60–180 ms
  - history_max_load_more 10, health_check_event_page True, save_html_on_selector_error True
  - lock_stale_after_s 600, lock_heartbeat_s 30
  - max_odds_rise_factor 1.5 (None disables), potential_win_min_ratio 0.70, potential_win_max_ratio 1.05
  - journal_retention_days 60

## Session (`cs2bot.executor.session`)
- `build_launch_options(settings, tuning, profile_dir) -> dict[str, Any]`
- `parse_proxy_url(url) -> dict[str, str]` (server / username / password; the URL is never echoed in errors)
- `BrowserSession` (used internally); `ensure_lock()` verifies the profile lock, takes a deleted lock again, and closes the browser and raises `ProfileLockedError` if another process holds it.
- Constants `BROWSER_LOCALE="pl-PL"`, `BROWSER_TIMEZONE="Europe/Warsaw"`

## CLI / integration guidance
- `cs2bot executor login` → `open_login_window()`. If `cs2bot up` already holds the profile it raises `ProfileLockedError`; in that case have the dashboard call `open_login_window` on the running wrapper instead.
- `cs2bot executor health` → `health_check()`; store `health.model_dump(mode="json")` under `db.state.EXECUTOR_HEALTH`.
- `cs2bot executor balance` → `get_balance()`.
- Mapping from result to `bets.status`: use `cs2bot.domain.betting.bet_status_after_placement` (`engine.ledger.apply_placement` does):
  - PLACED → PLACED (write the ledger stake)
  - DRY_RUN_OK → DRY_RUN
  - UNCERTAIN → keep PLACING and reconcile with `fetch_bet_history` (match by `external_bet_id`, else by selection + stake + `placed_at` ≥ attempt time)
  - LOGIN_REQUIRED → `state.set_paused(..., reason="Betclic login required")`
  - SELECTOR_ERROR → pause + dashboard alert
  - anything else → FAILED
- ODDS_MOVED also covers "odds rose too far": re-price the outcome with fresh odds on the next cycle instead of resending the same order.

## Known limitations

- All selectors in config/betclic_selectors.yaml are unverified guesses (betclic.pl is geo-blocked here); the mock site was built to match them, so passing tests prove the logic, not the live DOM. Before real bets the owner must run `cs2bot executor health` and one DRY_RUN placement on the PC, fixing selectors by following the DevTools guide in the YAML header.
- The strict checks fail closed on a page that differs from the guesses: several visible stake fields or place buttons, hidden slip items kept in the DOM, a slip that words the market differently, or a `slip_potential_win` locator that hits the wrong element all give SELECTOR_ERROR until the YAML is adjusted (flags `count_hidden_slip_items`, `require_slip_market_match`; remove or fix the locator). The DRY_RUN smoke test shows all of them except the final-button checks.
- How Betclic's odds-change dialog behaves is unknown, so behaviour.odds_change_accept_places_bet defaults to true (accepting submits the bet). If the real site needs a second click, the result is UNCERTAIN (reconciliation finds no bet), never a double bet; set it to false once observed.
- `max_odds_rise_factor` (1.5), the potential-win ratios and `journal_retention_days` can only be changed through an explicit `ExecutorTuning`; they are not in `ExecutorSettings` yet.
- The potential-win cross-check assumes Betclic shows the total payout (stake x odds x 0.88). If it shows the net profit instead, the check aborts with SELECTOR_ERROR and the ratios must be adjusted.
- The placement journal lives in the browser-profile directory. Deleting that directory (to reset the login) also deletes the record of clicked keys.
- The idempotency key is the only identity the executor knows. Two different keys for the same outcome are two bets: the engine's `load_already_bet_keys` must prevent that.
- Session cookies without an expiry may be dropped when Chromium restarts. The bot relies on staying alive inside `cs2bot up` and on the owner ticking 'remember me' at the manual login. Cookies are not exported to disk on purpose: that would store an unencrypted session token.
- Bet-history parsing is best effort: dates are read as Warsaw local time, rows without a readable id come back with external_bet_id=None, and status comes from the status element or the configured phrases (UNKNOWN otherwise).
- Message classification after the click (texts.*) can only be as good as the configured Polish phrases. Unknown messages count as UNCERTAIN, which is safe but needs reconciliation.
- The login state trusts `logged_in_indicator` first. If its loosest fallback (a header containing an amount) matched a logged-out promo header, the bot would proceed and the attempt would end UNCERTAIN or LOGIN_REQUIRED at the final click, never as a bet.
- The Windows branch of pid_alive (ctypes OpenProcess/GetExitCodeProcess) is only reviewed and type-declared; tests run on Linux, so it has not been executed here.
- If DEBUG=pw:api is set, Playwright's own debug log would print values passed to fill(), including a pre-filled password. Do not enable it together with credentials_source env/keyring.
- Playwright's sync API cannot run inside an asyncio event loop and is tied to one thread. Use ThreadBoundExecutor / create_betclic_executor; a long open_login_window blocks other queued executor calls until it returns.
- The cookie banner is only dismissed via 'reject / necessary only' buttons; the bot never accepts optional cookies for the owner (the owner handles the banner during the first manual login).
- Attempts that never reach the browser (invalid order, duplicate key, executor disabled, profile lock or browser start failure) produce no screenshot. Every attempt that reaches the browser does.
- Browser tests launch Chromium once per test: the directory takes about 2 min 25 s serially, 1 min 25 s with `-n 2`.

## Foundation change requests

- src/cs2bot/config.py `ExecutorSettings`: add `max_odds_rise_factor: float | None = 1.5`, `potential_win_min_ratio: float = 0.70`, `potential_win_max_ratio: float = 1.05` and `journal_retention_days: float = 60.0`, so the owner can change them from `.env`. Then add the four names to `cs2bot.executor.tuning.SETTINGS_FIELDS` (one line; `from_settings` copies whatever is listed there).
- docs/ARCHITECTURE.md P9: document that `BetExecutor` includes `close()`, plus `AccountExecutor` (is_logged_in, open_login_window, fetch_bet_history), `ThreadBoundExecutor`, `create_betclic_executor(settings, session_factory)`, that real bets require both `order.dry_run` and `settings.executor.dry_run` to be false, and that the executor keeps a placement journal (`cs2bot-placements.jsonl`) in the browser-profile directory.
- src/cs2bot/domain/betting.py `PlacementStatus.ODDS_MOVED`: add a comment that it also covers odds that rose implausibly far above `requested_odds` (the order must be re-priced, not resent).
- src/cs2bot/domain/betting.py `BetOrder`: consider adding the bookmaker's event name (`event_name: str = ""`, from `bookmaker_events.name`). The executor could then verify the event shown on the page and in the slip; today a wrong event can only be detected through the event id in the URL.

## New dependency requests

- (none)

## Review round 1

Independent review of the package against docs/dev/briefs/executor.md (requirements, addendum, review focus). All fixes are inside the package's owned paths. Checks after the fixes: 274 tests pass (193 unit, 81 browser), ruff check and format clean, mypy clean, tests/foundation passes.

### What was verified
- Requirements 1–5 point by point: contracts, selector loading and resolution, launch options, profile lock, login window, placement steps 1–11, balance, bet history, health check, utilities, test scenarios.
- The placement flow was re-derived step by step for the review-focus properties: never twice, never when halted, never below the minimum odds, never with a stale slip, stake equal to the order stake, dry run never clicks.
- Polish number parsing and formatting, text normalisation, name matching, date parsing, URL host check, screenshot naming.
- Credentials: never logged, never in screenshots' names, journal or lock files.
- Interfaces against docs/ARCHITECTURE.md, `cs2bot.domain.betting`, `engine.ledger.apply_placement` and the API's `login_launcher` / `executor_health_provider`.
- Each bug below was first reproduced as a failing test against the unmodified code (where marked "reproduced"), then fixed.

### Issues found and fixed
1. **Critical, reproduced: a page re-render could redirect the bet to another market.** The chosen market and selection are addressed by position. If the page re-rendered during the human-like pause (a market added or removed above), the click landed on the same-named outcome of another market, and the slip check only logged a warning. The mock test placed a real bet on "Zwycięzca mapy 1" instead of "Zwycięzca meczu". Fixed: the market title, selection name, lock state and odds are read again immediately before the click, and a slip showing another market now aborts (`behaviour.require_slip_market_match`).
2. **Critical, reproduced: the stake was not re-checked before the final click.** A page that changed the stake field after it was typed (re-render, default stake) got the changed stake placed. Fixed: the stake field is read again right before the click and must equal the order stake.
3. **Critical, reproduced: an odds-change dialog could be accepted below the minimum odds.** The new price was read from the dialog alone; any other number in it (or a dialog lagging behind the slip) was trusted. The mock test accepted "1,90" while the bet went in at 1,60 with a minimum of 1,80. Fixed: the dialog and the slip are both read and the lower price counts; a free-text number without a readable slip is not trusted.
4. **Critical: an "odds changed" text still visible after accepting was read as a rejection.** Accepting submits the bet; the old text lingering while the server works produced ODDS_MOVED (a "no bet" status that allows a retry) for a bet that was then accepted. Fixed: after an accept the text is inconclusive; without a receipt the result is UNCERTAIN. Also fixed a probe-order race that turned a dialog rendering a moment after its text into ODDS_MOVED.
5. **Major: idempotency did not survive a restart or crash.** Clicked keys were only remembered in memory, and only after the outcome was known. A crash between the click and the caller's database write could lead to a second placement. Fixed: write-ahead placement journal (see above), checked before every real placement; the in-memory record is also set before the click.
6. **Major, reproduced: a hidden stale selection in the slip was not counted.** Only visible slip items were counted, so a hidden one would have turned the single into a combination bet. Fixed: hidden items count (`behaviour.count_hidden_slip_items`), also when clearing the slip and before the final click.
7. **Major, reproduced: a misread stake could not be detected.** The field can show "12,50" while the site computes with 1250. Fixed: optional `slip_potential_win` cross-check against stake x odds; a balance change different from the stake is reported loudly on the PLACED result.
8. **Major, reproduced: several stake fields or place buttons were resolved with "first match".** Fixed: exactly one visible element is required, otherwise SELECTOR_ERROR.
9. **Major: a redirect to another event was detected by substring.** Event id 300220100 was "found" in `...-m3002201001`, so a finished event redirecting to a live one with the same team names passed. Fixed: whole-number match, applied to the listing path too.
10. **Major: the addendum's `ExecutorTuning.from_settings` was missing**, so the `CS2BOT_EXECUTOR__*` tuning knobs had no effect. Implemented; used by `create_betclic_executor` and by `BetclicExecutor` when no tuning is passed; bad values fail at start-up.
11. **Major: a covered or disabled place button ended as UNCERTAIN** (the click timed out after the attempt was marked as clicked), blocking the key although nothing was clicked. Fixed: trial click first; failure is a definite SELECTOR_ERROR.
12. **Major: a lost profile lock was only logged.** After another process took the lock over, this process kept betting with the shared profile. Fixed: the lock is verified before each placement; a deleted lock is taken again, a foreign one stops the placement and closes the browser.
13. **Major: a stale confirmation message was ignored before the click and accepted as this bet's confirmation after it.** It was only caught because the receipt locator happens to match the same words. Fixed explicitly: any confirmation message on screen before the click aborts.
14. **Major: `ThreadBoundExecutor.place` honoured `call_timeout_s`**, so a caller could give up on a placement that kept running. Fixed: `place` always waits; a factory failure is returned as FAILED.
15. **Minor, reproduced: odds far above the requested price were taken blindly.** Fixed: `max_odds_rise_factor` (1.5) → ODDS_MOVED.
16. **Minor: an unknown message right after the click ended the attempt at once as UNCERTAIN**, even if the receipt followed a second later. Fixed: keep waiting until the confirmation timeout.
17. **Minor: a receipt saying "pending approval" was reported as PLACED.** Fixed: UNCERTAIN unless a final confirmation or rejection follows.
18. **Minor: slip messages were classified as one joined text**, so one phrase could hide another message. Fixed: classified per element; new `texts.ignore` list for harmless notices.
19. **Minor:** the pacing gap was slept before the halt check (a halted bot waited 20 s per order); a blank idempotency key and `requested_odds <= 1` were accepted; `fill()` errors on number inputs surfaced as FAILED; "load more" in the bet history was clicked up to 10 times even when it loaded nothing; one unparsable history row aborted the whole fetch; the odds text was removed from every position of a selection label; `FakeExecutor` placed the same key twice; a flaky test asserted the receipt was closed before the page had closed it (the executor now waits briefly for it). All fixed.

### Remaining concerns
- The live site is still unverified. The new strict checks make a wrong guess fail closed with SELECTOR_ERROR rather than bet wrongly, but the owner must expect to adjust the YAML during the DRY_RUN smoke test.
- The executor cannot verify the event itself beyond the id in the URL, because `BetOrder` carries no team or event name (see the foundation change request).
- The acknowledge-mode path (`odds_change_accept_places_bet: false`) re-clicks the place button once. It now re-checks the receipt, messages, slip and stake after a pause first, but its safety on the real site depends on the flag matching the site's real behaviour.
- The integration layer must still mark bets PLACING before calling `place` and reconcile UNCERTAIN results; the journal only guards against clicking twice for the same key.
- Serial test time (2 min 25 s) is above the 2-minute guideline; with `-n 2` it is 1 min 25 s.
