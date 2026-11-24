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
                                      # login_retry_interval_min=15.0, twofa_retry_interval_min=240.0,
                                      # balance_recheck=True
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
class LoginAttempt(BaseModel): status: LoginStatus, message: str, at: datetime
```

### State (`cs2bot.db.state`)

```python
add_pause_reason(session, code: PauseCode | str, message: str, *, actor="system", details=None, at=None,
                 replace=False) -> bool          # True when it paused a running bot; same code is merged
resolve_pause_reasons(session, codes, *, actor: str, note="") -> list[PauseReason]   # unpauses when none left
get_pause_reasons(session) -> list[PauseReason]
set_paused(session, on, *, actor, reason="", code=PauseCode.MANUAL)   # on=False clears EVERY reason (owner)
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
next_local_midnight(now, tz) / next_local_monday(now, tz) -> datetime

from cs2bot.services.telegram import TelegramClient, TelegramNotifier, TelegramCommands
client = TelegramClient.from_settings(settings)          # None unless enabled + token + chat id
client.send(text, *, chat_id=None, silent=False) -> bool # never raises
client.get_updates(offset, *, timeout_s=None) / client.get_me() / client.status() / client.close()
TelegramNotifier(session_factory, settings, client).scan_once(now=None) -> int  # messages sent
TelegramNotifier(...).daily_report(now=None) -> bool
TelegramCommands(session_factory, settings, client).handle_update(update) -> str | None
TelegramCommands(...).poll_once() -> int ; .poll(should_stop=..., session_s=..., sleep=...) -> int
daily_report_text / status_text / balance_text / bets_text / format_audit / redact

from cs2bot.services.betting import trusted_markets   # bundle -> set[str] | None (see limitations)
from cs2bot.services.doctor import check_telegram, default_telegram_probe, check_autonomy, check_display
run_checks(settings, *, chromium_finder=None, telegram_probe=None)
```

### Flows

* `flows.jobs`: lanes `NOTIFY="notify"`, `TELEGRAM="telegram"`; `Runtime.telegram: TelegramClient | None`; jobs `autonomy_check` (fast, enabled with `autonomy.enabled`), `telegram_notify`, `telegram_daily_report` (notify), `telegram_commands` (telegram) - enabled only with a client.
* `flows.supervisor.run_up(settings, ..., telegram_factory: Callable[[Settings], Any] | None = None, ...)` - builds the client (`TelegramClient.from_settings` by default), sends "cs2bot started ..." / "cs2bot is stopping ...", closes it; clears a stopped auto-login at start-up. `runtime_overrides={"telegram": ...}` also works.

### Engine (`engine.decision`)

```python
evaluate(priced, *, match, settings, betclic, now, trusted_markets: Collection[str] | None = None) -> BetDecision
evaluate_all(priced, *, match, settings, betclic, now, trusted_markets=None) -> list[BetDecision]
ev_check_enabled(min_ev: float) -> bool ; EV_CHECK_DISABLED_AT = -1.0 ; ReasonCode.UNTRUSTED_CALIBRATION
```

### API / CLI

* `GET /api/control/state`, `POST /api/control/pause|resume` responses and `GET /api/health` `.trading`: new fields `pause_reasons`, `circuit_open`, `circuit_codes`, `autonomy`, `telegram` (schemas `PauseReasonOut`, `AutonomyStatusOut`, `TelegramStatusOut`; see docs/API.md). `trading_state(session, settings=None)`.
* CLI: `cs2bot telegram test [--message TEXT]`, `cs2bot control status` (extended), `cs2bot doctor` (new checks).

## Known limitations

* **Whole suite:** the last full run (`pytest tests -q -n 2`) had exactly one failure, `tests/ingest/pandascore/test_pandascore_sync.py::test_missing_token_is_reported_not_raised`, caused by deep_data's in-progress "PandaScore optional" change in `ingest/pandascore/sync.py` (not in my paths; deep_data updates that test). deep_data also has unformatted / long lines in its in-progress files (`backtest/deep_eval.py`, `features/deep.py`, `features/rows.py`, `tests/odds/test_deep_odds_movement.py`, `tests/ingest/hltv/test_deep_hltv_proxy.py`); `ruff` on my paths is clean.
* **Trusted calibration flag - TODO for the final review:** deep_data's per-market "trusted" verdict (`cs2bot.models.confidence` / `BacktestReport.market_trust`) had no report yet. `engine.decision` enforces it (`require_trusted_calibration`, default off) and `services.betting.trusted_markets(bundle)` reads a bundle attribute `market_trust` (`{market: {"trusted": bool}}` or `{market: bool}`) when it exists; wire the final attribute there once docs/dev/reports/deep_data.md describes it.
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
