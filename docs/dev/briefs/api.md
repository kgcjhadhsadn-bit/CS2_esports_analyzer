# P10a - Local dashboard API (cs2bot.api)

> Work-package brief: the requirements this package was built against. Keep it in sync when scope changes.

**Owned paths:** src/cs2bot/api/, docs/API.md, tests/api/

**Tests:** tests/api/

## Requirements

Goal: a FastAPI backend for the local dashboard on the owner's PC (http://127.0.0.1:8000). Read-mostly views over the DB plus safety controls. Other packages (ingestion, models, engine, executor) are being built in parallel: depend only on the foundation (DB models, domain types, db.state, utils) - never import those packages.

1. create_app(settings: Settings | None = None, session_factory: sessionmaker | None = None, *, login_launcher: Callable[[], None] | None = None, executor_health_provider: Callable[[], ExecutorHealth | None] | None = None) -> FastAPI. Include run(settings) using uvicorn (the integration layer starts it).
2. Security for a localhost app: bind 127.0.0.1 by default (settings.api.host). Reject requests whose Host header isn't an allowed local host (127.0.0.1, localhost, [::1], with or without the configured port; and the configured host when non-loopback) to block DNS-rebinding attacks. CSRF: every state-changing request (POST/PUT/PATCH/DELETE) must carry the header "X-Requested-With: cs2bot" (cross-site pages can't add it without a CORS preflight, and CORS allows only settings.api.cors_origins). Auth: if settings.api.password is set, session-cookie login (POST /api/auth/login, POST /api/auth/logout, GET /api/auth/me; itsdangerous-signed cookie with settings.api.session_secret, HttpOnly, SameSite=Strict, max age settings.api.session_max_age_s; constant-time comparison; simple login rate limiting); if no password is set (config validation only allows that on loopback) auth is disabled and /api/auth/me says so. Never log passwords/cookies.
3. Endpoints (JSON; pydantic response models; datetimes ISO-8601 UTC; money as strings with 2 decimals; odds as strings; probabilities as floats; limit/offset pagination with total):
   - GET /api/health: DB ok, per-job last run/status/age (job_runs), data freshness (last odds snapshot, last sync jobs), executor health (system_state "executor_health" or the provider), trading-halted state (kill switch/pause + reason), alerts list (e.g. "Betclic login required", "odds collector stale", "loss limit reached", "dry-run mode is ON").
   - GET /api/matches (filters: status, from, to, in_scope, linked; sorted by scheduled_at) with teams, tournament, tier, format, data completeness, linked Betclic event + link status, count of accepted decisions, best EV.
   - GET /api/matches/{id}: lineups, vetoes, maps (scores/sides/picks), context flags, latest analysis payload (match_analyses.payload = MatchPrediction JSON) + rationale, and a markets table joining latest predictions (is_latest) with bookmaker selections/odds (spec, selection, model p_win/p_push/p_void/sigma, odds, captured_at, EV after tax computed with settings.effective_tax_rate via a small local helper - don't import cs2bot.engine - and the latest decision accepted/reasons).
   - GET /api/matches/{id}/odds-history?selection_id=
   - GET /api/bets (filters: status, dry_run, from, to) and GET /api/bets/{id}; GET /api/bets/{id}/screenshot serves the image only from inside settings.executor.screenshot_dir (block path traversal).
   - GET /api/bankroll: current balance (ledger), open exposure, today's/this week's P&L vs the loss limits (effective bankroll settings incl. overrides via db.state), equity curve series (ledger balance over time), ROI, yield, hit rate, average CLV, max drawdown, P&L by day/week/month.
   - GET /api/performance/calibration (reliability bins of settled bets' p_win from bet_decisions vs outcome, and of predictions for finished matches' match/map winner markets) and GET /api/performance/markets (per market type: bets, hit rate, ROI, CLV).
   - GET /api/decisions (filters: match_id, accepted, limit).
   - Controls (audited via cs2bot.db.state; CSRF header required): GET/POST /api/control/kill-switch ({on: bool, reason}), POST /api/control/pause, POST /api/control/resume, GET/PUT /api/control/config (decision + bankroll overrides via db.state.set_config_overrides; 422 on invalid values; the response returns the effective settings and the defaults), POST /api/control/betclic-login (calls login_launcher in a background thread; 501 if not configured).
   - Review queue: GET /api/review/links (PENDING_REVIEW bookmaker events with the best candidate matches by time window and team similarity using cs2bot.utils.names) and POST /api/review/links/{event_id} ({action: confirm|reject|relink, match_id?, teams_swapped?}) setting link_status CONFIRMED/REJECTED (manual decisions are never overridden by the collector).
   - GET /api/jobs (recent job runs), GET /api/teams/{id} (recent results, maps played, players).
4. Static dashboard: if settings.api.serve_dashboard and settings.api.dashboard_dist_dir (resolve with cs2bot.utils.paths.resolve_path) exists, serve it at / with SPA fallback (index.html for non-/api paths); otherwise / returns a short JSON hint.
5. docs/API.md: every endpoint with request params and a JSON example response (the dashboard engineer works from it next).
6. Tests (TestClient, SQLite via the conftest session_factory/engine fixtures; populate with cs2bot.testing.synthetic.populate_db plus handcrafted bookmaker events/markets/selections/snapshots, predictions, analyses, decisions, bets and ledger rows): every endpoint's happy path + filters + 404s; auth on/off; Host header rejection; the CSRF header requirement on mutations; kill switch/pause/resume persist and create audit rows; config override validation (422) and effective settings; review confirm/reject/relink; screenshot path traversal blocked; SPA fallback with a temp dist dir.

## Addendum (after the first build wave)

- `APISettings` now has `allowed_hosts`, `login_max_failures`, `login_global_max_failures`, `login_window_s`: read them (ApiOptions may keep overriding for tests).
- New indexes exist (`ix_job_runs_name_started`, `ix_bet_decisions_outcome`). `BetDecision.notes` exists in the domain; `bet_decisions.reasons` stores rejection reasons.
- The odds package exposes `remap_event` (docs/dev/reports/odds.md); keep the `event_remapper` injection and the `extra["remap_required"]` fallback consistent with it.
- The scheduler exposes `Scheduler.status()` (src/cs2bot/flows/scheduler.py); accept an optional `scheduler_status_provider: Callable[[], list[dict]]` in `create_app` and show it under `/api/health` (next due time, failures per job).
