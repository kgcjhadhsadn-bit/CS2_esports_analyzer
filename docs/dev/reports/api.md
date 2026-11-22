# api: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/api -q` - 185 passed, 0 failed (lint/type clean: True)

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
    event_remapper: Callable[[Session, BookmakerEvent], object] | None = None,    # e.g. lambda s, ev: cs2bot.odds.remap_event(s, ev, mapper); called after a link review changed match/orientation
    options: ApiOptions | None = None,
) -> fastapi.FastAPI

def make_server(settings=None, session_factory=None, *, login_launcher=None, executor_health_provider=None,
                event_remapper=None, options=None, log_level: str | None = None) -> uvicorn.Server
    # bound to settings.api.host:port, proxy_headers=False, server_header=False.
    # server.run() works from a non-main thread; set server.should_exit = True to stop it.

def run(settings=None, session_factory=None, *, login_launcher=None, executor_health_provider=None,
        event_remapper=None, options=None) -> None   # blocking: make_server(...).run()

@dataclass(frozen=True)
class ApiOptions:
    login_max_failures: int = 5            # per client inside the window -> 429
    login_global_max_failures: int = 20
    login_window_s: float = 300.0
    cookie_name: str = "cs2bot_session"
    extra_allowed_hosts: tuple[str, ...] = ()   # extra "host" / "host:port" for the Host header (e.g. a LAN name when binding 0.0.0.0)
    default_page_size: int = 50
    max_page_size: int = 500
    review_window_hours: float = 24.0
    review_max_candidates: int = 5
    stuck_job_after_s: float = 7200.0
    executor_health_stale_s: float = 3600.0
    failing_job_threshold: int = 3
```

## HTTP endpoints (full contract with params + example JSON in docs/API.md)
- Auth and liveness: `GET /api/auth/me`, `POST /api/auth/login` (body `{username, password}`), `POST /api/auth/logout`, `GET /api/ping` (always public).
- `GET /api/health`
- Matches: `GET /api/matches` (status, from, to, in_scope, linked, team_id, order, limit, offset), `GET /api/matches/{id}`, `GET /api/matches/{id}/odds-history` (selection_id, since, max_points).
- Bets: `GET /api/bets` (status, dry_run, from, to, match_id, limit, offset), `GET /api/bets/{id}`, `GET /api/bets/{id}/screenshot`.
- Bankroll: `GET /api/bankroll` (dry_run, max_points).
- Performance: `GET /api/performance/calibration` (bins, mode=all|real|dry_run, from, to), `GET /api/performance/markets` (mode=real|dry_run|all, from, to).
- Decisions: `GET /api/decisions` (match_id, accepted, from, to, limit, offset).
- Controls: `GET /api/control/state`; `GET` and `POST /api/control/kill-switch` (body `{on, reason}`); `POST /api/control/pause` and `POST /api/control/resume` (optional body `{reason}`); `GET` and `PUT /api/control/config` (body `{decision?, bankroll?}`); `GET` and `POST /api/control/betclic-login` (202 started, 501 not configured, 409 already open).
- Review queue: `GET /api/review/links` (status, window_hours, max_candidates, limit, offset), `POST /api/review/links/{event_id}` (body `{action: confirm|reject|relink, match_id?, teams_swapped?}`).
- `GET /api/jobs` (name, status, limit, offset).
- `GET /api/teams/{id}` (limit, window_days).
- Docs: `GET /api/docs` and `/api/openapi.json`. `GET /` and every non-/api path serve the SPA from `dashboard/dist`, or a JSON hint if it isn't built.
- Every POST/PUT/PATCH/DELETE must send `X-Requested-With: cs2bot`, otherwise 403. A Host header that isn't allowed gets 400.

## Contracts other packages rely on
- **Executor health**: `system_state["executor_health"]` must hold `ExecutorHealth.model_dump(mode="json")`. Alternatively pass `executor_health_provider`, which takes precedence when it returns a value.
- **Manual link decisions**: the API sets `link_status` to CONFIRMED (`link_confidence = 1.0`) or REJECTED (`match_id = None`). The collector must never override these (P4 already honours this).
- When a review changes `match_id` or `teams_swapped`, the API clears the event's market/selection mappings (`is_mapped = False`, `selection_key`, `spec`, `spec_key` and `market_type` set to NULL) and sets `extra["remap_required"] = True`. It also writes `extra["manual_review"] = {action, by, at, previous_match_id, previous_status, previous_teams_swapped}`.
- **Config overrides** are stored through `db.state.set_config_overrides` as JSON-mode values (Decimals as strings) and only fields that differ from the defaults. PUT semantics: an omitted section is kept, `{}` clears that section, given fields replace that section.
- **Audit actor** is `"dashboard"`, or `"dashboard:<username>"` when auth is on. Audit actions: `kill_switch_on`, `kill_switch_off`, `pause`, `resume`, `config_overrides` (written by db.state), plus `link_review`, `betclic_login_requested`, `auth_login`, `auth_logout`.
- **Job names** (owned by the scheduler) are classified by keyword to find the expected interval in `SchedulerSettings`: odds, predict, settle, bet, result, news, demo, ranking, train/retrain, backup, reconcil, upcoming/fixture/schedule. Example: `odds_collect`, `ingest_upcoming`, `predict_upcoming`, `settle_bets`. A job is stale when its last success is older than 2 × interval + 10 min.

## Known limitations

- The EV shown by the API covers the 12% stake tax (or 0 during a promo) and refunds pushes/voids, but not the 10% tax on single wins above 2280 PLN, because that depends on the stake. The decision engine handles that tax.
- Bet calibration uses `bet_decisions.p_win` as written by the engine. For map-scoped markets that value may not be conditional on the map being played.
- Prediction calibration uses the `is_latest` match/map-winner rows of finished matches, one selection per market (team1's row when present). This assumes the predictor stops updating a match once it is live.
- In the matches list, `accepted_decisions` counts distinct outcomes (spec_key + selection_key) with at least one accepted decision, not decision rows. `best_ev` uses available pre-match prices from auto/confirmed links even when those prices are stale; the per-row `odds_stale` flag is in the match detail.
- Login rate limiting and logout revocation are kept in process memory: they reset on restart and assume a single API process. If `session_secret` is unset, a random secret is used and sessions end on restart (a warning is logged).
- Health classifies jobs by keywords in the scheduler's job names. Jobs whose names match no keyword get no staleness check.
- `executor_health_provider` is called synchronously on every `/api/health` request, so it must return a cached value rather than run a live browser check.
- Nothing in the executor package currently writes `system_state["executor_health"]`. Unless the integration layer persists `health_check()` there or wires a provider, health will report `executor_health_unknown`.
- After a review changes an event's match or team order, its mappings stay cleared (so it can't be bet) until re-mapped: immediately if `event_remapper` is wired, otherwise on the collector's next pass (about 3 minutes).
- The bankroll and performance endpoints load the full bets and ledger tables into memory on each request. That is fine at a personal bot's volumes; very large histories (100k+ bets) would need SQL-side aggregation.
- When the API is bound to 0.0.0.0, the allowed Host names are the loopback names, this PC's hostname and resolved addresses, and `ApiOptions.extra_allowed_hosts`.
- The Swagger UI at /api/docs loads its JavaScript from a CDN, so offline only the raw schema at /api/openapi.json works.
- The PostgreSQL test (tests/api/test_api_postgres.py) was skipped on this machine because CS2BOT_TEST_DATABASE_URL isn't set; all other tests run on SQLite.

## Foundation change requests

- config.APISettings: add `allowed_hosts: list[str] = []` (extra Host-header names, needed when binding 0.0.0.0) and login rate-limit settings (`login_max_failures`, `login_window_s`). These currently live in `cs2bot.api.ApiOptions` because I could not change config.py.
- docs/ARCHITECTURE.md P10: record the actual signature `create_app(settings=None, session_factory=None, *, login_launcher=None, executor_health_provider=None, event_remapper=None, options=None)` plus `make_server(...)` and `run(...)`. Also state the review contract: the API clears mappings and sets `extra.remap_required` when a manual review changes match or orientation; the collector re-maps on its next pass or via the injected `remap_event`.
- docs/ARCHITECTURE.md P9/P12: state who persists `ExecutorHealth.model_dump(mode="json")` into `system_state["executor_health"]` (or that the integration layer passes a cached `executor_health_provider`), so the dashboard health page and the 'Betclic login required' alert work.
- Optional performance indexes in db/models.py, for large PostgreSQL histories: `Index("ix_job_runs_name_started", "job_name", "started_at")` for the per-job health query, and `Index("ix_bet_decisions_outcome", "match_id", "spec_key", "selection_key")` for the latest-decision-per-outcome lookup.

## New dependency requests

- (none)
