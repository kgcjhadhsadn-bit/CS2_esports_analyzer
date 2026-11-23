# api: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/api -q` - 220 passed, 0 failed, 2 skipped (PostgreSQL; both pass with `CS2BOT_TEST_DATABASE_URL` set) after review round 1 (lint/type clean: True)

## Summary

P10a is finished: the local dashboard API (`cs2bot.api`) is built and every definition-of-done check passes. `tests/api` gives 185 passed, 0 failed, 1 skipped (the PostgreSQL test, which needs `CS2BOT_TEST_DATABASE_URL`). ruff check, ruff format --check and mypy are clean on my paths, and `tests/foundation` still passes (37). I changed nothing outside `src/cs2bot/api/`, `tests/api/` and `docs/API.md`.

What the API has:
- `create_app`, `make_server` and `run` (uvicorn). `create_app` also takes optional `event_remapper` and `options` arguments.
- Requests go through three checks in order: a Host-header allow-list (blocks DNS rebinding), CORS limited to `cors_origins` with `*` ignored, and a CSRF check requiring `X-Requested-With: cs2bot` (plus an Origin check) on every POST/PUT/PATCH/DELETE. Responses carry anti-framing, no-sniff and no-store headers.
- Optional cookie login, only when `api.password` is set: itsdangerous-signed, HttpOnly, SameSite=Strict, constant-time credential check, in-memory login rate limit (429), logout revokes the session, and changing the username or password ends all sessions.
- All requested endpoints are in place. Money is returned as 2-decimal strings, odds as strings, datetimes as ISO UTC, and lists are paginated with a total.
- EV after tax is computed by a small local helper; the API imports nothing from engine, executor or odds.
- Bets and bankroll can show either real money (from the ledger) or dry-run bets, with loss limits computed from the effective settings including dashboard overrides and Warsaw day/week boundaries.
- Config overrides get range and typo checks on top of type checks (422), and are stored per section in JSON-safe form.
- All controls are audited through `db.state`.
- The Betclic login launcher runs in a background thread: 501 if not configured, 409 if a window is already open, and the last result or error is reported.
- In the review queue, candidates are scored 80% on team names (incl. aliases, best orientation) and 20% on start time. If an action changes the linked match or team order, the event's market mappings are cleared so no selection keyed for the old order can be priced or bet. The odds collector already re-maps every market on each pass; the optional `event_remapper` re-maps immediately.
- The health endpoint never returns 5xx on DB problems: it reports `db.ok = false` and separates "unreachable" from "schema missing" (`db_error`). It covers job staleness/failing/stuck, data freshness, executor health (provider first, else `system_state`), trading state and coded alerts.
- Elsewhere, connection errors give 503 JSON and other DB errors 500 JSON. 422 responses use FastAPI's shape but never echo submitted values, so a bad login body can't reflect the password.
- The built dashboard is served with SPA fallback and traversal protection; without it, `/` returns a short JSON hint.
- `docs/API.md` documents every endpoint with parameters and example responses generated from the real test dataset.

The tests also caught and I fixed: player nicknames being re-capitalised in outcome labels, and `predict_upcoming` being classified as a sync job in health.

## Public API

## `cs2bot.api` (re-exported from `cs2bot/api/__init__.py`)

```python
from cs2bot.api import ApiOptions, create_app, make_server, run

def create_app(
    settings: Settings | None = None,                      # default: cs2bot.config.get_settings()
    session_factory: sessionmaker[Session] | None = None,  # default: make_session_factory(make_engine(settings.database.url))
    *,
    login_launcher: Callable[[], object] | None = None,    # e.g. executor.open_login_window (may block; run in a daemon thread, one at a time; a bool return is shown as last_result)
    executor_health_provider: Callable[[], ExecutorHealth | None] | None = None,  # must be FAST (cached health_check()); None -> falls back to system_state["executor_health"]
    event_remapper: Callable[[Session, BookmakerEvent], object] | None = None,    # e.g. lambda s, ev: cs2bot.odds.remap_event(s, ev, mapper, settings=settings); called after a link review changed match/orientation
    scheduler_status_provider: Callable[[], Sequence[Mapping[str, object]]] | None = None,  # cs2bot.flows.scheduler.Scheduler.status; shown under /api/health
    options: ApiOptions | None = None,
) -> fastapi.FastAPI

def make_server(settings=None, session_factory=None, *, login_launcher=None, executor_health_provider=None,
                event_remapper=None, scheduler_status_provider=None, options=None,
                log_level: str | None = None) -> uvicorn.Server
    # bound to settings.api.host:port, proxy_headers=False, server_header=False.
    # server.run() works from a non-main thread; set server.should_exit = True to stop it.
    # log_level / settings.log_level are mapped to uvicorn's names (WARN -> warning, unknown -> info).

def run(settings=None, session_factory=None, *, login_launcher=None, executor_health_provider=None,
        event_remapper=None, scheduler_status_provider=None, options=None) -> None   # blocking: make_server(...).run()

@dataclass(frozen=True)
class ApiOptions:
    # overrides of settings.api (None / empty = use the settings; mainly for tests)
    login_max_failures: int | None = None          # settings.api.login_max_failures (5): per client inside the window -> 429
    login_global_max_failures: int | None = None   # settings.api.login_global_max_failures (20)
    login_window_s: float | None = None            # settings.api.login_window_s (300)
    extra_allowed_hosts: tuple[str, ...] = ()      # added to settings.api.allowed_hosts ("host" / "host:port" for the Host header)
    # tunables without a setting
    cookie_name: str = "cs2bot_session"
    default_page_size: int = 50
    max_page_size: int = 500
    review_window_hours: float = 24.0
    review_max_candidates: int = 5
    stuck_job_after_s: float = 7200.0
    executor_health_stale_s: float = 3600.0
    failing_job_threshold: int = 3
    scheduler_overdue_grace_s: float = 900.0       # a scheduler job due longer ago than this while its lane is idle -> "scheduler_job_overdue"
```

Settings read from `settings.api`: `host`, `port`, `serve_dashboard`, `dashboard_dist_dir`, `username`, `password`, `session_secret`, `session_max_age_s`, `cors_origins`, `allowed_hosts`, `login_max_failures`, `login_global_max_failures`, `login_window_s`. Elsewhere: `executor.screenshot_dir` / `enabled` / `dry_run`, `betclic.tax_rate` / `tax_free_promo` / `void_refunds_stake_tax` / `odds_max_age_s`, `scheduler.*` intervals, `timezone`, `decision`, `bankroll`.

## HTTP endpoints (full contract with params + example JSON in docs/API.md)
- Auth and liveness: `GET /api/auth/me`, `POST /api/auth/login` (body `{username, password}`), `POST /api/auth/logout`, `GET /api/ping` (always public).
- `GET /api/health` (incl. `scheduler` = `Scheduler.status()` and `scheduled` / `next_due_at` / `scheduler_failures` per job when `scheduler_status_provider` is wired)
- Matches: `GET /api/matches` (status, from, to, in_scope, linked, team_id, order, limit, offset), `GET /api/matches/{id}`, `GET /api/matches/{id}/odds-history` (selection_id, since, max_points).
- Bets: `GET /api/bets` (status, dry_run, from, to, match_id, limit, offset), `GET /api/bets/{id}`, `GET /api/bets/{id}/screenshot`. Every bet has `result` (`won` / `lost` / `void` / null) and `is_open` next to the stored `status`; the `status` filter matches either.
- Bankroll: `GET /api/bankroll` (dry_run, max_points).
- Performance: `GET /api/performance/calibration` (bins, mode=all|real|dry_run, from, to), `GET /api/performance/markets` (mode=real|dry_run|all, from, to).
- Decisions: `GET /api/decisions` (match_id, accepted, from, to, limit, offset).
- Controls: `GET /api/control/state`; `GET` and `POST /api/control/kill-switch` (body `{on, reason}`); `POST /api/control/pause` and `POST /api/control/resume` (optional body `{reason}`); `GET` and `PUT /api/control/config` (body `{decision?, bankroll?}`); `GET` and `POST /api/control/betclic-login` (202 started, 501 not configured, 409 already open).
- Review queue: `GET /api/review/links` (status, window_hours, max_candidates, limit, offset), `POST /api/review/links/{event_id}` (body `{action: confirm|reject|relink, match_id?, teams_swapped?}`).
- `GET /api/jobs` (name, status, limit, offset).
- `GET /api/teams/{id}` (limit, window_days).
- Docs: `GET /api/docs` and `/api/openapi.json`. `GET /` and every non-/api path serve the SPA from `dashboard/dist`, or a JSON hint if it isn't built.
- Every POST/PUT/PATCH/DELETE must send `X-Requested-With: cs2bot`, otherwise 403. A Host header that isn't allowed gets 400. Ids outside the 32-bit range are a 422.

## Contracts other packages rely on
- **Executor health**: `system_state["executor_health"]` must hold `ExecutorHealth.model_dump(mode="json")`. Alternatively pass `executor_health_provider`, which takes precedence when it returns a value.
- **Manual link decisions**: the API sets `link_status` to CONFIRMED (`link_confidence = 1.0`) or REJECTED (`match_id = None`). The collector must never override these (P4 already honours this).
- When a review changes `match_id` or `teams_swapped`, the API clears the event's market/selection mappings (`is_mapped = False`, `selection_key`, `spec`, `spec_key` and `market_type` set to NULL) and sets `extra["remap_required"] = True`. It also writes `extra["manual_review"] = {action, by, at, previous_match_id, previous_status, previous_teams_swapped}`.
- **Config overrides** are stored through `db.state.set_config_overrides` as JSON-mode values (Decimals as strings) and only fields that differ from the defaults. PUT semantics: an omitted section is kept, `{}` clears that section, given fields replace that section.
- **Audit actor** is `"dashboard"`, or `"dashboard:<username>"` when auth is on. Audit actions: `kill_switch_on`, `kill_switch_off`, `pause`, `resume`, `config_overrides` (written by db.state), plus `link_review`, `betclic_login_requested`, `auth_login`, `auth_logout`.
- **Job names** (owned by the scheduler) are classified by keyword to find the expected interval in `SchedulerSettings`: odds, predict, settle, bet, result, news, demo, ranking, train/retrain, backup, reconcil, upcoming/fixture/schedule. Example: `odds_collect`, `ingest_upcoming`, `predict_upcoming`, `settle_bets`. A job is stale when its last success is older than 2 × interval + 10 min.

- **Paper bets** (contract with `engine.ledger.settle_bet`): a settled paper bet keeps `bets.status = "dry_run"`; its result is read from `bets.extra["settled_outcome"]` (`won` / `lost` / `void` / `push`), falling back to `settled_at` + the sign of `pnl`. The API exposes it as `result` and uses it in every statistic. A row counts as paper when `dry_run` is true or its status is `dry_run` (the engine's `is_dry_run`).
- **Bankroll numbers** follow `engine.bankroll.compute_state` / `loss_limit_status` (balance = last written ledger row, `bankroll_reference` = balance + debited open stakes, period-start bankroll = reference - realized P&L of the period, limit rounded down to the grosz). A test compares the endpoint with the engine on the same database.
- **Scheduler status**: the provider returns `Scheduler.status()` dicts (`name`, `lane`, `enabled`, `last_run`, `next_due` as ISO strings or datetimes, `failures`). It is called on every `/api/health` request and must not block.
- **Predictions** are expected to be unconditional (`OutcomeProbability`): `p_win + p_push + p_void + p_lose = 1`. The match page shows `p_win_settled`; calibration uses it.

## Known limitations

- The EV shown by the API covers the 12% stake tax (or 0 during a promo) and refunds pushes/voids, but not the 10% tax on single wins above 2280 PLN, because that depends on the stake. The decision engine handles that tax.
- Bet calibration uses `bet_decisions.p_win` divided by `1 - p_push - p_void` of the prediction the decision links to (`prediction_id`). If the integration layer does not set `prediction_id`, the unconditional `p_win` is used as is, which under-states the probability of map-scoped bets that may be voided.
- Prediction calibration uses the `is_latest` match/map-winner rows of finished matches written **before** `matches.scheduled_at`, one selection per market (team1's row when present). If the predictor keeps writing predictions after the start, that match simply drops out of the calibration (no look-ahead, but also no sample): the prediction job should stop once a match is no longer scheduled.
- In the matches list, `accepted_decisions` counts distinct outcomes (spec_key + selection_key) with at least one accepted decision, not decision rows. `best_ev` uses available pre-match prices from auto/confirmed links even when those prices are stale; the per-row `odds_stale` flag is in the match detail.
- Login rate limiting and logout revocation are kept in process memory: they reset on restart and assume a single API process. If `session_secret` is unset, a random secret is used and sessions end on restart (a warning is logged).
- Health classifies jobs by keywords in the scheduler's job names. Jobs whose names match no keyword get no staleness check.
- `executor_health_provider` and `scheduler_status_provider` are called synchronously on every `/api/health` request, so they must return cached / in-memory values rather than run a live browser check.
- Nothing in the executor package currently writes `system_state["executor_health"]`. Unless the integration layer persists `health_check()` there or wires a provider, health will report `executor_health_unknown`.
- After a review changes an event's match or team order, its mappings stay cleared (so it can't be bet) until re-mapped: immediately if `event_remapper` is wired, otherwise on the collector's next pass (about 3 minutes).
- The bankroll and performance endpoints load the full bets and ledger tables into memory on each request. That is fine at a personal bot's volumes; very large histories (100k+ bets) would need SQL-side aggregation.
- When the API is bound to 0.0.0.0, the allowed Host names are the loopback names, this PC's hostname and resolved addresses, and `ApiOptions.extra_allowed_hosts`.
- The Swagger UI at /api/docs loads its JavaScript from a CDN, so offline only the raw schema at /api/openapi.json works.
- The PostgreSQL tests (tests/api/test_api_postgres.py) are skipped unless CS2BOT_TEST_DATABASE_URL is set; in review round 1 they were run once against a local PostgreSQL 16 and passed. All other tests run on SQLite.
- `GET /api/bets` loads six small columns of every bet matching the non-status filters to apply the status filter and build the summary in Python (the result of a paper bet lives in a JSON field). Fine for a personal bot; very large histories would need a real `result` column.
- In paper mode the bankroll page shows loss limits for information only: the engine applies limits to real money, and sizes paper stakes from the real bankroll state.

## Foundation change requests

- (applied) config.APISettings got `allowed_hosts` and the login rate-limit settings; the API reads them.
- docs/ARCHITECTURE.md P10: record the actual signature `create_app(settings=None, session_factory=None, *, login_launcher=None, executor_health_provider=None, event_remapper=None, scheduler_status_provider=None, options=None)` plus `make_server(...)` and `run(...)`. Also state the review contract: the API clears mappings and sets `extra.remap_required` when a manual review changes match or orientation; the collector re-maps on its next pass or via the injected `remap_event`.
- docs/ARCHITECTURE.md P9/P12: state who persists `ExecutorHealth.model_dump(mode="json")` into `system_state["executor_health"]` (or that the integration layer passes a cached `executor_health_provider`), so the dashboard health page and the 'Betclic login required' alert work.
- (applied) the `ix_job_runs_name_started` and `ix_bet_decisions_outcome` indexes exist.
- db/models.py `Bet`: consider a nullable `result` column (won / lost / void) written by `engine.ledger.settle_bet` for real and paper bets alike. Today the result of a paper bet only exists in `bets.extra["settled_outcome"]`, so it cannot be filtered or aggregated in SQL (the API does it in Python).
- docs/ARCHITECTURE.md / integration: the prediction job must set `bet_decisions.prediction_id` and stop writing `is_latest` predictions for a match once it has started (see the calibration limitations above).

## New dependency requests

- (none)

## Review round 1

Independent review of the whole package against the brief and its addendum. An earlier review attempt had been cut off after plumbing `scheduler_status_provider` into `create_app` / `ApiContext` and making the `ApiOptions` login fields optional; that work was kept, verified and finished. Checks after the review: `tests/api` 220 passed, 2 skipped (PostgreSQL; both pass when `CS2BOT_TEST_DATABASE_URL` points at a server - run once against a local PostgreSQL 16); ruff check / format and mypy clean on `src/cs2bot/api` and `tests/api`; `tests/foundation` 37 passed.

### What was verified

- Every requirement of the brief, endpoint by endpoint, and every addendum item.
- The math: EV after tax, fair / break-even odds, loss limits, drawdown, yield / ROI / hit rate, Wilson intervals, Brier / log-loss / ECE, team1/team2 orientation of labels, lines and link reviews, Warsaw day / week boundaries.
- Both sides of each cross-package contract, by running the other package's real code in tests (the API source still imports none of them): `engine.ledger.apply_placement` / `settle_bet`, `engine.bankroll.compute_state` / `loss_limit_status`, `engine.ev.expected_value`, `flows.scheduler.Scheduler.status`, `odds.remap_event` with the shipped `config/betclic_markets.yaml`.
- Security: Host allow-list, CSRF header + Origin check, session cookies, rate limiting, path traversal for screenshots and the SPA, nothing secret in logs or 422 bodies. A new test walks the OpenAPI schema and asserts that every data / control operation answers 401 without a session.
- Robustness with malformed rows (matches without teams or start time, garbage spec keys, infinite floats, non-list JSON, negative balances, future-dated job runs) and hostile query / body values.

### Issues found and fixed

1. **Major - settled paper bets were invisible.** `engine.ledger.settle_bet` keeps `status = dry_run` on a paper bet and stores the result in `extra["settled_outcome"]`; the API only recognised `won` / `lost` / `void` statuses, so in dry-run mode (the default) every paper bet stayed "open" for ever: no paper P&L, hit rate, calibration or per-market performance, and the paper balance kept shrinking by the open stakes. Added `analytics.effective_status`, used by the bankroll, bets, performance and review code; `BetOut` / `BetBrief` gained `result` and `is_open`; the `status` filter of `GET /api/bets` matches the stored status or the result.
2. **Major - loss limits disagreed with the engine.** The period-start bankroll was rebuilt from the ledger balance at the period start, which is 0 on the day (and in the week) the ledger is created, so the first lost bet raised a false critical "loss limit reached" alert, and deposits made during a period were ignored. It is now `bankroll_reference - realized P&L of the period`, the limit is rounded down to the grosz and `reached` uses the engine's rule; `open_stakes` is all money at risk and the new `stake_allowance` is the engine's guard. A test compares the endpoint with `compute_state` / `loss_limit_status`.
3. **Major - wrong balance with back-dated ledger rows.** The balance was taken from the row with the latest timestamp; the engine defines it as the last *written* row (a stake of a reconciled bet is booked at the time it was really placed). The equity curve is now re-accumulated in time order and ends at the balance. Debited stakes are read from the ledger's STAKE rows instead of being guessed from the bet status; new `available_balance` (balance minus pending / unconfirmed stakes).
4. **Major - addendum item missing:** `scheduler_status_provider` was accepted but never shown. `/api/health` now has a `scheduler` section (lane, enabled, last run, next due, failures, running, overdue), per-job `scheduled` / `next_due_at` / `scheduler_failures`, and the alerts `scheduler_job_overdue`, `scheduler_job_disabled`, `scheduler_status_error`. Jobs the scheduler does not run are no longer flagged stale (odds staleness then falls back to the data itself). A leftover RUNNING row from a crashed process is not mistaken for a live run.
5. **Major - calibration used unconditional probabilities.** Samples are won / lost only, but `p_win` includes the chance of a refund (e.g. map 3 not played), so the model looked under-confident on map-scoped markets (a stored 0.20 for a map that is played 40 % of the time really means 0.50). Both calibration sets now use `p_win / (1 - p_push - p_void)`. Predictions written at or after the match start are ignored (look-ahead).
6. **Major (safety) - NaN accepted as a config override.** Python's JSON parser accepts `NaN`; every range check passed and the value was stored, which would have switched off the engine's `min_probability` / `min_ev` / `max_sigma` comparison (and made PostgreSQL reject the row). Non-finite numbers are now a 422.
7. **Minor - HTTP 500 for ids beyond 32 bit** in paths, query parameters, `offset` and the review body (driver overflow). Now a 422.
8. **Minor - displayed EV ignored `betclic.void_refunds_stake_tax = false`.** `api.ev` takes `TaxTerms` (stake tax + refund loss rate) and matches `engine.ev.expected_value` in both modes (tested against it).
9. **Minor - calibration bin edges:** `0.3 / 0.1` is 2.9999999999999996, so a probability exactly on an edge fell into the lower bin.
10. **Minor - "dry-run mode is ON" was a warning,** so the health status could never be `ok` in the default mode. It is an `info` alert now.
11. **Minor -** the login audit actor was not truncated to the 64-character column (the truncation existed only for other actions); the login rate limiter never forgot clients whose failures had expired; the loss-limit alert printed "limit -0.00".
12. **Tests / docs:** the calibration seed wrote its predictions after the matches had started (now one hour before); 35 new tests in `tests/api/test_api_review1.py`, a second PostgreSQL test covering every read endpoint; `docs/API.md` updated for every change above.

### API changes in this round (for the dashboard engineer)

- `GET /api/health`: new `scheduler`; `jobs[].scheduled` / `next_due_at` / `scheduler_failures`; three new alert codes; `dry_run_on` is `info`.
- Bets (`BetOut`, and `bet` in the market rows): new `result`, `is_open`.
- `GET /api/bankroll`: new `available_balance`, `daily.stake_allowance` / `weekly.stake_allowance`; `open_stakes` is now all open stakes; `limit_amount` rounds down.
- Market rows: new `p_win_settled`.
- Python: `scheduler_status_provider` on `create_app` / `make_server` / `run`; `ApiOptions.login_*` default to `None` (settings); `ApiOptions.scheduler_overdue_grace_s`; `queries.match_summaries` / `build_market_rows` take `TaxTerms` instead of a bare tax rate (internal).

### Remaining concerns

- No package writes `bet_decisions`, `predictions` or `system_state["executor_health"]` yet (integration is not built). The API's assumptions about them are listed under "Contracts other packages rely on"; the integration layer must set `bet_decisions.prediction_id`, store unconditional probabilities, and either persist the executor health or pass a cached provider.
- `BetDecision.notes` (addendum) has no column: the API shows `reasons` and, in the bet detail, the whole `settings_snapshot`. If the integration layer stores notes somewhere, the decision list should surface them.
- Job classification for staleness still relies on keywords in job names when no scheduler status is available; with `scheduler_status_provider` wired the overdue check covers every job regardless of its name.
- Selectors, Betclic payloads and the live site remain unverified (not this package), so the end-to-end path dashboard -> login window -> executor has only been exercised with fakes.

