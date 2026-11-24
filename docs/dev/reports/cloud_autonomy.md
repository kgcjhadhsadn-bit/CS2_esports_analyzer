# cloud_autonomy: implementation report

Tests: `.venv/bin/python -m pytest tests/services/test_autonomy_service.py tests/services/test_telegram_service.py tests/services/test_autonomy_cli_doctor.py tests/api/test_api_autonomy.py tests/executor/test_executor_autologin.py -q` -> 72 passed, 0 failed (plus 2 new decision tests in tests/engine, 5 new vitest tests in `dashboard/src/components/TradingControls.test.tsx`; dashboard `npm run lint && npm test && npm run build`: 84 passed, build OK). Whole suite `.venv/bin/python -m pytest tests -q -n 2`: 3130 passed, 20 skipped, 1 failed - the failure belongs to deep_data's in-progress work (see "Known limitations"); `tests/foundation`: 37 passed; ruff / ruff format / mypy (201 files) clean on my paths.

## Summary

The bot can now run unattended on the VPS (PLAN.md 0 / 0b):

* **Settings** (`config.py`, added with Edit only): `AutonomySettings` (`autonomy.*`: `enabled`, `auto_login`, `auto_resume`, `auto_resume_reasons`, `check_interval_min`, `circuit_breaker_limit` / `_window_h`, `login_retry_interval_min`, `twofa_retry_interval_min`, `balance_recheck`), `TelegramSettings` (`telegram.*`: `enabled`, `bot_token: OptionalSecret`, `chat_id`, `daily_report_time` (Warsaw), `notify_*` per event family, transport knobs), `executor.headless_without_display`, `executor.login_submit_timeout_s`, `decision.require_trusted_calibration`. Everything is off by default; `.env.example` documents the VPS values.
* **Structured pause reasons** (`db/state.py`, `domain/control.py`): `system_state["paused"]` keeps a list of `{code, message, since, actor, count, details}`; the bot is paused while any reason is active. Every automatic pause now records its code: `loss_limit_daily` / `loss_limit_weekly` (`engine.bankroll.check_loss_limits`, one reason per limit with `details.period`), `balance_mismatch` (`engine.ledger.reconcile`), `uncertain_bet`, `login_required`, `selector_error` (`services.betting`), `2fa_required` (auto-login). Pauses by a person are `manual` (dashboard, Telegram, CLI). The joined text (`get_pause()`, `paused.reason`) is unchanged for old readers; a pause stored in the old format reads as one owner-only `other` reason.
* **Auto-login** (`executor`): `BetclicExecutor.auto_login()` fills AND submits the login form with the env/keyring credentials, then waits for: logged in / an SMS-2FA step (stops there, never touches a code) / a wrong-password or account-locked message (classified with new `texts` phrases) / timeout. New optional selectors `login_submit_button`, `login_2fa_step`, `login_error` in `config/betclic_selectors.yaml`. Credentials never logged, no screenshot of the filled form. The mock site got a real login flow (ok / 2FA step / wrong password / locked / silent) and browser tests.
* **Auto-resume service** (`services/autonomy.py`, job `autonomy_check`, fast lane after settlement): per reason - next Warsaw midnight / Monday, no real `PLACING` bet left, logged in (owner) or auto-login success (with retry back-off: 15 min doubling, 2FA every 240 min, wrong password / locked / no credentials stop until an owner resume or a restart), balance mismatch re-read + reconciled (books any remaining difference; without a readable balance the entry booked at detection is accepted; waits while unconfirmed stakes could explain a shortfall), selector / executor errors after a fresh passing health check. Never the kill switch, never `manual` / `circuit_open` / `other`. Circuit breaker: a reason that already auto-resumed `circuit_breaker_limit` times in the window gets `circuit_open` (owner-only) instead. Every auto-resume -> `audit_log` (`auto_resume`, `circuit_open`, `auto_login`, `balance_adjustment_accepted`) -> Telegram.
* **Telegram** (`services/telegram.py`): send-only Bot API client (rate-limited, retries on network / 5xx / 429 `retry_after`, `send` never raises, the token is redacted from every error and log line), a notifier job that reads new events from the database with a persisted cursor (placed bets with match / market / selection / odds / stake / p / EV, settlements, every pause / resume / auto-resume / circuit breaker / kill switch / auto-login / UNCERTAIN / balance event, failing jobs rate-limited per job), the daily P&L report job at `telegram.daily_report_time`, startup / shutdown messages from `run_up`, and the long-polling command job (`/status /balance /bets /pause /resume /stop /go /help`; only the configured chat; stale commands > 10 min ignored; offset persisted per update so a restart never replays a command). Reading the DB instead of hooking the betting code means notifications can never slow down or break a placement.
* **Wiring**: `flows/jobs.py` (jobs `autonomy_check`, `telegram_notify`, `telegram_daily_report`, `telegram_commands`; new lanes `notify` and `telegram`; `Runtime.telegram`), `flows/supervisor.py` (Telegram client, start/stop messages, auto-login block reset at start-up), `cs2bot doctor` (Telegram token / chat id + `getMe`, autonomy credentials incl. keyring, browser display), CLI `cs2bot telegram test`, `cs2bot control status` (every reason with auto/owner, ETA, circuit breaker, autonomy / Telegram mode).
* **Headless / server**: `executor.session.effective_headless()` - a visible browser on Linux without `DISPLAY` / `WAYLAND_DISPLAY` falls back to headless (setting `executor.headless_without_display`, doctor warns); the VPS runs it headful on Xvfb (`DISPLAY=:99`, documented in OPERATIONS.md). A crashed Chromium is relaunched by the next call (test added).
* **Decision rule** (user request + owner decision 2026-11-24, committed as `decision.min_ev = -1.0`): `engine.decision` treats `min_ev <= -1` as "EV rule off" (no `low_ev` reason, `min_acceptable_odds` keeps only the tolerance / min-odds floors), `ev_check_enabled()`; `require_trusted_calibration` + `trusted_markets` adds `untrusted_calibration`. Engine tests updated (EV-rule tests now enable the rule explicitly; a new test proves odds are ignored by default, e.g. p 0.89 at 1.10 is accepted). The dashboard settings page shows the bet rule ("confidence only, odds ignored") and accepts `min_ev` in [-1, 1]; the API's config validation too. Also fixed an `engine.ev.kelly_for_net_odds` underflow (ZeroDivisionError for a subnormal settled probability, found by hypothesis).
* **API / dashboard**: `GET /api/control/state` (and `/api/health` `trading`) add `pause_reasons` (code, label, message, since, actor, count, auto_resume, eta, last_check), `circuit_open`, `circuit_codes`, `autonomy`, `telegram`; health alerts `circuit_open`, `betclic_2fa_required`, `auto_login_blocked`, `telegram_not_configured`, `telegram_failing`. Dashboard: structured reason list in the pause control (auto + ETA vs "needs you", circuit-breaker banner), an "Autonomy & Telegram" card on Overview and Health, the bet-rule note on Settings.
* **Docs**: OPERATIONS.md (autonomous operation, pause-reason table, what each Telegram alert means, the two cases that still need the owner, jobs, tuning), ARCHITECTURE.md, API.md, README.md, CLAUDE.md, STATUS.md.

## Public API

### Settings (`cs2bot.config`)

```python
settings.autonomy: AutonomySettings   # enabled=False, auto_login=False, auto_resume=True,
                                      # auto_resume_reasons=[...8 codes], check_interval_min=5.0,
                                      # circuit_breaker_limit=3, circuit_breaker_window_h=24.0,
                                      # login_retry_interval_min=15.0, max_login_failures=3 (review 1),
                                      # twofa_retry_interval_min=240.0, balance_recheck=True
settings.telegram: TelegramSettings   # enabled=False, bot_token: SecretStr|None, chat_id: str|None,
                                      # api_base, daily_report_time=time(21,0), notify_bets, notify_dry_run_bets,
                                      # notify_settlements, notify_control, notify_errors, notify_lifecycle,
                                      # daily_report, commands_enabled, request_timeout_s=15, max_retries=3,
                                      # min_send_interval_s=1.1, poll_timeout_s=5, poll_session_s=600,
                                      # notify_interval_s=30, error_alert_cooldown_min=360
settings.executor.headless_without_display: bool = True
settings.executor.login_submit_timeout_s: float = 30.0
settings.decision.require_trusted_calibration: bool = False
```

Env: `CS2BOT_AUTONOMY__ENABLED=true`, `CS2BOT_AUTONOMY__AUTO_LOGIN=true`, `CS2BOT_BETCLIC__CREDENTIALS_SOURCE=env` + `USERNAME` / `PASSWORD`, `CS2BOT_TELEGRAM__ENABLED=true`, `CS2BOT_TELEGRAM__BOT_TOKEN`, `CS2BOT_TELEGRAM__CHAT_ID`, `CS2BOT_EXECUTOR__HEADLESS=false` + `DISPLAY=:99` (Xvfb).

### Domain (`cs2bot.domain.control`)

```python
class PauseCode(StrEnum): MANUAL, LOSS_LIMIT_DAILY, LOSS_LIMIT_WEEKLY, UNCERTAIN_BET, LOGIN_REQUIRED,
    TWO_FA_REQUIRED ("2fa_required"), BALANCE_MISMATCH, SELECTOR_ERROR, EXECUTOR_UNAVAILABLE, CIRCUIT_OPEN, OTHER
AUTO_RESUMABLE: frozenset[PauseCode]; OWNER_ONLY: frozenset[PauseCode]; PAUSE_CODE_LABELS: dict[str, str]
def parse_pause_code(value: object) -> PauseCode            # unknown -> OTHER (owner-only)
class PauseReason(BaseModel): code, message, since, actor="system", count=1, details={}; .auto_resumable
class LoginStatus(StrEnum): LOGGED_IN, ALREADY_LOGGED_IN, TWO_FA_REQUIRED, BAD_CREDENTIALS, ACCOUNT_LOCKED,
    NO_CREDENTIALS, FORM_NOT_FOUND, FAILED; .stops_retrying, .logged_in
class LoginAttempt(BaseModel): status: LoginStatus, message: str, at: datetime, submitted: bool = False
def circuit_codes(reasons: Iterable[PauseReason]) -> set[str]   # codes held by circuit_open (details codes/code)
```

### State (`cs2bot.db.state`)

```python
add_pause_reason(session, code: PauseCode | str, message: str, *, actor="system", details=None, at=None,
                 replace=False) -> bool          # True when it paused a running bot; same code is merged
resolve_pause_reasons(session, codes, *, actor: str, note="") -> list[PauseReason]   # unpauses when none left
get_pause_reasons(session, *, lock=False) -> list[PauseReason]   # lock=True: SELECT ... FOR UPDATE
# every writer (add_pause_reason, resolve_pause_reasons, set_paused) locks the "paused" row first
set_paused(session, on, *, actor, reason="", code=PauseCode.MANUAL)   # on=False clears EVERY reason (owner),
                                      # and a stopped auto-login (blocked / submit_failures reset)
get_pause(session) -> (on, joined_text)  /  is_trading_halted(session)  # unchanged
# keys: AUTO_LOGIN="auto_login", AUTONOMY="autonomy", TELEGRAM="telegram"; MAX_PAUSE_REASON_CHARS=2000
```

`engine.ledger.pause_trading(session, reason, *, actor="system", code=PauseCode.OTHER, details=None) -> bool` (same return semantics as before). Wiring example:

```python
from cs2bot.domain.control import PauseCode
ledger.pause_trading(session, "Betclic page layout changed: ...", actor="betting", code=PauseCode.SELECTOR_ERROR)
```

### Executor

```python
BetclicExecutor.auto_login(timeout_s: float | None = None) -> LoginAttempt
ThreadBoundExecutor.auto_login(timeout_s=None) -> LoginAttempt      # FAILED if the wrapped executor has none
FakeExecutor(..., auto_login_results: Iterable[LoginStatus] = ()).auto_login(timeout_s=None)
cs2bot.executor.AutoLoginExecutor        # runtime-checkable Protocol with auto_login
cs2bot.executor.session.has_display(environ=None, platform=None) -> bool
cs2bot.executor.session.effective_headless(settings, *, environ=None, platform=None) -> bool
```

### Services

```python
from cs2bot.services.autonomy import AutonomyService, pause_overview, clear_login_block
service = AutonomyService(session_factory, settings, executor, health_cache=health_cache)
report = service.run_once()          # AutonomyReport: .checks[ReasonCheck(code, status, detail, eta)],
                                     # .resumed, .circuit_opened, .login, .errors, .as_dict()
pause_overview(session, settings, *, now=None) -> dict   # paused, reasons[{code,label,message,since,actor,count,
                                     # auto_resume,eta,last_check,details}], circuit_open, circuit_codes,
                                     # autonomy_enabled, auto_login_enabled, last_check_at, last_login
auto_resume_count(session, code, *, since) -> int
last_owner_resume(session, *, since) -> datetime | None     # the breaker window restarts at the owner's resume
clear_login_block(session, *, actor, startup=False) -> bool # startup=True clears only a no_credentials block
next_local_midnight(now, tz) / next_local_monday(now, tz) -> datetime

from cs2bot.services.telegram import TelegramClient, TelegramNotifier, TelegramCommands
client = TelegramClient.from_settings(settings)          # None unless enabled + token + chat id
client.send(text, *, chat_id=None, silent=False, attempts=None, timeout_s=None) -> bool  # never raises;
                                                         # .last_failure_permanent: refused for its content (400)
client.call(method, payload, *, timeout_s=None, attempts=None); TelegramError(msg, *, status_code=None)
truncate_message(text, limit=4096) -> str                # UTF-16 code units, like Telegram counts
client.get_updates(offset, *, timeout_s=None) / client.get_me() / client.status() / client.close()
TelegramNotifier(session_factory, settings, client).scan_once(now=None) -> int  # messages sent
TelegramNotifier(...).daily_report(now=None) -> bool
TelegramCommands(session_factory, settings, client).handle_update(update) -> str | None
TelegramCommands(...).poll_once() -> int ; .poll(should_stop=..., session_s=..., sleep=...) -> int
daily_report_text / status_text / balance_text / bets_text / format_audit / redact

from cs2bot.services.betting import trusted_markets, load_confidence_guard
trusted_markets(bundle, settings=None) -> set[str] | None  # deep_data ConfidenceGuard file first, then
                                                           # bundle.market_trust; None = unknown
load_confidence_guard(settings) -> ConfidenceGuard | None  # models.confidence_guard_file (malformed: None)
from cs2bot.services.doctor import check_telegram, default_telegram_probe, check_autonomy, check_display
run_checks(settings, *, chromium_finder=None, telegram_probe=None)
```

### Flows

* `flows.jobs`: lanes `NOTIFY="notify"`, `TELEGRAM="telegram"`; `Runtime.telegram: TelegramClient | None`; jobs `autonomy_check` (fast, enabled with `autonomy.enabled`), `telegram_notify`, `telegram_daily_report` (notify), `telegram_commands` (telegram) - enabled only with a client.
* `flows.supervisor.run_up(settings, ..., telegram_factory: Callable[[Settings], Any] | None = None, ...)` - builds the client (`TelegramClient.from_settings` by default), sends "cs2bot started ..." / "cs2bot is stopping ..." (the latter one try, `LIFECYCLE_SEND_TIMEOUT_S` = 5 s), closes it; at start-up clears an auto-login stopped for missing credentials only. `runtime_overrides={"telegram": ...}` also works.

### Engine (`engine.decision`)

```python
evaluate(priced, *, match, settings, betclic, now, trusted_markets: Collection[str] | None = None) -> BetDecision
evaluate_all(priced, *, match, settings, betclic, now, trusted_markets=None) -> list[BetDecision]
ev_check_enabled(min_ev: float) -> bool ; EV_CHECK_DISABLED_AT = -1.0 ; ReasonCode.UNTRUSTED_CALIBRATION
```

### API / CLI

* `GET /api/control/state`, `POST /api/control/pause|resume` responses and `GET /api/health` `.trading`: new fields `pause_reasons`, `circuit_open`, `circuit_codes`, `autonomy`, `telegram` (schemas `PauseReasonOut`, `AutonomyStatusOut`, `TelegramStatusOut`; see docs/API.md). `trading_state(session, settings=None)`.
* CLI: `cs2bot telegram test [--message TEXT]`, `cs2bot control status` (extended), `cs2bot doctor` (new checks; an `@channel` chat id warns that commands need the numeric id), `cs2bot backtest` also writes deep_data's confidence guard to `models.confidence_guard_file` (review 1).

## Known limitations

* **Whole suite:** the last full run (`pytest tests -q -n 2`) had exactly one failure, `tests/ingest/pandascore/test_pandascore_sync.py::test_missing_token_is_reported_not_raised`, caused by deep_data's in-progress "PandaScore optional" change in `ingest/pandascore/sync.py` (not in my paths; deep_data updates that test). deep_data also has unformatted / long lines in its in-progress files (`backtest/deep_eval.py`, `features/deep.py`, `features/rows.py`, `tests/odds/test_deep_odds_movement.py`, `tests/ingest/hltv/test_deep_hltv_proxy.py`); `ruff` on my paths is clean.
* **Trusted calibration flag** (updated in review round 1): wired. `services.betting.trusted_markets(bundle, settings)` reads deep_data's `ConfidenceGuard` from `models.confidence_guard_file` (written by `cs2bot backtest`; `guard.is_trusted` per market type), falling back to a bundle `market_trust` attribute; with `decision.require_trusted_calibration` on and no verdict at all, nothing is trusted (fail safe). Still open for the final review: deep_data's `guard.adjust` (conservative shrinking of probabilities above 0.75) is not applied to the decision probabilities, and the nightly job does not refresh the guard (only `cs2bot backtest` writes it).
* **Staking tiers** (`bankroll.staking_mode = "confidence_tiers"`, committed by the owner) are the final review's job (docs/dev/briefs/final_review.md); `engine.bankroll.size_stake` still sizes with Kelly + caps. My docs describe the bet rule, not the tiers.
* Unverified against the live services: the Betclic login form (selectors `login_*`, texts `login_*` are best guesses like the rest of the file), the 2FA step wording, and the Telegram Bot API (built from its documentation; tests use respx).
* Balance-mismatch auto-resume trusts the balance the executor reads (owner chose "auto-resume everything"); a misread balance is booked as a reconciliation entry. The circuit breaker bounds repeats.
* A `2fa_required` login is retried every `twofa_retry_interval_min` (240 min) because the SMS step may disappear; each retry may send an SMS to the owner.
* The Telegram poller runs inside the scheduler (one `poll_session_s` session per job run, 5 s long-poll), so a shutdown waits up to ~5 s for it; the daily-report job also fires once at the first start (scheduler catch-up of the last slot).
* `executor_unavailable` is a supported code with its auto-resume rule, but nothing pauses with it yet (an unavailable executor only stops the current cycle, as before).
* Notifier ids: placed bets are found by id above the first-scan watermark (database `created_at` defaults have second precision on SQLite / the server clock), settlements by `settled_at`.

## Foundation change requests

None required (cloud_autonomy owns `db/`, `domain/` and adds sections to `config.py` itself). For awareness of the final review:

* `src/cs2bot/config.py` `Settings._validate`: consider validating `telegram.enabled` with a missing token / chat id and `autonomy.auto_login` with `betclic.credentials_source == "none"` at start-up (today `cs2bot doctor` fails and the dashboard alerts instead, so the bot still starts).
* No Alembic migration was needed: the structured reasons live in the existing `system_state` JSON value.

## New dependency requests

None (httpx, respx, Playwright and typer were already dependencies).

## Review round 1

Independent review against docs/dev/briefs/cloud_autonomy.md (no Addendum / Review-focus section exists in the brief; the task's special focus was applied). Everything below was re-derived from the code and exercised with tests; the fixes are in the package's owned paths.

### What was verified

* **Kill switch / manual pause are never undone automatically.** `AutonomyService` only ever calls `resolve_pause_reasons` for codes in `AUTO_RESUMABLE` (never `manual`, `circuit_open`, `other`) and never touches `kill_switch`; `is_trading_halted` checks the kill switch first, so a lifted reason cannot re-enable betting while it is on. Telegram `/resume` and the dashboard resume clear pause reasons only; `/go` (owner) is the only remote way to turn the kill switch off, as the brief asks. Unknown / legacy pause codes read as owner-only `other`.
* **No auto-resume while the cause persists:** loss limits lift only after the Warsaw day / week of the pause (DST-safe), `uncertain_bet` only when no real bet is left `PLACING`, `login_*` only after a logged-in check / successful auto-login, `selector_error` only after a health check made *after* the pause, `balance_mismatch` waits while unconfirmed stakes could explain a shortfall. The autonomy job runs in the money lane, so it never interleaves with a betting cycle.
* **Auto-login:** fills and submits, never types a code; the SMS step returns `2fa_required` (retried every 240 min); a recognised wrong password / locked account stops at once; no screenshot of the filled form; credentials are not logged.
* **Telegram:** token only in the URL, httpx logging is at WARNING, every error text is redacted; commands only from the configured chat; stale commands ignored; the offset is persisted per update.
* **Decision rule:** PLAN.md section 0 (owner decision 2026-11-24, made after the brief's "break-even guard" wording) and the user's request ("I do not want anything 'value', even a 1.28 wins") both say *confidence only, odds ignored*: `decision.min_probability = 0.75`, `decision.min_ev = -1.0` (EV rule off). `engine.decision`, the API config validation, the dashboard settings page ("odds ignored ... even 1.28"), `.env.example`, README, CLAUDE.md and OPERATIONS.md agree. Two stale Windows-guide lines (`MIN_EV` "0.03 = +3%", "bets only when the price is profitable after tax") were fixed. The task text's "EV >= 0" is the superseded break-even variant; `min_ev = 0.0` still gives it.

### Issues found and fixed (each with a regression test)

1. **Lost update between pause writers (critical, could undo a manual pause).** `add_pause_reason` / `resolve_pause_reasons` / `set_paused` did an unlocked read-modify-write of `system_state["paused"]` from different threads (autonomy in the fast lane, Telegram commands, the dashboard, betting). On PostgreSQL (READ COMMITTED) an auto-resume that read `[login_required]` could write `[]` over a `manual` pause committed a moment earlier, unpausing the bot. Fixed: every writer reads the row `SELECT ... FOR UPDATE` (+ `populate_existing`) via `db.state._pause_row` / `get_pause_reasons(lock=True)`; the autonomy service takes the same lock for its whole resume / circuit-breaker decision, and the lock order is `paused` -> `auto_login` everywhere (no deadlock between `/resume` and an auto-login). Tests: `test_pause_writers_lock_the_pause_row`, `test_concurrent_pause_and_auto_resume_on_postgres` (`@postgres`, skipped without a database).
2. **Circuit breaker loop / alert spam with two codes (major).** The `circuit_open` reason stored one `details["code"]`; when a second code tripped, the merge overwrote it, so the first code was checked again every 5 minutes (auto-login attempts, health checks) and re-opened the breaker each time with a new Telegram alert. Fixed: `details["codes"]` keeps every held code (`domain.control.circuit_codes`, also used by `pause_overview`). Test: `test_two_codes_tripping_the_breaker_both_stay_held`.
3. **Circuit breaker re-opened right after the owner's resume (minor).** Old auto-resumes in the 24 h window counted after the owner had checked and resumed, so the next genuine pause of that code opened the breaker at once. Fixed: the window starts at the owner's last resume (`last_owner_resume`). Test: `test_the_owner_resume_restarts_the_breaker_window`.
4. **Auto-login could hammer Betclic (major).** (a) An error text the classifier does not know (a reworded "wrong password") or a silent page counted as `failed` and was retried forever with back-off - enough submissions to lock the account. Now `autonomy.max_login_failures` (3) submitted attempts in a row that neither log in nor reach the SMS step stop the auto-login until the owner resumes (`LoginAttempt.submitted`; the SMS step proves the password and resets the budget). (b) Start-up cleared every stopped auto-login, so a crash-looping container resubmitted a wrong password on each restart; now a restart clears only `no_credentials` (nothing reached Betclic), everything else needs the owner's resume - which is what the Telegram alert already asked for. (c) After the owner logged in by hand (e.g. past the SMS step), an old 2FA interval / back-off still delayed the next auto-login by up to 4 h; a confirmed login now resets it. Tests: `test_unrecognised_login_failures_stop_after_the_limit`, `test_the_sms_step_resets_the_failure_budget`, `test_startup_clears_only_a_block_that_never_reached_betclic`, `test_wrong_password_stops_the_retries_until_the_owner_acts` (updated), `test_a_manual_login_clears_an_old_login_back_off`, `test_run_up_sends_startup_and_shutdown_messages` (updated).
5. **Password could leak through a browser error (major).** Playwright's call log contains `fill("<value>")`; `auto_login` caught only some exception types, and the autonomy service stored `str(exc)` (300 chars, call log included) in `system_state`, `audit_log` and Telegram. Fixed: `auto_login` turns any exception into a `FAILED` outcome, keeps the first line and scrubs the password and username (`_scrub`); the service keeps only the first line of an unexpected exception. Tests: `test_a_browser_error_never_leaks_the_credentials` (browser), `test_a_crashing_auto_login_reports_one_line_only`.
6. **Telegram commands accepted by username (major, security).** With an `@name` chat id, any private chat whose *username* matched was obeyed; usernames can be changed and re-registered by someone else. Now only the numeric chat id (an int, as Telegram sends it) is obeyed; `cs2bot doctor` warns that an `@channel` id gets notifications but no commands. Test: `test_only_the_numeric_chat_id_may_command_the_bot` (5 cases).
7. **Failed long jobs were never reported (major).** Job failures were looked up by `started_at >= last scan`, so a job still running at the previous scan (retrain, HLTV sync) that failed afterwards was skipped. Now by `finished_at` with a 2-minute overlap and remembered run ids. Test: `test_a_long_job_failing_after_the_scan_is_still_reported`.
8. **One refused message blocked every later notification (minor).** A 400 for a message's content (e.g. too long) kept the cursor on that event forever. Such a message is now skipped (`TelegramClient.last_failure_permanent`; a 400 about the chat is still retried); truncation counts UTF-16 code units like Telegram. Tests: `test_a_message_telegram_refuses_is_skipped_not_retried_forever`, `test_a_chat_error_is_retried_on_the_next_scan`, `test_messages_are_truncated_in_utf16_units`.
9. **Shutdown could be delayed ~1 minute by Telegram (minor).** The "stopping" message used the full retry policy (4 tries x 15 s timeouts + back-off) before the bounded shutdown started. Now one try, 5 s, without waiting on another message's send lock. Test: `test_a_bounded_send_gives_up_quickly` + the run_up test.
10. **Token-bearing transport errors (minor, hardening).** A non-`HTTPError` from httpx (e.g. `InvalidURL`) escaped `call` unredacted with its traceback. Now wrapped in a redacted `TelegramError` raised `from None`. Test: `test_an_unexpected_transport_error_is_wrapped_and_redacted`.
11. **Brief item "wire deep_data's trusted flag when its report describes it" (requirement gap).** docs/dev/reports/deep_data.md now documents `ConfidenceGuard`; it was not wired (only a speculative bundle attribute was read). Now `trusted_markets(bundle, settings)` reads the guard file, `cs2bot backtest` writes it and prints the trusted market types, and with `require_trusted_calibration` on but no verdict nothing is bet (fail safe; previously "unknown" meant "no check"). Tests: `test_trusted_markets_from_the_confidence_guard_file`, `test_require_trusted_calibration_is_fail_safe_in_the_betting_cycle`, `test_backtest_writes_the_confidence_guard`.
12. Docs: OPERATIONS.md (login stop rules, `MAX_LOGIN_FAILURES`, numeric chat id, trusted calibration, the Kelly caveat below), `.env.example` (`CS2BOT_AUTONOMY__MAX_LOGIN_FAILURES`), WINDOWS_SETUP.md (stale EV rule).

### Remaining concerns

* **"Odds ignored" is not yet effective end to end.** The decision accepts a 75%+ pick at any price, but `engine.bankroll.size_stake` still sizes with fractional Kelly, which stakes 0 (`no_edge`) when the EV after tax is not positive - so a 1.28 favourite at p = 0.80 (EV -0.10) is accepted and then not bet. The confidence-tier staking that makes the owner's rule real is assigned to the final review (docs/dev/briefs/final_review.md); it must land before the rule behaves as the owner expects.
* `guard.adjust` (deep_data's conservative shrinking above 0.75) is not applied to decision probabilities, and no scheduled job refreshes the confidence guard (only `cs2bot backtest`) - final review.
* A Telegram **group** chat id lets every group member command the bot (the brief's "only from chat_id" is met); a private chat with the bot is recommended.
* Balance-mismatch auto-resume still accepts the adjustment booked at detection when the balance cannot be re-read (per the owner's "auto-resume everything"); the circuit breaker bounds repeats.
* `SELECT ... FOR UPDATE` protects PostgreSQL (production); SQLite (tests / development) relies on its database lock and is not protected against the same interleaving. The PostgreSQL concurrency test only runs with `CS2BOT_TEST_DATABASE_URL`.
* The SMS-step retry every 240 min may send the owner an SMS each time (unchanged, documented).
* `executor_unavailable` still has no producer.

### Foundation change requests (review round 1)

* `docs/ARCHITECTURE.md` (not editable in this review): in the `engine.decision.evaluate` bullet, add that the trusted set comes from `services.betting.trusted_markets(bundle, settings)` reading deep_data's `ConfidenceGuard` (`models.confidence_guard_file`, written by `cs2bot backtest`), and that a required-but-unknown verdict trusts nothing.

### Checks (review round 1)

* Whole suite `.venv/bin/python -m pytest tests -q -n 2`: 3153 passed, 21 skipped, 0 failed (run during the review; the last edits were re-verified with `tests/services tests/api tests/executor tests/engine tests/foundation`: 1391 passed, 3 skipped, browser tests included).
* `ruff check` / `ruff format --check` on the owned paths: clean; `mypy` (201 files): clean. Dashboard unchanged in this round (no npm run needed).
