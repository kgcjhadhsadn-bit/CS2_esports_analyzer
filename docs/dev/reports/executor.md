# executor: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/executor -q -n 2` (browser tests use PLAYWRIGHT_CHROMIUM_EXECUTABLE, default /opt/pw-browsers/chromium, and are skipped if it is missing; `-m "not browser"` runs only the 260 unit tests) - 390 passed, 0 failed (lint/type clean: True). About 2 min 30 s with `-n 2` on this shared machine (130 browser tests).

## Summary

P9 `cs2bot.executor` is built: it places single pre-match bets on Betclic.pl through a visible Chromium using a dedicated persistent profile. The whole flow passes against a local mock "Betclic-like" site. The real betclic.pl DOM could not be checked (geo-blocked), so every selector in config/betclic_selectors.yaml is a best guess marked `verified: false`. The owner must run `executor health` and a DRY_RUN placement on their PC before betting real money.

What is in the package:
- **Contracts.** `BetExecutor` (place / get_balance / health_check / close) and `AccountExecutor`, which adds is_logged_in, open_login_window and fetch_bet_history. Also `BookmakerBetRecord` / `BookmakerBetStatus` (history rows, defined in `cs2bot.domain.betting`) and an error hierarchy.
- **BetclicExecutor.** Runs this flow:
  - refuse a key whose final button was already clicked (memory + placement journal) before anything else, then validate the order, halt check, then open the event (by URL or found on the CS2 listing by id; a redirect to another page is detected), then check login, then clear stale slip selections;
  - find market and selection by exact normalised name, never guessing (ids only break ties; an id that sits on a different name counts as a conflict); an event that shows the live marker is refused (pre-match only);
  - read the Polish odds; refuse if below `min_acceptable_odds` or implausibly far above the requested odds;
  - after the human-like pause, read the chosen market title, selection name and odds again (the elements are addressed by position and a live page may re-render), check the halt flag once more (with a quick-bet account mode that click alone would bet), then click;
  - verify the slip holds exactly that one selection (hidden ones count), showing the ordered selection and market; if instead a receipt appears or the balance drops (a quick-bet mode placed a bet on the click), the result is UNCERTAIN;
  - type the stake as "12,50" and read it back; cross-check the potential win shown in the slip (required by default: no readable potential win, no bet); an odds-change notice or dialog shown at any point before the final click ends the attempt as ODDS_MOVED (nothing clicked);
  - locate the one final button (also in a dry run) and require it to be enabled;
  - dry run stops here: screenshot, clear the slip, never click the final button;
  - just before the click: make sure the button is clickable (trial click; a promo popup or cookie banner in the way is dismissed once), then re-read the slip odds, the slip's selection and market names, the stake field, the potential win, the messages and any odds-change notice, write the journal record, then the halt check again;
  - click "Postaw zakład". Odds-change dialog: accept only at or above the minimum (the lowest of the dialog's prices and the slip's price counts, read again right before the accept click), at most once per dialog, otherwise decline. Parse the receipt (bet id, new balance); a receipt box that carries only a rejection or "pending" text is not a confirmation. Known errors map to their status, but only if neither a receipt nor a balance drop contradicts them. Anything unclear after the click returns UNCERTAIN and is never retried.
- **Extra safety beyond the brief:**
  - real bets need both `order.dry_run` and `settings.executor.dry_run` to be false;
  - a receipt or confirmation message left over from an earlier bet is closed first, and if it can't be closed the bot refuses to click, so an old bet id is never reported for the new bet;
  - an idempotency key that was PLACED or UNCERTAIN is refused (as UNCERTAIN) if it comes again, also after a restart or crash (write-ahead placement journal in the profile directory) and whatever else is wrong with the order by then; definite rejections can be retried;
  - a rejection read off the page after the final click is watched for a few seconds: a receipt or a lower balance turns it into UNCERTAIN;
  - the accept / decline buttons of the odds-change dialog are taken from inside the notice or the bet slip, never "the first match on the page";
  - an order that carries a kick-off time (`event_starts_at`, not in `BetOrder` yet) is refused once the event has started, also right before the final click;
  - minimum gap between bets (default 20 s);
  - the bot never opens non-Betclic URLs with the logged-in profile;
  - a lost profile lock stops placements (and closes the browser if another process holds the profile);
  - SELECTOR_ERROR saves a screenshot plus the page HTML so selectors can be fixed;
  - "matched fallback" warnings are logged once per element;
  - housekeeping clicks (clear the slip and its confirmation, remove an item, close a receipt, popup or cookie banner, open the slip) refuse any element whose text contains "postaw" or that is, contains or sits inside a `slip_place_button` match, whatever the YAML says: a wrong locator can never turn the dry run's clean-up into a bet;
  - `place` never raises for bad input or an environment problem (an absurd stake, an unwritable profile directory, an internal bug): FAILED before the final click, UNCERTAIN (key blocked) after it.
- **Profile lock.** A lock file created exclusively, holding PID, host, a token and a heartbeat thread. It counts as stale when the heartbeat is older than a set age, or immediately if it is on this machine and the PID is dead. Release only deletes our own file. The Windows PID check uses ctypes.
- **Login.** `open_login_window` pre-fills credentials from env or keyring (keyring imported lazily) but never submits the form or automates 2FA, then waits for the owner. Credentials are never logged (a test checks this).
- **Other methods.** `fetch_bet_history` parses "Moje zakłady": id, Warsaw dates (a displayed minute counts up to its end when filtering by `since`), odds, stake, status open/won/lost/void/cashout, payout, with "Pokaż więcej" paging. `health_check` covers browser start, login, balance, key selectors on the listing and on one event page, and the placement journal. `get_balance` parses "1 234,56 zł".
- **Selectors.** Pydantic-validated `SelectorSet` loaded from YAML: 56 elements, each with ordered fallbacks (css / role+name / text / xpath, Polish texts). It also holds message phrases and bet-id/history regexes, plus behaviour flags. The YAML header explains how to update selectors with Chrome DevTools.
- **Helpers.**
  - `FakeExecutor` (scripted results, records every call, simulates balance and history, never places twice for one key);
  - `ThreadBoundExecutor`, needed because Playwright's sync API is tied to one thread and cannot run inside asyncio;
  - `create_betclic_executor()` builds the production executor with the DB halt check and the settings-driven tuning in one call;
  - `db_halt_check` treats any DB failure as "halted";
  - `PlacementJournal` (crash-safe idempotency);
  - Polish number/date/text utilities and screenshot naming.

Tests (390; the review round 1 third pass added 21, review round 2 added 26):
- Unit tests cover parsing, formatting, selector validation, matching, the lock, the journal, tuning from settings, the fake executor, the halt check and pacing, launch options via a fake Playwright, the thread wrapper, the URL guard and the order guards.
- 108 browser tests run headless with executable_path /opt/pw-browsers/chromium against a mock site on 127.0.0.1. Every click on the final button reaches the mock server, so tests can assert it was never clicked (dry run, halt, odds moved). To save time most of them get a fresh browser context of one shared Chromium per module (`tests/executor/conftest.py`, `_SharedBrowserChromium`); `make_executor(real_profile=True)` and the thread-wrapper test use the production `launch_persistent_context` with a real profile directory.
- Scenarios covered: dry run; placement; stake formatting; stale slip; odds moved before the click and silently in the slip; odds dialog accepted, declined, repeated, and acknowledge-mode; missing and ambiguous market/selection; suspended market; insufficient balance; stake limit; unknown message; login required; halt switched on between steps and during a pause; no confirmation → UNCERTAIN, no retry; idempotency; leftover receipt; broken selectors; history parsing; login prefill (env and keyring); relaunch after the window is closed; profile lock; thread confinement; screenshots.
- Adversarial scenarios added in review round 1 (`test_executor_browser_safety.py`): page re-render between choosing and clicking a selection; slip showing another market; hidden stale slip selection; stake changed by the page before the final click; stake misread by the site (potential-win cross-check); missing or unreadable potential win; two stake fields; odds-dialog number contradicting the slip; lingering "odds changed" text after accepting; odds far above the request; covered place button; stale confirmation message; pending-approval receipt; harmless notice next to a real error; restart after UNCERTAIN; crash right at the click; unwritable journal; redirect to another event.
- Second batch of review round 1 (`test_executor_browser_review.py`, `test_executor_review_units.py`): odds-change dialog that lingers or never closes after accepting (one accept click only); a second odds change right after accepting; odds dropping while the dialog is open; look-alike "Akceptuj" buttons elsewhere on the page; an ambiguous accept button; a bet taken behind an error-looking notice (three wordings); a real rejection with an untouched balance; selection suspended at the click; a bet placed by the selection click itself (quick-bet mode); live event; kick-off passing while the bot works; broken, duplicated or swapped ("deposit") final button in a dry run; bet-history row placed later in the displayed minute; journal with a torn last line, keys containing Unicode line breaks, a journal that must not be rewritten without the profile lock; already-clicked key with a disabled executor / invalid order / started event / halt; URL scheme downgrade and look-alike hosts; non-finite numbers in an order.
- Third pass of review round 1 (`test_executor_browser_review_third.py`, `test_executor_review_third_units.py`): odds-change dialog shown before the final click (button enabled / disabled, also in a dry run); the slip's selection swapped right before the click; a clear-slip confirmation locator that hits the final button in a dry run (by text and by element identity); a closable popup over the button; halt right before the outcome click; a receipt box showing a rejection; a confirmation phrase next to an odds-change remark; a dialog showing "old → new" price with a lagging slip; absurd stakes; lock-file `PermissionError` (transient, persistent, unwritable directory); unexpected errors before and after the click.

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
- The wrapper gets `clicked_check=journal_clicked_check(settings, tuning)`: if the executor cannot be created (broken selector file), `place` still answers UNCERTAIN for a key the placement journal shows as clicked (FAILED otherwise).
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
`journal_clicked_check(settings: Settings, tuning: ExecutorTuning) -> Callable[[BetOrder], PlacementResult | None]` (`cs2bot.executor.factory`, re-exported): read-only journal lookup giving the UNCERTAIN "refusing to place twice" result for a clicked key (None for unknown keys, definite rejections, dry runs and an unreadable journal).

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
  - `PLACED`: `external_bet_id` (None if the receipt shows no id - possible only when it shows a confirmation phrase; reconcile in that case), `placed_odds`, `balance_after` (None if the header did not update yet), `screenshot_path`. A receipt counts only on positive evidence (review round 2): a `texts.bet_accepted` phrase, or a readable bet id with no error / pending phrase; an empty (still loading) or unrecognised receipt is inconclusive. The message carries a `WARNING:` when the balance did not drop by exactly the stake or the accepted odds are below the minimum.
  - `DRY_RUN_OK`: `placed_odds` = the odds the bet would have had. Every step up to the final click succeeded, including locating the one enabled "Postaw zakład" button; it was never clicked.
  - `UNCERTAIN`: the final button was clicked but no confirmation or definite rejection was detected within `confirmation_timeout_s` (including an unrecognised message, a receipt that says "pending approval", a receipt element that shows a rejection phrase but no confirmation phrase, a receipt that stays empty or shows neither a confirmation phrase nor a bet id, an "odds changed" text that stays after the new odds were accepted, or an odds-change dialog that stays on screen after it was accepted). Also:
    - a rejection was shown after the click but a receipt appeared or the header balance dropped below its value from before the click within `balance_update_wait_s` (the message says which);
    - a receipt appeared or the balance dropped right after clicking the SELECTION (quick-bet mode; also for dry-run orders);
    - without touching the page, when the key's final button was already clicked with a PLACED/UNCERTAIN outcome in this process or according to the placement journal (earlier run, crash). This check comes first: such a key is never answered with FAILED, KILL_SWITCH or MARKET_UNAVAILABLE. The journal is read again once the profile lock is held (another process may have clicked the key while this one waited for the lock or the pacing gap);
    - an unexpected internal error after the final click.
    NEVER retry; reconcile with `fetch_bet_history`.
  - No money at risk for all other statuses: `KILL_SWITCH` (halt checked at the start, right before the outcome is clicked, right before the final click, before accepting changed odds and before an acknowledge-mode re-click), `LOGIN_REQUIRED`, `ODDS_MOVED` (below the minimum, or more than `max_odds_rise_factor` x above the requested odds: re-price; also an odds-change dialog whose accept button is ambiguous, and any odds-change notice or dialog displayed before the final click), `MARKET_UNAVAILABLE` (market/selection missing or suspended, event removed or redirected, event live, event already started according to the order's `event_starts_at`), `INSUFFICIENT_BALANCE` (the bookmaker's message; or the final button is missing/disabled while the stake exceeds the header balance), `REJECTED_BY_BOOKMAKER` (stake limits / rejected), `SELECTOR_ERROR` (a selector did not resolve; the market/selection is ambiguous; several stake fields or place buttons; the slip shows another market, a hidden extra selection, a changed stake, an implausible or unreadable potential win, or - right before confirming - another selection or market name; the place button is covered (also after dismissing popups once); a stale confirmation or receipt is displayed; also saves `<screenshot>.html`), `FAILED` (invalid order incl. non-finite numbers and stakes that are no valid amount (e.g. `1E+30`), executor disabled, browser or lock problem incl. an `OSError` of the lock file, the profile lock no longer ours right before the final click (verified before the journal record is written), an unexpected internal error before the final click, foreign URL or another scheme than `base_url`, the page changed while selecting, unknown slip message before the click, unreadable/unwritable placement journal).
  - After the final click only explicit bookmaker rejections (not contradicted by a receipt or the balance), a declined odds change and a halt before re-confirming count as "no bet"; any other problem after the click is reported as UNCERTAIN. A post-click rejection takes up to `balance_update_wait_s` longer to return.
  - Definite rejections that happen after the click may be retried later with the same key.
  - Dry-run orders are never refused for an already clicked key (they never click).
  - Validation: non-blank `idempotency_key`; stake > 0 with at most 2 decimals and ≥ `settings.betclic.min_stake`; `min_acceptable_odds > 1`; `requested_odds > 1`; names present; `event_url`, or `external_event_id` matching `[A-Za-z0-9_.:-]+`.
- `get_balance() -> Decimal | None`: loads the home page and parses the header ("1 234,56 zł" → `Decimal("1234.56")`). Returns None if logged out, unreadable, or the browser/lock fails (incl. an `OSError` of the lock file).
- `is_logged_in() -> bool`: loads the home page and checks the header state. Raises `BrowserStartError` / `ProfileLockedError` / Playwright errors if the browser cannot be used.
- `open_login_window(timeout_s: float = 600.0) -> bool`: visible browser on `urls.login`; clicks "Zaloguj się". With `credentials_source` "env" or "keyring" (lazy `import keyring`; `get_password(service, username)` if a username is configured, else `get_credential(service, None)`) it pre-fills the fields but never submits. Returns True when the logged-in indicator appears, False on timeout. If the owner closes the window, it re-checks the saved session.
- `health_check() -> ExecutorHealth`: `ok` requires the browser running, logged in, balance readable, all checked selectors resolving and a readable placement journal. `selectors_ok` keys: `login_state`, `header_balance`, `nav_my_bets`, `event_link`, `market_container`, `market_title`, `selection_button`, `selection_odds`. Failures appear in `message`. A lock or browser failure (incl. an `OSError` of the lock file) gives `ok=False` and never raises.
- `fetch_bet_history(since: datetime) -> list[BookmakerBetRecord]`: rows from "Moje zakłady" that may have been placed at or after `since`. The history shows minutes (or only the day) and `placed_at` is the START of that minute/day, so a row is returned when its minute (day) ends at or after `since`: pass the attempt time as it is, no margin needed, and match on selection + stake afterwards. Rows with an unknown date are kept; an unreadable row is skipped. The status comes from `history_item_status`; without that label the whole row text is classified and phrases of more than one status (an open bet with a "Cash out" button, a promo mentioning "zwrot") give `UNKNOWN`. `payout` is None for OPEN and UNKNOWN rows. Raises `LoginRequiredError`, `SelectorNotFoundError`, `BrowserStartError`, `ProfileLockedError`.
- `close() -> None`: idempotent.
- `abort_login() -> None` (review round 2): the one thread-safe method; a waiting `open_login_window` returns False at its next poll. `ThreadBoundExecutor.close()` calls it so a shutdown is not held up by a login window.
- Properties: `screenshot_dir: Path`, `listing_url: str`.
- `LoginState(StrEnum)`: LOGGED_IN / LOGGED_OUT / UNKNOWN.

Screenshots: one per attempt that reaches the browser, in `settings.executor.screenshot_dir`, named `YYYYMMDDTHHMMSSZ_bet<bet_id>_<idempotency-key>_<status>.png`.

## Placement journal (`cs2bot.executor.journal`)
- File `<settings.betclic.browser_profile_dir>/cs2bot-placements.jsonl` (`JOURNAL_FILENAME`), JSON lines `{"event": "click"|"result", "key", "bet_id", "at", "status"?}`, ASCII only (non-ASCII key characters are `\u`-escaped), records separated by `\n`. No credentials, stakes or account data.
- A `click` record is appended and fsync-ed immediately before the final click (if it cannot be written the button is not clicked: `FAILED`). A `result` record follows when the outcome is known. A record is always a line of its own, also after a torn earlier write.
- A key is blocked while its latest record is a `click` (the process died around the click) or a result of `placed` / `uncertain`. Definite rejections unblock it. Records older than `journal_retention_days` (60) are ignored; they are removed (`compact`) on the first placement after this process has taken the profile lock, never by merely constructing an executor. Malformed lines are skipped.
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
    call_timeout_s: float | None = None,
    clicked_check: Callable[[BetOrder], PlacementResult | None] | None = None)
```
- Runs every call on one worker thread; the wrapped executor is created on its first call. Exceptions are passed back to the caller.
- `place` always waits for its result: `call_timeout_s` applies only to `get_balance`, `health_check`, `is_logged_in` and `fetch_bet_history`.
- If the wrapped executor cannot be created (broken selector file), `place` returns `clicked_check(order)` when that gives a result (UNCERTAIN for a journaled key), else a `FAILED` result (nothing was clicked); the next call tries again; the other methods raise the error.
- `started: bool` property; `close()` is idempotent; works as a context manager.
- A long `open_login_window` blocks queued calls until it returns; `close()` first calls the wrapped executor's `abort_login()` (when it has one), so shutting down does not wait for the login timeout.

## Safety (`cs2bot.executor.safety`)
- `db_halt_check(session_factory: sessionmaker[Session]) -> HaltCheck`
- `never_halted() -> tuple[bool, str | None]`
- `HumanPacer(min_s: float, max_s: float, *, sleep=time.sleep, rng: random.Random | None = None, typing_delay_ms: tuple[int, int] = (60, 180))` with `.pause(scale: float = 1.0) -> float`, `.sleep(seconds: float)`, `.typing_delay_ms() -> float`.

## Selectors (`cs2bot.executor.selectors`, file `config/betclic_selectors.yaml`)
- `load_selectors(path: str | Path | None = None, *, settings: Settings | None = None) -> SelectorSet`. Defaults to `settings.betclic.selectors_file`, resolved with `resolve_path`; raises `SelectorConfigError`.
- `SelectorSet` (pydantic) fields:
  - `version`
  - `urls: SiteUrls(login="/", cs2_listing=None, bet_history=None)`
  - `behaviour: SelectorBehaviour(odds_change_accept_places_bet=True, dismiss_cookie_banner=True, dismiss_popups=True, require_slip_market_match=True, count_hidden_slip_items=True, require_potential_win=True)`
  - `attributes: dict[str, list[str]]`, `texts: dict[str, list[str]]` (normalised at load), `patterns: dict[str, list[str]]`
  - `elements: dict[str, ElementSpec]`
  - Class constants `REQUIRED_ELEMENTS` and `REQUIRED_TEXTS`.
- Behaviour flags added in review round 1:
  - `require_slip_market_match`: a market name shown in the slip that does not fit the ordered market aborts the bet (false: warning only).
  - `count_hidden_slip_items`: slip selections that are in the DOM but hidden count as stale selections (false: only visible ones count).
  - `require_potential_win` (second pass): no bet and no successful dry run unless `slip_potential_win` resolves and holds exactly one amount within the accepted range of stake x odds (false: the check is skipped when the element is missing or unreadable). With the flag on, a selector file that does not define `slip_potential_win` is rejected when it is loaded.
- `texts.ignore`: phrases of harmless slip notices; such messages are skipped. Messages are classified element by element in the order pending, bet_accepted, insufficient_balance, stake_limit, odds_changed, market_suspended, login_required, rejected.
- Element `slip_potential_win`: potential payout in the slip, used to cross-check the stake (needed for betting while `require_potential_win` is true).
- Optional element `event_live_indicator` (second pass): marker of an event that is in play; when visible the bet is refused with MARKET_UNAVAILABLE.
- `slip_stake_input` and `slip_place_button` must match exactly one visible element.
- `odds_changed_new_odds`: every match inside the notice is read and the LOWEST price counts (a dialog may show "1,85 → 1,70" with the old price first); the result is then compared with the slip's price, again the lower one counts.
- `slip_clear_confirm` (shipped locator): `[role=dialog]` / `[role=alertdialog]` buttons starting with Tak / Usuń / Potwierdź / Wyczyść, but never "Potwierdź zakład" nor any text containing "postaw"; the code-level housekeeping guard (see the summary) applies on top.
- `odds_changed_accept` / `odds_changed_decline`: looked up inside `odds_changed_notice` first, then inside `slip_container` (the whole page only if that does not resolve), and used only when exactly one visible element matches. The shipped names are anchored regexes ("Akceptuj", "Akceptuj zmiany", "Akceptuj nowy kurs", "... i postaw zakład"; "Anuluj", "Odrzuć"), so "Akceptuj wszystkie" of a cookie banner does not match.
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
- Also: `classify_text`, `names_compatible` (numbers are whole: "powyzej 26" does not fit "powyzej 26.5"), `contains_phrase`, `first_regex_group`, `sanitize_filename_part`.

## Lock (`cs2bot.executor.lock`)
- `ProfileLock(profile_dir: Path, *, stale_after_s: float = 600.0, heartbeat_s: float | None = 30.0, filename: str = "cs2bot-executor.lock", clock: Callable[[], datetime] = utcnow)` with `.acquire()`, `.release()`, `.refresh()`, `.verify() -> bool`, `.read_info() -> LockInfo | None`, `.held`, `.lost`; works as a context manager.
- `verify()` re-reads the lock file now: True only if we hold the lock and the file is still ours. `acquire()` on a lost lock takes it again from scratch (raises `ProfileLockedError` if another process holds it).
- `acquire()` retries a `PermissionError` from the exclusive create twice (Windows: a lock file being deleted by its previous holder); if it persists it raises `ProfileLockedError` when the lock file exists and re-raises the `PermissionError` when it does not (the profile directory is not writable).
- `LockInfo(pid, host, token, created_at, heartbeat_at)`; `pid_alive(pid: int) -> bool`.

## Tuning (`cs2bot.executor.tuning`)
`ExecutorTuning` is a frozen dataclass.
- `ExecutorTuning.from_settings(settings: Settings, **overrides) -> ExecutorTuning` copies every tuning field that `settings.executor` also has under the same name (today exactly the names in `SETTINGS_FIELDS`; a field added to `ExecutorSettings` later is picked up without a code change); overrides win; inconsistent values raise `ValueError`, unknown override names `TypeError`.
- Defaults:
  - viewport 1440x900
  - action_timeout_s 15, element_timeout_s 10, optional_timeout_s 1.5
  - login_check_timeout_s 8, login_logged_out_stable_s 1.5, login_poll_s 1
  - slip_update_timeout_s 8, confirmation_timeout_s 45, balance_update_wait_s 5 (also the watch window after a post-click rejection)
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
  - UNCERTAIN → keep PLACING and reconcile with `fetch_bet_history(since=<attempt time>)` (match by `external_bet_id`, else by event + market + selection + stake among the rows returned; the method already allows for the minute resolution of the displayed time). An UNCERTAIN result can also come from a dry-run order (quick-bet mode): treat it the same way and alert the owner.
  - LOGIN_REQUIRED → `state.set_paused(..., reason="Betclic login required")`
  - SELECTOR_ERROR → pause + dashboard alert
  - anything else → FAILED
- ODDS_MOVED also covers "odds rose too far": re-price the outcome with fresh odds on the next cycle instead of resending the same order.
- Never send a bet that is PLACING (UNCERTAIN) again. The executor answers UNCERTAIN for its key as a second line of defence, but a retry under a NEW idempotency key is a second bet.
- A PLACED result whose message contains `WARNING:` (balance changed by another amount than the stake, or accepted below the minimum odds) should raise a dashboard alert.
- Before going live the owner checks two account settings by hand (also listed in the header of config/betclic_selectors.yaml): Betclic must ask before taking a bet at changed odds (no automatic acceptance), and no quick-bet / one-click mode.

## Known limitations

- All selectors in config/betclic_selectors.yaml are unverified guesses (betclic.pl is geo-blocked here); the mock site was built to match them, so passing tests prove the logic, not the live DOM. Before real bets the owner must run `cs2bot executor health` and one DRY_RUN placement on the PC, fixing selectors by following the DevTools guide in the YAML header.
- The strict checks fail closed on a page that differs from the guesses: several visible stake fields or place buttons, hidden slip items kept in the DOM, a slip that words the market differently, a potential win that is missing, unreadable or outside the expected range all give SELECTOR_ERROR until the YAML is adjusted (flags `count_hidden_slip_items`, `require_slip_market_match`, `require_potential_win`; or fix the locator). The DRY_RUN smoke test shows all of them, including a final button that cannot be located; only the trial click on that button and everything after the click are not exercised by a dry run.
- If Betclic's account is set to accept changed odds automatically, a bet can be taken below `min_acceptable_odds` without any dialog. The executor can only report that afterwards (a `WARNING:` on the PLACED result when the receipt shows the odds). The owner must switch automatic acceptance off.
- Whether Betclic has a quick-bet / one-click mode is unknown. If a bet appears right after the bot clicks an outcome, the attempt ends UNCERTAIN (detected through a receipt or a lower balance only).
- The post-click cross-check of a rejection needs a readable header balance (or a receipt). With an unreadable balance a wrongly classified rejection is not detected; `health_check` reports an unreadable balance.
- The accepted-dialog rule is conservative: if the page reuses the same accept-button element for a second odds change, or a second change offers exactly the price already accepted, it is treated as the same dialog, nothing is clicked and the result is UNCERTAIN.
- `event_live_indicator` is an unverified guess like every other selector: until it is confirmed, the only protection against betting on a started match is the caller's timing (and `event_starts_at`, once `BetOrder` has it).
- How Betclic's odds-change dialog behaves is unknown, so behaviour.odds_change_accept_places_bet defaults to true (accepting submits the bet). If the real site needs a second click, the result is UNCERTAIN (reconciliation finds no bet), never a double bet; set it to false once observed.
- `max_odds_rise_factor` (1.5), the potential-win ratios and `journal_retention_days` can only be changed through an explicit `ExecutorTuning`; they are not in `ExecutorSettings` yet.
- The potential-win cross-check assumes Betclic shows the total payout (stake x odds x 0.88). If it shows the net profit instead, the check aborts with SELECTOR_ERROR; the ratios must then be adjusted (explicit `ExecutorTuning` today) or, as a last resort, `require_potential_win` switched off.
- If the wrapped executor cannot be created (broken selector file), `ThreadBoundExecutor.place` consults the placement journal only when built by `create_betclic_executor` (or given a `clicked_check`); a bare `ThreadBoundExecutor(factory)` answers FAILED, also for a key that was clicked in an earlier run.
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
- A placement against the mock site costs about a second of Playwright round trips, so the 121 browser tests take about 2 min 10-20 s with `-n 2` on this shared machine.
- Any visible `odds_changed_notice` before the final click ends the attempt as ODDS_MOVED. If Betclic displays a text that the notice locator matches permanently (e.g. a settings label), every attempt ends ODDS_MOVED; the DRY_RUN smoke test shows it (same answer), and the message names the selector to tighten.
- After a crash or a reboot the old lock file's PID may have been reused by another process; the lock then counts as live until its heartbeat is older than `lock_stale_after_s` (10 min by default) and placements answer FAILED ("profile in use") meanwhile. Self-healing; no process start-time check (it would need psutil or more Windows ctypes code that cannot be tested here).
- The final click itself keeps Playwright's actionability wait (`action_timeout_s`) after the last halt check. The trial click right before makes it immediate in practice; a click that times out is reported UNCERTAIN (never a silent second attempt).

## Foundation change requests

- src/cs2bot/config.py `ExecutorSettings`: add `max_odds_rise_factor: float | None = 1.5`, `potential_win_min_ratio: float = 0.70`, `potential_win_max_ratio: float = 1.05` and `journal_retention_days: float = 60.0`, so the owner can change them from `.env`. `ExecutorTuning.from_settings` picks up same-named fields automatically; only add the four names to `cs2bot.executor.tuning.SETTINGS_FIELDS` (a test pins that tuple to the shared fields).
- src/cs2bot/domain/betting.py `BetOrder`: add `event_starts_at: datetime | None = None` (UTC-aware kick-off of the bookmaker event, from `bookmaker_events.start_time`) and have the integration layer fill it. The executor already refuses an order whose `event_starts_at` has passed (before opening the browser and again right before the final click); without the field that check is inactive. It matters because orders are queued: a manual login window alone can hold the queue for ten minutes, and the model is pre-match only.
- docs/ARCHITECTURE.md P9: document that `BetExecutor` includes `close()`, plus `AccountExecutor` (is_logged_in, open_login_window, fetch_bet_history), `ThreadBoundExecutor`, `create_betclic_executor(settings, session_factory)`, that real bets require both `order.dry_run` and `settings.executor.dry_run` to be false, and that the executor keeps a placement journal (`cs2bot-placements.jsonl`) in the browser-profile directory.
- src/cs2bot/domain/betting.py `PlacementStatus.ODDS_MOVED`: add a comment that it also covers odds that rose implausibly far above `requested_odds` (the order must be re-priced, not resent).
- src/cs2bot/domain/betting.py `BetOrder`: consider adding the bookmaker's event name (`event_name: str = ""`, from `bookmaker_events.name`). The executor could then verify the event shown on the page and in the slip; today a wrong event can only be detected through the event id in the URL.

## New dependency requests

- (none)

## Review round 1

Independent review of the package against docs/dev/briefs/executor.md (requirements, addendum, review focus). All fixes are inside the package's owned paths. The round was done in three passes: the first reviewer was cut off after writing "First pass" below; the second pass verified that work critically (read all code, ran everything) and continued; a third pass did the same with the result of the second. Checks after the third pass: 364 tests pass (243 unit, 121 browser), ruff check and format clean, mypy clean, tests/foundation passes (37).

### First pass

#### What was verified
- Requirements 1–5 point by point: contracts, selector loading and resolution, launch options, profile lock, login window, placement steps 1–11, balance, bet history, health check, utilities, test scenarios.
- The placement flow was re-derived step by step for the review-focus properties: never twice, never when halted, never below the minimum odds, never with a stale slip, stake equal to the order stake, dry run never clicks.
- Polish number parsing and formatting, text normalisation, name matching, date parsing, URL host check, screenshot naming.
- Credentials: never logged, never in screenshots' names, journal or lock files.
- Interfaces against docs/ARCHITECTURE.md, `cs2bot.domain.betting`, `engine.ledger.apply_placement` and the API's `login_launcher` / `executor_health_provider`.
- Each bug below was first reproduced as a failing test against the unmodified code (where marked "reproduced"), then fixed.

#### Issues found and fixed
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

### Second pass

#### What was verified
- Everything the first pass changed: read in full, the 274 tests re-run (all passed), each claimed fix traced in the code. Two of its statements were corrected here: the dry run did not check the final button at all (now it locates it), and the potential-win cross-check was silently skipped when the element was missing (now required by default).
- Requirements 1–5 and the addendum once more, point by point; nothing was missing or stubbed.
- The placement flow again from the point of view of "what can the page do that we do not expect": slow or repeated answers, look-alike buttons, messages that mean something else, account modes, queues and time.
- What "definitely not placed" means for the caller: `bets.idempotency_key` is unique in the database, so a retry of a FAILED bet is a NEW key. The executor's per-key idempotency does not protect against a wrong "no bet" answer; every such answer after a click had to be re-examined.
- Journal file format and crash behaviour, URL guard, order validation, launch options, lock, thread wrapper, credentials (never logged; tests exist), text utilities with adversarial inputs.
- Interfaces: `engine.ledger.apply_placement`, `engine.decision.min_acceptable_odds` (3 decimals, Decimal comparison), the odds collector's raw names, external ids and event URLs (`-m<id>`; split market rows keep Betclic's raw name and original id), the API's `login_launcher` / health provider.
- Every issue below marked "reproduced" was first written as a failing test against the code as the first pass left it.

#### Issues found and fixed
1. **Critical, reproduced: an odds-change dialog could be accepted twice.** After the accept click the code waited a few seconds and, if the dialog was still on screen (slow server, no spinner), treated it as a new odds change and clicked "accept" again. Against the mock the second click placed the bet while the first request was still in flight: on a real site that is a second bet. Fixed: the clicked accept button is remembered (element handle) together with the accepted price; a dialog counts as new only if the page showed no dialog in between, or the clicked element is gone AND the offered price differs. Otherwise nothing is clicked and, without a receipt, the result is UNCERTAIN.
2. **Critical, reproduced: a bet that WAS taken could be reported as "definitely not placed".** After the final click any text matching a rejection phrase ended the attempt as ODDS_MOVED / MARKET_UNAVAILABLE / REJECTED_BY_BOOKMAKER. "Kurs uległ zmianie" can accompany a bet taken at updated odds, "niedostępny" can be about cash-out. The mock took the bet and the executor answered ODDS_MOVED; the caller would have staked the money again under a new key. Fixed: before a post-click rejection is returned the page is watched for `balance_update_wait_s`; a receipt or a balance below its value from before the click turns the result into UNCERTAIN.
3. **Critical, reproduced: the new odds were not read again before accepting.** The price was read when the dialog was found, then came a human-like pause, then the click. A price that dropped below the minimum during the pause was accepted (the mock placed at 1,60 with a minimum of 1,80, reported as 1,82). Fixed: read again immediately before the accept click; decline if no longer acceptable.
4. **Critical, reproduced: the accept / decline button was "the first match on the page".** The locator was `/akceptuj/i` page-wide; with a cookie banner's "Akceptuj wszystkie" earlier in the page the bot clicked that one (twice in the mock test), accepting cookies for the owner and never the odds. Fixed in code (inside the notice, else inside the bet slip, else page-wide; exactly one match or nothing is clicked) and in the YAML (anchored names; the bare-text fallback that could hit a label was removed).
5. **Critical: an already-clicked key could be answered with FAILED.** Order validation ran before the "already clicked" check, so a re-sent UNCERTAIN bet came back as FAILED when the executor was disabled or the order no longer validated - a "no bet" answer for a bet that may exist. Fixed: the already-clicked check comes first (test covers disabled executor, invalid stake, started event, halt).
6. **Major, reproduced: reconciliation could miss the very bet it looks for.** `fetch_bet_history(since)` compared the displayed time ("18:30", minute resolution, parsed as 18:30:00) with `since`; a bet placed at 18:30:40 and looked up with the attempt time 18:30:20 was filtered out as older, so an UNCERTAIN bet would be judged "not placed". Fixed: a row is kept when its displayed minute (or day) ends at or after `since`; the load-more stop condition uses the same rule.
7. **Major, reproduced: a `click` journal record could be unreadable.** (a) After a torn last line (crash inside a write) the next record was glued to the fragment and skipped as malformed. (b) A key containing U+2028 or U+0085 was written raw and split in two by `str.splitlines()`. In both cases the write-ahead record did not block the key. Fixed: a record always starts on its own line; records are ASCII-escaped and split on `\n` only (old files stay readable).
8. **Major: the journal was rewritten without the profile lock.** `BetclicExecutor.__init__` compacted the journal; a second process (`cs2bot executor login` while `cs2bot up` runs) could drop a record the running bot was appending. Fixed: compaction happens on the first placement after the lock is held.
9. **Major, reproduced: the dry run did not look for the final button.** The smoke test could not reveal a wrong `slip_place_button` selector, the most important one. Fixed: the dry run locates the one enabled button (never clicks it); `_click_place_button` additionally refuses to click unless the attempt is real, journaled and marked as clicked.
10. **Major: an unverifiable stake was sent.** The potential-win cross-check, the only check of what the site understood from "12,50", was skipped when its element did not resolve or held two numbers. Fixed: required by default (`behaviour.require_potential_win`), with an explicit opt-out for the owner.
11. **Major, reproduced: a bet placed by the selection click itself went unnoticed.** With a quick-bet account mode the slip stays empty and the attempt ended SELECTOR_ERROR, a "no bet" status. Fixed: a receipt that appears right after the outcome was clicked, or (when the selection did not become the slip's single item) a lower balance, makes the result UNCERTAIN and blocks the key (journal), also for dry-run orders.
12. **Major: nothing stopped a bet on a match that had started.** Orders wait in a queue (pacing gap, a login window of up to ten minutes). Fixed as far as possible inside the package: optional `event_live_indicator` selector (MARKET_UNAVAILABLE), and a kick-off check on `order.event_starts_at` that takes effect once `BetOrder` has the field (foundation change request), run before the browser and again right before the final click.
13. **Minor, reproduced: a selection suspended at the moment of the click was a SELECTOR_ERROR** (which pauses the bot). Fixed: the bookmaker's slip message is read first (MARKET_UNAVAILABLE).
14. **Minor: a missing or disabled final button on an underfunded account was a SELECTOR_ERROR.** Sites swap the button for a deposit button. Fixed: INSUFFICIENT_BALANCE when the stake exceeds the header balance.
15. **Minor: `_safe_url` accepted `http://` for an `https://` site** (the session cookie would travel in clear text). Fixed: same scheme, host and port as `base_url`.
16. **Minor: the trial click ran after the final reads**, so up to `action_timeout_s` could pass between verifying odds/stake and clicking. Fixed: actionability first, then the reads, the journal record, the halt check and the click; a receipt appearing during the pause also aborts.
17. **Minor:** NaN / Infinity in an order built with `model_copy` raised from `place` (now FAILED); `from_settings` needed a code change for every knob added to `ExecutorSettings` (now picks up same-named fields); a racy assertion in one new test.

#### Not changed on purpose
- The stake check stays a numeric comparison of the field value with the order stake (not a string comparison): the site may reformat "12,50" as "12,50 zł" or "12.50". What the site understood is covered by the potential-win check.
- No balance pre-check before the click (the header balance may exclude bonus funds); the bookmaker's own answer decides, except for the missing/disabled-button case above.

### Third pass

The first two passes were complete and correct in what they claimed (re-verified: all code read again, every check run, 345 tests passing before any change). This pass looked for what an adversarial page, a misconfigured locator or the owner's Windows environment can still do, and wrote a failing test (or probe) for each issue before fixing it. Every new browser test was also mutation-checked: with its fix reverted it fails.

#### Issues found and fixed
1. **Critical, reproduced: an odds-change dialog shown BEFORE the final click was ignored.** The pre-click checks read the slip's own price only. With Betclic offering 1,70 (minimum 1,80) in a dialog while the slip still read 1,85 and the button enabled, the bot clicked "Postaw zakład" (on the real site that click may count as accepting 1,70). With the button disabled by the dialog it ended as SELECTOR_ERROR, pausing the bot for an ordinary odds change. Fixed: any visible `odds_changed_notice` before the final click (after typing the stake, when the button is disabled, after the last pause, when the trial click fails, and in the final re-check) ends the attempt as ODDS_MOVED; nothing is clicked and the slip is cleared.
2. **Critical, reproduced: the dry run's clean-up could place a real bet.** Clearing the slip clicks unverified, partly page-wide locators (`slip_clear_confirm` = any `[role=dialog] button` starting with "Tak / Usuń / Potwierdź / Wyczyść"). In a slip rendered as a dialog the final button ("Potwierdź zakład", or "Postaw zakład" via a wrong locator) matched: the mock recorded a real place call from a DRY_RUN order. Fixed in code (housekeeping clicks refuse any element whose text contains "postaw" or that is, contains or sits inside a `slip_place_button` match; fail safe when it cannot be told) and in the YAML (`slip_clear_confirm` excludes "Potwierdź zakład" and any "postaw" text). Guarded: slip clear / confirm / item remove / open, receipt close (also after a placed bet), popup close, cookie banner.
3. **Major, reproduced: a dialog showing "old → new" price could be accepted below the minimum.** `odds_changed_new_odds` took the FIRST match; its shipped fallback is any `.oddValue` in the notice, so "1,85 → 1,70" with a lagging slip (1,85) was read as 1,85 and accepted. Fixed: every match is read and the lowest price counts (then the lower of that and the slip, as before).
4. **Major, reproduced: a receipt box carrying a refusal was reported as PLACED.** Any visible `bet_receipt` element whose text was not "pending" confirmed the bet, but the receipt locators include plain containers (`.betslip_receipt`, `.betslip_confirmation`). "Zakład odrzucony" in such a box became PLACED: the ledger would debit a stake for a bet that does not exist. Fixed: a receipt confirms only without a known error phrase, or with an explicit confirmation phrase (which wins: "Zakład przyjęty - kurs uległ zmianie" stays PLACED); otherwise the attempt keeps waiting and ends UNCERTAIN.
5. **Major, reproduced: `place()` could raise instead of answering.** (a) A stake such as `1E+30` (`model_copy` skips validation) made `Decimal.quantize` raise `InvalidOperation`; (b) an `OSError` of the profile lock (unwritable profile directory; on Windows a lock file being deleted) escaped `place`, `health_check` and `get_balance`; (c) an exception inside the error handling itself (after a click) escaped as well. The caller would have got an exception instead of FAILED / UNCERTAIN. Fixed: validation catches `ArithmeticError`; lock and browser start errors are FAILED / `ok=False` / `None`; a top-level safety net answers FAILED before the final click and UNCERTAIN (key blocked) after it. `ProfileLock.acquire` retries a transient `PermissionError` and raises `ProfileLockedError` when it persists on an existing lock file.
6. **Major: no halt check right before the outcome click.** In a quick-bet account mode that click places a bet by itself, and the last halt check could be a 20 s pacing gap plus page loads old. Fixed: the halt check also runs immediately before the outcome is clicked (KILL_SWITCH, slip left empty). `test_halt_switched_on_between_steps` was updated for the extra check.
7. **Minor, reproduced: the slip was not re-checked by NAME before the final click** (only the count and the odds). Fixed: the single item must still name the ordered selection (and market, when shown) right before confirming, else SELECTOR_ERROR.
8. **Minor: a promo popup covering the button paused the bot** (SELECTOR_ERROR). Fixed: overlays (`popup_close`, `cookie_banner_dismiss`, guarded as above) are dismissed once and the trial click repeated; an overlay that cannot be dismissed stays a SELECTOR_ERROR.

#### Verified without change
- Idempotency (memory + write-ahead journal, ordering of the journal record, halt check and click; definite results unblock, PLACED / UNCERTAIN block; a quick-bet UNCERTAIN of a dry run blocks the key); `_click_place_button` as the single, guarded place click; the acknowledge-mode re-click; post-click rejection cross-check; `fetch_bet_history` minute/day resolution; Polish number parsing/formatting and normalisation with adversarial inputs; URL guard; lock staleness and takeover; thread wrapper; credentials never logged; interfaces against `cs2bot.domain.betting`, `engine.ledger.apply_placement` and the API's login launcher / health provider.

### Remaining concerns
- The live site is still unverified. The strict checks make a wrong guess fail closed (SELECTOR_ERROR, UNCERTAIN) rather than bet wrongly, but the owner must expect to adjust the YAML during the DRY_RUN smoke test, in particular `slip_potential_win` (now required), the odds-change dialog buttons (anchored names) and `event_live_indicator`.
- Two Betclic account settings cannot be checked by the bot: automatic acceptance of changed odds and a possible quick-bet mode. They are documented in the YAML header and must be checked by hand.
- The executor cannot verify the event itself beyond the id in the URL, because `BetOrder` carries no team or event name, and it cannot refuse a started event until `BetOrder` carries `event_starts_at` (see the foundation change requests).
- The acknowledge-mode path (`odds_change_accept_places_bet: false`) re-clicks the place button once. It re-checks the receipt, messages, slip and stake after a pause first, but its safety on the real site depends on the flag matching the site's real behaviour.
- The integration layer must still mark bets PLACING before calling `place`, never re-send a PLACING bet, and reconcile UNCERTAIN results; the journal only guards against clicking twice for the same key, and a retry under a new key is a new bet.
- A post-click rejection is only as trustworthy as the Polish phrases in `texts.*` plus the balance / receipt cross-check; with an unreadable header balance the cross-check has only the receipt to go on.
- Test time is about 2 min 10-20 s with `-n 2` on this shared machine (121 browser tests), slightly above the ~2-minute guideline. Each browser test guards a money-safety property, so none was dropped; marking them `slow` would not shorten the default run (the marker is not deselected by default).
- (third pass) A wrong `slip_place_button` locator also weakens the housekeeping guard (it compares with that locator); the text check ("postaw") still applies. The DRY_RUN smoke test must show "place-bet button located" before real bets.
- (third pass) Pre-click odds notices are refused rather than accepted: in a market whose price ticks constantly the bot may keep answering ODDS_MOVED and re-pricing. Pre-match CS2 prices rarely move that fast; observe it during the first real bets.
- Telling a new odds-change dialog from the accepted one relies on the clicked element having gone and the offered price being different. A page that re-creates the accept button of the SAME dialog while its request is in flight and, in that moment, shows a different price would be taken for a new dialog (a second accept click). No way was found to rule this out without the real DOM; `max_odds_change_rounds: 0` in the settings disables accepting altogether (every odds change is declined) if the owner prefers.

## Review round 2

Independent second review against docs/dev/briefs/executor.md (requirements, addendum, review focus), starting from the state round 1 left (364 tests passing, re-verified first). All source files, the selector YAML, the mock site and the round-1 tests were read; the integration contracts were re-checked against `cs2bot.domain.betting`, `engine.ledger.apply_placement`, `engine.decision.min_acceptable_odds` and `docs/ARCHITECTURE.md` P9. Round 1 concentrated on the page before and around the final click; this round looked at what the bot concludes AFTER the click, at the profile lock / journal across processes, at the wrapper's failure and shutdown paths and at the bet-history parsing that reconciliation relies on. Every issue below was reproduced first (failing test or probe against the unmodified code), then fixed; each new browser test was mutation-checked (it fails with its fix reverted).

### Issues found and fixed
1. **Critical, reproduced: a rejected bet was reported PLACED.** Any visible `bet_receipt` element whose text carried no error phrase confirmed the bet - including an EMPTY text. The shipped receipt locators include plain containers (`.betslip_receipt`, `.betslip_confirmation`) that a site may render with a spinner before the answer arrives. Against the mock (receipt box with a spinner, then "Zakład odrzucony") the executor answered PLACED without a bet id; the ledger would debit a stake for a bet that does not exist. Fixed: a receipt confirms only on positive evidence - a `texts.bet_accepted` phrase (still beaten by a "pending" phrase), or a readable bet id (`receipt_bet_id` / `patterns.bet_id`) with no error phrase. An empty or unrecognised receipt is inconclusive: the bot keeps waiting and, without a final word, answers UNCERTAIN (key blocked, reconciled). YAML header and `bet_receipt` description updated.
2. **Major, reproduced: the same key could be clicked twice across two processes.** The journal / memory check ran before the profile lock was taken (and before the pacing gap of up to 20 s). A second process (`cs2bot bet run` next to `cs2bot up`) that got the lock after the first one clicked the key and released it would place the bet again. Fixed: the "already clicked" check runs again as soon as the lock is held.
3. **Major, reproduced: a lost profile lock did not stop the final click.** The lock was verified only at the start of an attempt; if another process broke it as stale mid-attempt (PC asleep longer than `lock_stale_after_s`), the journal was no longer single-writer but the bot still clicked. Fixed: `_journal_click` verifies the lock immediately before writing the write-ahead record; if it is not ours: FAILED, nothing clicked, nothing journaled, the page is left alone.
4. **Major, reproduced: an open bet could be read from the history as cashed out (or void).** Without the (unverified) `history_item_status` label the whole row text was classified with the first phrase in the order cashout, void, lost, won, open, so an open row with a "Cash out 9,80 zł" button became CASHOUT with a payout of 9,80 (a promo mentioning "zwrot" became VOID). Settlement from such a record would be wrong. Fixed: whole-row text that matches phrases of more than one status is UNKNOWN (a status label keeps the configured order); `payout` is None for OPEN and UNKNOWN rows (a potential win or cash-out offer is not a payout).
5. **Major: `ThreadBoundExecutor.place` answered FAILED ("no bet") for a key that was already clicked** when the executor could not be created (broken selector file after a restart). Fixed: new `clicked_check` parameter; `create_betclic_executor` passes `journal_clicked_check(settings, tuning)`, which answers UNCERTAIN for a key the journal blocks (read-only; dry runs and unknown keys fall through to FAILED).
6. **Minor, reproduced: the slip sanity check accepted another line.** `names_compatible` treated "." as a word boundary, so "Powyżej 26" fitted "Powyżej 26,5" and "Natus Vincere -1" fitted "Natus Vincere -1,5". Fixed: numbers are whole (no match next to "digit." / ".digit").
7. **Minor: the acknowledge-mode re-click did not re-check which selection the slip holds** (only count, odds and stake). Fixed: `_recheck_slip_selection` before the second click; a mismatch ends UNCERTAIN without a second click (test added).
8. **Minor, reproduced: date-only history rows on the day the clocks go back ended an hour early** (`_latest_possible` added 24 h in UTC to a 25-hour local day), so a bet placed 23:00-24:00 local could be filtered out by `since`. Fixed: the next local midnight.
9. **Minor: shutting down during a login window waited up to its timeout (600 s)** - the worker thread is not a daemon, so the process could not exit. Fixed: thread-safe `BetclicExecutor.abort_login()`, called by `ThreadBoundExecutor.close()` before the shutdown is queued.

### Verified without change
- Requirements 1-5 and the addendum point by point: nothing missing or stubbed; `ExecutorTuning.from_settings` is used by `create_betclic_executor` and by `BetclicExecutor`, explicit `tuning=` wins.
- Review focus re-derived on the current code: halt checks (start, before the outcome click, right before the final click after the journal record, before accepting changed odds, before an acknowledge re-click); minimum odds on every read incl. both dialog reads (lower of dialog and slip); stale / hidden slip items; stake read back numerically before the final click plus the potential-win cross-check; dry run cannot reach `_click_place_button` and housekeeping clicks refuse the place button; KILL_SWITCH after the journal record unblocks the key (result record), a failed result write keeps it blocked (safe).
- Polish number parsing (NBSP / thin-space thousands, "1.234,56", several numbers -> refused), stake formatting, URL guard (userinfo, scheme downgrade, ports, protocol-relative paths), selector parameter injection guard, credentials never logged (proxy URL, keyring, prefill), screenshot names sanitised.
- Lock: exclusive create, stale detection, takeover put-back, transient read errors self-heal through re-acquire; journal: torn lines, ASCII escaping, retention, compaction only under the lock.
- `engine.decision.min_acceptable_odds` (3 decimals, Decimal) and `engine.ledger.apply_placement` (UNCERTAIN -> PLACING, placed_odds below minimum only logged) agree with the executor's statuses.

### Remaining concerns
- All selectors are still unverified guesses; with fix 1 the first real bets may end UNCERTAIN if Betclic's receipt wording is not in `texts.bet_accepted` and no bet id is readable (safe: reconciled via history). The DRY_RUN cannot exercise the receipt: watch the first real placements and add the wording.
- History status relies on `history_item_status` resolving; until it is verified, rows that show several status words come back UNKNOWN (settlement must then use match results, not the history).
- The potential-win cross-check catches x10 / x100 misreads but not small ones (12 read as 12,50 is within the 0.70-1.05 band); the stake field itself is compared numerically.
- A post-click rejection with an unreadable header balance and no receipt is still trusted (documented in round 1).
- Test time is about 2 min 30 s with `-n 2` on this shared machine (130 browser tests), above the ~2-minute guideline; every browser test guards a money-safety property.

### Foundation change requests (unchanged from round 1, still open)
- `BetOrder.event_starts_at` (kick-off check is inactive without it) and `BetOrder.event_name` (event verification beyond the URL id); the four `ExecutorSettings` knobs (`max_odds_rise_factor`, `potential_win_min_ratio`, `potential_win_max_ratio`, `journal_retention_days`); ARCHITECTURE.md P9 documentation of `close()`, `AccountExecutor`, `ThreadBoundExecutor`, `create_betclic_executor`, the double dry-run switch and the placement journal.
