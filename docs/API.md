# Dashboard API (`cs2bot.api`)

Local FastAPI backend for the React dashboard. It runs on the owner's PC at
**http://127.0.0.1:8000** (`settings.api.host` / `settings.api.port`), serves the built dashboard
from `dashboard/dist` at `/`, and exposes JSON endpoints under `/api`. Interactive OpenAPI docs:
`/api/docs` (schema: `/api/openapi.json`).

Contents: [Conventions](#conventions) · [Security](#security) · [Auth](#auth) · [Health](#health) ·
[Matches](#matches) · [Bets](#bets) · [Bankroll](#bankroll) · [Performance](#performance) ·
[Decisions](#decisions) · [Controls](#controls) · [Link review](#link-review-queue) ·
[Jobs](#jobs) · [Teams](#teams) · [Static dashboard](#static-dashboard) · [Python API](#python-api)

---

## Conventions

| Topic | Rule |
|---|---|
| Datetimes | ISO-8601 **UTC** with `Z`, e.g. `"2026-11-20T12:00:00Z"`. Query datetimes without an offset are read as UTC; a bare date (`2026-11-21`) means midnight UTC. |
| Money | string with exactly two decimals: `"12.50"`, `"-30.00"` |
| Odds | string, 2-3 decimals: `"1.85"`, `"1.875"`, `"2.00"` |
| Probabilities, EV, CLV, rates | JSON numbers (floats) |
| Orientation | `team1` / `team2` always mean the match's `team1` / `team2` (never Betclic's display order) |
| Pagination | `limit` (default 50, capped at 500) and `offset` (default 0); list responses are `{"total", "limit", "offset", "items": [...]}` |
| Multi-value filters | repeat the parameter (`?status=won&status=lost`) **or** comma-separate (`?status=won,lost`) |
| `from` / `to` | inclusive bounds |
| Errors | `{"detail": "..."}`; validation errors (422) use FastAPI's shape `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` |
| Status codes | 400 bad Host header · 401 not logged in · 403 CSRF check failed · 404 not found · 409 conflict · 422 invalid input · 429 too many failed logins · 501 not configured · 503 database unavailable |
| Ids | 32-bit integers; an id outside that range in the path, query or body is a 422 (never a 500) |

**EV** shown by the API is per 1 unit of gross stake after the Polish stake tax
(`settings.effective_tax_rate`, 0.12, or 0 during a "bez podatku" promo), with pushes / voids
refunded: `EV = p_win * (1 - tax) * odds + (p_push + p_void) * (1 - c) - 1`, where `c` is 0 by
default and the stake tax rate when `betclic.void_refunds_stake_tax` is off (a refund then costs
the tax). This is the engine's formula for a small stake; the 10% tax on wins above 2280 PLN
depends on the stake and is not included in the displayed EV.

**Probabilities are unconditional.** `p_win + p_push + p_void + p_lose = 1`; a market on a map that
may not be played (e.g. the winner of map 3) carries that chance in `p_void`. `p_win_settled =
p_win / (1 - p_push - p_void)` is the probability of winning given the bet is not refunded - the
number the decision engine compares with `min_probability`, and the one used for calibration.

**Paper (dry-run) bets** keep `status: "dry_run"` for their whole life: the settlement job stores
the paper result on the row instead of changing the status. Read **`result`** (`"won"` / `"lost"` /
`"void"` / `null` = not settled) and `is_open` rather than `status` to show how a bet ended; every
statistic of the API (bankroll, P&L, hit rate, calibration, per-market performance) already does.

---

## Security

The API is meant for the local machine only. Every request passes, in order:

1. **Host allow-list (DNS rebinding).** The `Host` header must be `127.0.0.1`, `localhost` or
   `[::1]` (or the configured `api.host` when it is not loopback, or a name listed in
   `api.allowed_hosts`), either without a port, with
   `api.port`, or with the port of an entry in `api.cors_origins` (so a Vite dev proxy that keeps
   `Host: localhost:5173` works). Anything else gets **400** `{"detail": "invalid host header"}`.
2. **CORS** only for `api.cors_origins` (default `http://localhost:5173`), credentials allowed,
   headers `X-Requested-With` and `Content-Type`. A `*` entry is ignored.
3. **CSRF:** every `POST` / `PUT` / `PATCH` / `DELETE` must send **`X-Requested-With: cs2bot`**,
   otherwise **403**. If the request carries an `Origin` header it must be a CORS origin or the
   dashboard's own origin (`http://127.0.0.1:8000`, `http://localhost:8000`, `http://[::1]:8000`).
4. **Auth** (only when `api.password` is set): session cookie, see [Auth](#auth).

Responses carry `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`; `/api` responses also carry
`Cache-Control: no-store`.

Dashboard fetch helper (all requests):

```ts
fetch(`/api${path}`, {
  method,
  credentials: "include",
  headers: { "X-Requested-With": "cs2bot", ...(body ? { "Content-Type": "application/json" } : {}) },
  body: body ? JSON.stringify(body) : undefined,
});
```

For `npm run dev`, proxy `/api` to `http://127.0.0.1:8000` (Vite `server.proxy`); `changeOrigin`
may be `true` or `false`.

---

## Auth

Auth is **disabled** when `CS2BOT_API__PASSWORD` is empty (allowed only on a loopback host); every
endpoint is then open and `/api/auth/me` says `auth_enabled: false`. When a password is set, all
`/api/*` endpoints except `/api/ping` and `/api/auth/*` answer **401** without a valid session.

The session cookie `cs2bot_session` is an `itsdangerous`-signed token (`api.session_secret`; a
random per-process secret if unset), `HttpOnly`, `SameSite=Strict`, `Path=/`,
`Max-Age=api.session_max_age_s` (12 h). Changing the username or password invalidates all sessions.
`api.login_max_failures` (5) failed logins per client within `api.login_window_s` (300 s), or
`api.login_global_max_failures` (20) overall, -> **429** with `Retry-After`. The OpenAPI schema and
`/api/docs` are public too (they contain no data).

### `GET /api/auth/me`

```json
{"auth_enabled": true, "authenticated": false, "username": null}
```

### `POST /api/auth/login`

Body: `{"username": "admin", "password": "..."}`. 200 sets the cookie; 401 wrong credentials;
429 rate limited. With auth disabled it is a no-op returning `auth_enabled: false`.

```json
{"auth_enabled": true, "authenticated": true, "username": "admin"}
```

### `POST /api/auth/logout`

Clears the cookie and revokes the session.

```json
{"auth_enabled": true, "authenticated": false, "username": null}
```

### `GET /api/ping`

Unauthenticated liveness probe: `{"status": "ok"}`.

---

## Health

### `GET /api/health`

DB check, job runs (`job_runs`), scheduler status, data freshness, executor health, trading state
and alerts. It never fails with 5xx because of the database: `db.ok` becomes `false` instead.

* `status`: `ok` | `warning` | `critical` (worst alert level; `info` alerts keep `ok`).
* `executor.source`: `provider` (live callback given to `create_app`), `system_state`
  (`system_state["executor_health"]` written by the executor) or `none`.
* `jobs[]`: one entry per job name found in `job_runs`: last run, last success, consecutive
  failures, `kind` (classified from the name: `odds`, `predict`, `settle`, `bet`, `results`, `news`,
  `demos`, `rankings`, `retrain`, `backup`, `reconcile`, `upcoming`), the expected interval from
  `SchedulerSettings`, and `stale` (last success older than `2 x interval + 10 min`).
  With a scheduler (below) each entry also has `scheduled` (the scheduler runs it and it is
  enabled), `next_due_at` and `scheduler_failures`; a job that is **not** scheduled is never `stale`.
  Without a scheduler these three fields are `null`.
* `scheduler`: `null` when no scheduler runs in the API's process (e.g. `cs2bot api` on its own).
  Otherwise `{"ok", "jobs": [...]}` from `Scheduler.status()` - one entry per scheduler job, also
  those that never ran: `lane`, `enabled`, `last_run_at`, `next_due_at`, `due_in_s` (negative =
  past due), `failures` (consecutive failed runs, drives the back-off), `running` (a live run is
  recorded in `job_runs`) and `overdue` (enabled, due more than 15 min ago, and nothing is running
  in its lane - the scheduler thread is probably dead). `ok: false` = the status call failed.
* Alert codes: `db_unreachable`, `db_error`, `kill_switch_on`, `trading_paused`,
  `loss_limit_reached`, `betclic_login_required`, `executor_unhealthy`, `executor_selectors_failing`,
  `executor_health_stale`, `executor_health_unknown`, `executor_health_error`, `executor_disabled`,
  `dry_run_on`, `odds_collector_stale`, `odds_collector_never_ran`, `job_stale`, `job_failing`,
  `job_stuck`, `scheduler_job_overdue`, `scheduler_job_disabled`, `scheduler_status_error`,
  `link_review_pending`, `bankroll_unavailable`. Sorted critical -> warning -> info.
  `dry_run_on`, `executor_disabled`, `executor_health_unknown`, `scheduler_job_disabled` and
  `link_review_pending` are `info` (paper trading is the default mode and does not degrade `status`).

```json
{
  "status": "critical",
  "now": "2026-11-20T12:00:00Z",
  "version": "0.1.0",
  "db": {"ok": true, "latency_ms": 0.35, "error": null},
  "trading": {
    "halted": false,
    "reason": null,
    "kill_switch": {"on": false, "reason": null, "updated_at": null, "updated_by": null},
    "paused": {"on": false, "reason": null, "updated_at": null, "updated_by": null}
  },
  "dry_run": true,
  "executor": {
    "source": "system_state", "enabled": true, "dry_run": true, "ok": false, "logged_in": false,
    "balance": "968.44", "selectors_ok": {"stake_input": true, "confirm_button": false},
    "message": "session expired", "checked_at": "2026-11-20T11:55:00Z", "age_s": 300.0
  },
  "jobs": [
    {
      "name": "odds_collect", "kind": "odds", "status": "success",
      "last_started_at": "2026-11-20T11:58:00Z", "last_finished_at": "2026-11-20T11:59:00Z",
      "age_s": 120.0, "duration_s": 60.0, "items": 3, "error": null,
      "last_success_at": "2026-11-20T11:59:00Z", "last_success_age_s": 60.0,
      "consecutive_failures": 0, "expected_interval_s": 180.0, "stale": false,
      "scheduled": true, "next_due_at": "2026-11-20T12:01:00Z", "scheduler_failures": 0
    }
  ],
  "scheduler": {
    "ok": true,
    "jobs": [
      {
        "name": "odds_collect", "lane": "fast", "enabled": true,
        "last_run_at": "2026-11-20T11:58:00Z", "next_due_at": "2026-11-20T12:01:00Z",
        "due_in_s": 60.0, "failures": 0, "running": false, "overdue": false
      },
      {
        "name": "predict_upcoming", "lane": "fast", "enabled": true,
        "last_run_at": "2026-11-20T09:00:00Z", "next_due_at": "2026-11-20T09:15:00Z",
        "due_in_s": -9900.0, "failures": 0, "running": false, "overdue": true
      }
    ]
  },
  "freshness": {
    "last_odds_snapshot_at": "2026-11-20T11:00:00Z", "last_odds_snapshot_age_s": 3600.0,
    "last_event_seen_at": "2026-11-20T11:58:00Z", "last_event_seen_age_s": 120.0,
    "last_match_sync_at": "2026-11-20T11:50:00Z", "last_match_sync_age_s": 600.0,
    "last_prediction_at": "2026-11-20T11:40:00Z", "last_analysis_at": "2026-11-20T11:40:00Z",
    "last_decision_at": "2026-11-20T11:30:00Z",
    "sync_jobs": [{"kind": "odds", "name": "odds_collect", "last_success_at": "2026-11-20T11:59:00Z", "age_s": 60.0}]
  },
  "pending_link_reviews": 2,
  "alerts": [
    {"level": "critical", "code": "betclic_login_required", "message": "Betclic login required: open the login window and sign in"},
    {"level": "warning", "code": "scheduler_job_overdue", "message": "Job 'predict_upcoming' is overdue by 2.8 h - is the scheduler running?"},
    {"level": "warning", "code": "job_failing", "message": "Job 'settle_bets' failing (3 consecutive failures): RuntimeError: boom"},
    {"level": "info", "code": "dry_run_on", "message": "Dry-run mode is ON: bets are simulated, no real money is placed"},
    {"level": "info", "code": "link_review_pending", "message": "2 Betclic event(s) need link review (never bet until confirmed)"}
  ]
}
```

---

## Matches

### `GET /api/matches`

| Param | Type | Notes |
|---|---|---|
| `status` | list of `scheduled`, `live`, `finished`, `canceled`, `postponed`, `unknown` | |
| `from`, `to` | datetime | on `scheduled_at` |
| `in_scope` | bool | Tier 1/2 scope flag |
| `linked` | bool | `true`: has an **auto/confirmed** Betclic link; `false`: has none |
| `team_id` | int | either side |
| `order` | `asc` (default) / `desc` | by `scheduled_at` (matches without a time last) |
| `limit`, `offset` | int | |

Item fields: `betclic` is the most relevant non-rejected linked event (confirmed > auto > pending
review); `accepted_decisions` = number of distinct outcomes with at least one accepted decision;
`best_ev` = highest EV among available pre-match Betclic prices of auto/confirmed links that have
a latest model price (`null` if none).

```json
{
  "total": 3,
  "limit": 2,
  "offset": 0,
  "items": [
    {
      "id": 101,
      "scheduled_at": "2026-11-21T12:00:00Z",
      "status": "scheduled",
      "format": "bo3",
      "tier": "s",
      "stage": null,
      "is_lan": true,
      "in_scope": true,
      "data_completeness": 0.97,
      "team1": {"id": 1, "name": "Synth Team 1", "country": null, "logo_url": null, "hltv_rank": null},
      "team2": {"id": 2, "name": "Synth Team 2", "country": null, "logo_url": null, "hltv_rank": null},
      "tournament": {"id": 4, "name": "IEM Test Masters", "tier": "s", "is_lan": true},
      "team1_score": null,
      "team2_score": null,
      "winner_team_id": null,
      "betclic": {
        "id": 1, "bookmaker": "betclic", "external_id": "ev-101", "name": "Synth Team 1 - Synth Team 2",
        "team1_raw": "Synth Team 1", "team2_raw": "Synth Team 2", "competition_raw": "IEM",
        "starts_at": "2026-11-21T12:00:00Z", "url": "https://www.betclic.pl/e-sport/ev-101",
        "is_live": false, "match_id": 101, "link_status": "auto", "link_confidence": 0.97,
        "teams_swapped": false, "first_seen_at": "2026-11-20T06:00:00Z", "last_seen_at": "2026-11-20T11:58:00Z"
      },
      "accepted_decisions": 1,
      "best_ev": 0.3728,
      "has_analysis": true
    }
  ]
}
```

### `GET /api/matches/{id}`

Everything for the match page. 404 if unknown.

* `lineups.team1/team2`: players with `is_standin` and `role`.
* `vetoes`, `maps` (scores, sides, `picked_by`, `winner` as `team1`/`team2`), `context_flags`
  (Claude flags; `logit_shift` is from team1's perspective).
* `analysis`: latest `match_analyses` row: `payload` is the `MatchPrediction` JSON
  (`central/low/high` cores, `veto_distribution`, `map_estimates`, per-map `slots` with score
  pmfs, player projections ...) plus Claude's `rationale`.
* `betclic_events`: every event linked to the match, any link status.
* `markets`: one row per outcome. Latest predictions (`is_latest`) are joined on
  `(spec_key, selection_key)` with the mapped selections of **auto/confirmed** events. A
  prediction without a price has `odds: null`; a price without a model probability has
  `p_win: null`. `p_win` / `p_push` / `p_void` / `p_lose` are unconditional; `p_win_settled` =
  P(win | not refunded) is what the engine compares with `min_probability`. `fair_odds` =
  break-even odds without tax, `breakeven_odds` = smallest odds with `EV >= 0` after tax,
  `odds_stale` = price older than `betclic.odds_max_age_s`. `decision` is the latest decision for
  the outcome, `bet` the most relevant bet (`{id, status, result, dry_run, stake, ...}`; `result`
  as in [Bets](#bets)). Sorted by market taxonomy order.
* `unmapped_selections`: Betclic selections of linked events not mapped to our taxonomy yet.
* `tax_rate`: tax used for the EV column.

```json
{
  "match": {"id": 101, "status": "scheduled", "format": "bo3", "best_ev": 0.3728, "...": "same shape as a list item"},
  "lineups": {
    "team1": [{"player_id": 1, "nickname": "s1_awper", "is_standin": false, "role": "awper"}],
    "team2": [{"player_id": 30, "nickname": "s6_lurker", "is_standin": true, "role": "lurker"}]
  },
  "vetoes": [
    {"step": 1, "team_id": 1, "team": "team1", "action": "ban", "map_name": "nuke"},
    {"step": 2, "team_id": 2, "team": "team2", "action": "ban", "map_name": "train"}
  ],
  "maps": [
    {
      "id": 7, "map_number": 1, "map_name": "mirage", "status": "finished", "team1_rounds": 13, "team2_rounds": 9,
      "winner_team_id": 1, "winner": "team1", "team1_start_side": "ct", "team1_ct_rounds": 8, "team1_t_rounds": 5,
      "team2_ct_rounds": 4, "team2_t_rounds": 5, "overtime_rounds": 0, "picked_by_team_id": 2, "picked_by": "team2",
      "is_decider": false, "played_at": "2026-11-21T12:05:00Z"
    }
  ],
  "context_flags": [
    {
      "id": 1, "team_id": 2, "team": "team2", "flag_type": "stand_in", "severity": 0.6, "confidence": 0.9,
      "logit_shift": 0.12, "veto": false, "rationale": "Team 2 plays with a stand-in",
      "evidence": ["https://example.invalid/news/1"], "model": "claude-opus-5-5", "active": true,
      "created_at": "2026-11-20T10:00:00Z", "expires_at": null
    }
  ],
  "analysis": {
    "id": 2,
    "created_at": "2026-11-20T11:40:00Z",
    "model_version": "test-1",
    "payload": {
      "match_id": 101, "format": "bo3", "team1_id": 1, "team2_id": 2, "sigma_team1_win": 0.03,
      "central": {"p_team1_win": 0.8, "p_draw": 0.0, "map_score_pmf": {"2-0": 0.35, "2-1": 0.3, "1-2": 0.2, "0-2": 0.15}, "slots": []},
      "low": null, "high": null, "veto_distribution": [], "veto_known": false, "context_flags": []
    },
    "rationale": "Team 1 is much stronger on Mirage.",
    "rationale_model": "claude-opus-5-5"
  },
  "betclic_events": [{"id": 1, "external_id": "ev-101", "link_status": "auto", "...": "BookmakerEventRef"}],
  "markets": [
    {
      "market_type": "match_winner",
      "spec_key": "match_winner",
      "spec": {"market_type": "match_winner", "map_number": null, "maps_scope": null, "line": null, "team": null,
               "player_id": null, "player_name": null, "opponent_player_id": null, "half": null, "round_number": null},
      "selection_key": "team1",
      "label": "Match winner: Synth Team 1",
      "prediction_id": 2, "model_version": "test-1", "predicted_at": "2026-11-20T11:40:00Z",
      "p_win": 0.8, "p_push": 0.0, "p_void": 0.0, "p_lose": 0.2, "p_win_settled": 0.8, "sigma": 0.03,
      "fair_odds": "1.25", "breakeven_odds": "1.42",
      "selection_id": 1, "bookmaker_event_id": 1, "market_raw": "Zwycięzca meczu", "selection_raw": "Synth Team 1",
      "odds": "1.95", "effective_odds": "1.716", "odds_captured_at": "2026-11-20T11:59:00Z",
      "odds_age_s": 60.0, "odds_stale": false, "is_available": true, "event_is_live": false,
      "ev": 0.3728,
      "decision": {"id": 2, "accepted": true, "reasons": ["ok"], "decided_at": "2026-11-20T11:30:00Z", "odds": "1.95",
                   "p_win": 0.8, "sigma": 0.03, "ev": 0.3728, "kelly_fraction": 0.05, "proposed_stake": "15.00"},
      "bet": {"id": 3, "status": "placed", "result": null, "dry_run": false, "stake": "15.00", "requested_odds": "1.95",
              "placed_odds": "1.95", "placed_at": "2026-11-20T11:30:00Z", "pnl": null}
    },
    {
      "market_type": "total_maps", "spec_key": "total_maps|line=2.5", "selection_key": "over",
      "label": "Total maps over 2.5", "prediction_id": null, "p_win": null, "sigma": null,
      "selection_id": 5, "odds": "2.20", "odds_stale": true, "ev": null, "decision": null, "bet": null,
      "...": "remaining fields null"
    }
  ],
  "unmapped_selections": 1,
  "tax_rate": "0.12"
}
```

### `GET /api/matches/{id}/odds-history`

| Param | Type | Notes |
|---|---|---|
| `selection_id` | int, optional | one selection; it must belong to an event linked to this match (else 404). Omitted: every mapped selection of the match's non-rejected events. |
| `since` | datetime, optional | only snapshots captured at/after |
| `max_points` | int 10..20000, default 2000 | per series (evenly thinned, first/last kept) |

Snapshots are written only when the price/availability changes, so draw them as a step line.

```json
{
  "match_id": 101,
  "series": [
    {
      "selection_id": 1,
      "bookmaker_event_id": 1,
      "market_raw": "Zwycięzca meczu",
      "selection_raw": "Synth Team 1",
      "spec_key": "match_winner",
      "selection_key": "team1",
      "label": "Match winner: Synth Team 1",
      "current_odds": "1.95",
      "points": [
        {"captured_at": "2026-11-20T09:00:00Z", "odds": "2.05", "is_available": true},
        {"captured_at": "2026-11-20T10:00:00Z", "odds": "2.00", "is_available": true},
        {"captured_at": "2026-11-20T11:00:00Z", "odds": "1.95", "is_available": true}
      ]
    }
  ]
}
```

---

## Bets

### `GET /api/bets`

| Param | Type | Notes |
|---|---|---|
| `status` | list of `pending`, `placing`, `placed`, `won`, `lost`, `void`, `failed`, `cancelled`, `dry_run` | matches the stored status **or** the `result`: `won` also returns paper bets that won; `dry_run` returns every paper row |
| `dry_run` | bool | omitted = real and paper bets |
| `from`, `to` | datetime | on `placed_at` (falls back to `created_at`) |
| `match_id` | int | |
| `limit`, `offset` | int | newest first |

`summary` covers the whole filtered set (not just the page): `stake` excludes failed/cancelled
bets, `pnl` sums settled bets, `open` counts pending/placing/placed bets and unsettled paper bets;
`won` / `lost` / `void` count results (settled paper bets included).

Per bet: `status` is the stored status; **`result`** is `"won"` / `"lost"` / `"void"` once the bet
is settled (also for paper bets, whose `status` stays `"dry_run"`), else `null`; `is_open` is true
while money (or paper money) is still at stake.

```json
{
  "total": 8,
  "limit": 2,
  "offset": 0,
  "items": [
    {
      "id": 3,
      "match_id": 101,
      "match": {
        "id": 101, "scheduled_at": "2026-11-21T12:00:00Z", "status": "scheduled", "format": "bo3",
        "team1": {"id": 1, "name": "Synth Team 1", "country": null, "logo_url": null, "hltv_rank": null},
        "team2": {"id": 2, "name": "Synth Team 2", "country": null, "logo_url": null, "hltv_rank": null},
        "tournament": "IEM Test Masters"
      },
      "decision_id": 2,
      "selection_id": 1,
      "bookmaker": "betclic",
      "external_event_id": "ev-101",
      "market_raw": "Zwycięzca meczu",
      "selection_raw": "Synth Team 1",
      "market_type": "match_winner",
      "spec_key": "match_winner",
      "selection_key": "team1",
      "label": "Match winner: Synth Team 1",
      "requested_odds": "1.95",
      "min_acceptable_odds": "1.85",
      "placed_odds": "1.95",
      "stake": "15.00",
      "tax": "1.80",
      "potential_payout": null,
      "status": "placed",
      "result": null,
      "is_open": true,
      "dry_run": false,
      "external_bet_id": null,
      "has_screenshot": true,
      "screenshot_url": "/api/bets/3/screenshot",
      "error": null,
      "created_at": "2026-11-20T11:30:00Z",
      "placed_at": "2026-11-20T11:30:00Z",
      "settled_at": null,
      "payout": null,
      "pnl": null,
      "closing_odds": null,
      "clv": null,
      "p_win": 0.8,
      "ev": 0.3728
    }
  ],
  "summary": {"count": 8, "stake": "95.00", "pnl": "-8.96", "open": 2, "won": 2, "lost": 1, "void": 1}
}
```

### `GET /api/bets/{id}`

The bet, its decision (incl. `settings_snapshot`), its ledger entries and the latest Claude
rationale of the match. 404 if unknown.

```json
{
  "bet": {"id": 1, "status": "won", "result": "won", "is_open": false, "stake": "20.00", "pnl": "13.44", "clv": 0.03, "...": "BetOut as above"},
  "decision": {
    "id": 4, "match_id": 1, "match": {"id": 1, "...": "MatchBrief"}, "prediction_id": null, "selection_id": null,
    "market_type": "match_winner", "spec_key": "match_winner", "selection_key": "team1",
    "label": "Match winner: Synth Team 6", "odds": "1.90", "odds_captured_at": "2026-11-17T08:59:00Z",
    "decided_at": "2026-11-17T09:00:00Z", "accepted": true, "reasons": ["ok"], "p_win": 0.78, "sigma": 0.03,
    "ev": 0.3, "kelly_fraction": 0.05, "proposed_stake": "20.00", "bet_id": 1, "settings_snapshot": {"min_ev": 0.03}
  },
  "ledger": [
    {"id": 2, "ts": "2026-11-17T10:00:00Z", "kind": "stake", "amount": "-20.00", "balance_after": "980.00", "bet_id": 1, "note": null},
    {"id": 4, "ts": "2026-11-17T14:00:00Z", "kind": "payout", "amount": "33.44", "balance_after": "1003.44", "bet_id": 1, "note": null}
  ],
  "analysis_id": null,
  "rationale": null
}
```

### `GET /api/bets/{id}/screenshot`

The placement screenshot (`image/png`, `image/jpeg`, `image/webp`, `image/gif`), served inline.
The stored path may be absolute, relative to the working directory / project root, or relative to
`executor.screenshot_dir`; it is served **only** if, after resolving `..` and symlinks, it lies
inside `executor.screenshot_dir` and has an image extension. Otherwise 404 (`"this bet has no
screenshot"` / `"screenshot not found"`).

---

## Bankroll

### `GET /api/bankroll`

| Param | Type | Notes |
|---|---|---|
| `dry_run` | bool, default `false` | `false`: real-money bets + ledger; `true`: paper bets |
| `max_points` | int 10..20000, default 2000 | per curve |

The numbers follow the staking engine (`cs2bot.engine.bankroll`), so the page shows what the bot
actually enforces:

* `balance`: `balance_after` of the most recently **written** ledger row (`balance_source:
  "ledger"`; back-dated rows never change it); without ledger rows the effective starting bankroll
  adjusted by realized P&L (`"starting_bankroll"`); paper mode: starting bankroll + paper P&L - open
  paper stakes (`"paper"`).
* `open_exposure` / `open_bets`: pending, placing, placed bets (paper mode: unsettled paper bets).
  `bankroll_reference` = balance + open stakes already debited (cash + money at risk; stakes and
  limits are sized from it). `available_balance` = balance - open stakes **not** debited yet
  (pending bets and placements the bookmaker has not confirmed) = cash free for new stakes.
* `daily` / `weekly`: loss limits with the **effective** bankroll settings (defaults + dashboard
  overrides) and Warsaw day/week boundaries.
  `start_bankroll` = `bankroll_reference` - realized P&L of the period (a deposit or withdrawal
  made during the period counts as if made at its start);
  `limit_amount` = `start_bankroll * limit_fraction`, rounded **down** to the grosz;
  `reached` when the realized loss of the period >= `limit_amount` (the bot pauses itself);
  `remaining` = limit - realized loss; `open_stakes` = **all** stakes currently at risk;
  `worst_case_pnl` = realized - open stakes; `stake_allowance` = limit - (realized loss + open
  stakes), floored at 0: what the engine's daily / weekly guard still lets it stake.
  In paper mode the limits are shown for information only (the engine applies them to real money).
* `stats`: `turnover` (stakes of won+lost bets), `realized_pnl`, `roi` = realized P&L / capital
  base (ledger deposits, else starting bankroll), `yield_rate` = realized P&L / turnover,
  `hit_rate` = won / (won + lost), `avg_odds`, `avg_clv` (bets with CLV), `max_drawdown` = largest
  fall of cumulative realized P&L from its peak (settlement order), `max_drawdown_pct` = that fall /
  (capital base + peak).
* `equity_curve`: one point per ledger entry in time order with the running balance (re-accumulated
  in that order, so a back-dated entry cannot make the curve jump; the last point is `balance`).
  Paper mode: start + one point per settlement. `pnl_curve`: cumulative realized P&L per settlement.
* `pnl_by_day` / `pnl_by_week` (ISO week, `start` = Monday) / `pnl_by_month`: settled bets
  grouped by local settlement date; only periods with settlements are listed.
* `settings`: the effective bankroll settings.

```json
{
  "mode": "real",
  "currency": "PLN",
  "as_of": "2026-11-20T12:00:00Z",
  "balance": "968.44",
  "balance_source": "ledger",
  "available_balance": "968.44",
  "open_exposure": "15.00",
  "open_bets": 1,
  "bankroll_reference": "983.44",
  "exposure_by_match": [{"match_id": 101, "stake": "15.00", "bets": 1}],
  "daily": {
    "period": "day", "period_start": "2026-11-19T23:00:00Z", "start_bankroll": "1013.44",
    "limit_fraction": 0.05, "limit_amount": "50.67", "realized_pnl": "-30.00", "open_stakes": "15.00",
    "worst_case_pnl": "-45.00", "remaining": "20.67", "stake_allowance": "5.67",
    "used_fraction": 0.592066, "reached": false
  },
  "weekly": {
    "period": "week", "period_start": "2026-11-15T23:00:00Z", "start_bankroll": "1000.00",
    "limit_fraction": 0.1, "limit_amount": "100.00", "realized_pnl": "-16.56", "open_stakes": "15.00",
    "worst_case_pnl": "-31.56", "remaining": "83.44", "stake_allowance": "68.44",
    "used_fraction": 0.1656, "reached": false
  },
  "trading_halted": false,
  "halt_reason": null,
  "stats": {
    "bets": 4, "open_bets": 1, "settled_bets": 3, "won": 1, "lost": 1, "void": 1,
    "turnover": "50.00", "realized_pnl": "-16.56", "capital_base": "1000.00",
    "roi": -0.01656, "yield_rate": -0.3312, "hit_rate": 0.5, "avg_odds": "2.00",
    "avg_clv": 0.01, "clv_bets": 2, "max_drawdown": "30.00", "max_drawdown_pct": 0.029602
  },
  "equity_curve": [
    {"ts": "2026-11-10T09:00:00Z", "balance": "1000.00", "kind": "deposit", "amount": "1000.00"},
    {"ts": "2026-11-17T10:00:00Z", "balance": "980.00", "kind": "stake", "amount": "-20.00"}
  ],
  "pnl_curve": [
    {"ts": "2026-11-17T14:00:00Z", "cumulative_pnl": "13.44"},
    {"ts": "2026-11-20T11:00:00Z", "cumulative_pnl": "-16.56"}
  ],
  "pnl_by_day": [
    {"period": "2026-11-17", "start": "2026-11-17", "bets": 1, "won": 1, "lost": 0, "void": 0, "staked": "20.00", "pnl": "13.44", "yield_rate": 0.672}
  ],
  "pnl_by_week": [
    {"period": "2026-W47", "start": "2026-11-16", "bets": 3, "won": 1, "lost": 1, "void": 1, "staked": "50.00", "pnl": "-16.56", "yield_rate": -0.3312}
  ],
  "pnl_by_month": [
    {"period": "2026-11", "start": "2026-11-01", "bets": 3, "won": 1, "lost": 1, "void": 1, "staked": "50.00", "pnl": "-16.56", "yield_rate": -0.3312}
  ],
  "settings": {
    "starting_bankroll": "1000.00", "currency": "PLN", "kelly_fraction": 0.25, "max_stake_fraction": 0.02,
    "max_match_exposure_fraction": 0.04, "daily_loss_limit_fraction": 0.05, "weekly_loss_limit_fraction": 0.1,
    "max_bets_per_day": 10, "stake_step": "0.10"
  }
}
```

---

## Performance

### `GET /api/performance/calibration`

| Param | Type | Notes |
|---|---|---|
| `bins` | int 2..50, default 10 | equal-width probability bins |
| `mode` | `all` (default) / `real` / `dry_run` | which bets |
| `from`, `to` | datetime | bets: `settled_at`; predictions: match `scheduled_at` |

Calibration compares probabilities with won / lost results only, so the probability used is always
`P(win | not refunded) = p_win / (1 - p_push - p_void)` (see [Conventions](#conventions)).

* `bets`: won / lost bets that have a decision (settled paper bets included in `all` / `dry_run`);
  probability = the decision's `p_win`, conditioned with the `p_push` / `p_void` of the prediction
  the decision was made from (when the decision still links to it).
* `predictions`: latest (`is_latest`) match-winner and map-winner predictions of **finished**
  matches, one selection per market (team1's row when present, so complementary rows are not
  double counted); unplayed maps are skipped. No look-ahead: a prediction written at or after the
  match's `scheduled_at` is ignored. `predictions_by_market` splits them by market type.
* Each set: `n`, `base_rate`, `mean_predicted`, `brier`, `log_loss`, `ece`, `bins[]` (`count`,
  `mean_predicted`, `observed_rate`, 95% Wilson `ci_low` / `ci_high`) and `brier_by_month`.

```json
{
  "n_bins": 10,
  "bets": {
    "n": 2, "base_rate": 0.5, "mean_predicted": 0.77, "brier": 0.313, "log_loss": 0.837789, "ece": 0.27,
    "bins": [
      {"lower": 0.0, "upper": 0.1, "count": 0, "mean_predicted": null, "observed_rate": null, "ci_low": null, "ci_high": null},
      {"lower": 0.7, "upper": 0.8, "count": 2, "mean_predicted": 0.77, "observed_rate": 0.5, "ci_low": 0.094529, "ci_high": 0.905471}
    ],
    "brier_by_month": [{"period": "2026-11", "n": 2, "brier": 0.313}]
  },
  "predictions": {"n": 20, "base_rate": 0.1, "mean_predicted": 0.575, "brier": 0.31625, "log_loss": 0.827092, "ece": 0.475, "bins": [], "brier_by_month": []},
  "predictions_by_market": {
    "map_winner": {"n": 10, "brier": 0.2925, "...": "CalibrationSet"},
    "match_winner": {"n": 10, "brier": 0.34, "...": "CalibrationSet"}
  }
}
```

### `GET /api/performance/markets`

| Param | Type | Notes |
|---|---|---|
| `mode` | `real` (default) / `dry_run` / `all` | |
| `from`, `to` | datetime | on the bet's placement time |

Per market type (taxonomy order) and in `total`: bets (excluding failed/cancelled), open, settled,
won/lost/void, `hit_rate`, `staked` (turnover), `pnl`, `roi` (= pnl / staked), `avg_odds`,
`avg_clv`, `avg_ev` (decision EV).

```json
{
  "mode": "real",
  "items": [
    {"market_type": "match_winner", "bets": 2, "open": 1, "settled": 1, "won": 1, "lost": 0, "void": 0, "hit_rate": 1.0,
     "staked": "20.00", "pnl": "13.44", "roi": 0.672, "avg_odds": "1.90", "avg_clv": 0.03, "avg_ev": 0.3364},
    {"market_type": "map_winner", "bets": 1, "open": 0, "settled": 1, "won": 0, "lost": 1, "void": 0, "hit_rate": 0.0,
     "staked": "30.00", "pnl": "-30.00", "roi": -1.0, "avg_odds": "2.10", "avg_clv": -0.01, "avg_ev": 0.4}
  ],
  "total": {"market_type": "all", "bets": 4, "open": 1, "settled": 3, "won": 1, "lost": 1, "void": 1, "hit_rate": 0.5,
            "staked": "50.00", "pnl": "-16.56", "roi": -0.3312, "avg_odds": "2.00", "avg_clv": 0.01, "avg_ev": 0.3576}
}
```

---

## Decisions

### `GET /api/decisions`

| Param | Type | Notes |
|---|---|---|
| `match_id` | int | |
| `accepted` | bool | |
| `from`, `to` | datetime | on `decided_at` |
| `limit`, `offset` | int | newest first |

`bet_id` is the newest bet created from the decision (if any).

```json
{
  "total": 3,
  "limit": 2,
  "offset": 0,
  "items": [
    {
      "id": 3,
      "match_id": 101,
      "match": {"id": 101, "scheduled_at": "2026-11-21T12:00:00Z", "status": "scheduled", "format": "bo3",
                "team1": {"id": 1, "name": "Synth Team 1", "country": null, "logo_url": null, "hltv_rank": null},
                "team2": {"id": 2, "name": "Synth Team 2", "country": null, "logo_url": null, "hltv_rank": null},
                "tournament": "IEM Test Masters"},
      "prediction_id": null,
      "selection_id": 3,
      "market_type": "map_handicap",
      "spec_key": "map_handicap|line=-1.5",
      "selection_key": "team1",
      "label": "Map handicap: Synth Team 1 -1.5",
      "odds": "3.10",
      "odds_captured_at": "2026-11-20T11:29:00Z",
      "decided_at": "2026-11-20T11:30:00Z",
      "accepted": false,
      "reasons": ["p_win 0.45 < 0.75"],
      "p_win": 0.45,
      "sigma": 0.03,
      "ev": 0.2276,
      "kelly_fraction": 0.0,
      "proposed_stake": null,
      "bet_id": null
    }
  ]
}
```

---

## Controls

All changes go through `cs2bot.db.state` and write an `audit_log` row (actor `dashboard`, or
`dashboard:<username>` when auth is on). All mutations need `X-Requested-With: cs2bot`.

### `GET /api/control/state`

```json
{
  "halted": true,
  "reason": "kill switch is on",
  "kill_switch": {"on": true, "reason": "manual stop", "updated_at": "2026-11-20T12:00:01Z", "updated_by": "dashboard"},
  "paused": {"on": false, "reason": null, "updated_at": "2026-11-20T12:00:03Z", "updated_by": "dashboard"}
}
```

### `GET /api/control/kill-switch` · `POST /api/control/kill-switch`

Body: `{"on": true, "reason": "manual stop"}` (`reason` optional, <= 500 chars). Response:

```json
{"on": true, "reason": "manual stop", "updated_at": "2026-11-20T12:00:01Z", "updated_by": "dashboard"}
```

### `POST /api/control/pause` · `POST /api/control/resume`

Optional body `{"reason": "taking a break"}`. Pause halts trading until resumed (the engine also
pauses itself when a loss limit is hit). Resume clears the pause flag only - it does **not** turn
off the kill switch. Response: the trading state (as `GET /api/control/state`).

### `GET /api/control/config`

`effective` = what the engine uses (defaults + overrides), `defaults` = `.env` / built-in values,
`overrides` = what the dashboard stored. Decimal fields are strings.

```json
{
  "effective": {
    "decision": {
      "min_probability": 0.75, "min_ev": 0.05, "max_sigma": 0.05, "min_data_completeness": 0.95,
      "min_event_link_confidence": 0.9, "min_odds": "1.01", "max_odds": "15.0", "min_minutes_before_start": 10,
      "max_hours_before_start": 48, "prefer_known_veto": true, "market_overrides": {}, "disabled_markets": []
    },
    "bankroll": {
      "starting_bankroll": "1000.00", "currency": "PLN", "kelly_fraction": 0.2, "max_stake_fraction": 0.02,
      "max_match_exposure_fraction": 0.04, "daily_loss_limit_fraction": 0.05, "weekly_loss_limit_fraction": 0.1,
      "max_bets_per_day": 10, "stake_step": "0.10"
    }
  },
  "defaults": {"decision": {"min_ev": 0.03, "...": "all fields"}, "bankroll": {"kelly_fraction": 0.25, "...": "all fields"}},
  "overrides": {"decision": {"min_ev": 0.05}, "bankroll": {"kelly_fraction": 0.2}},
  "updated_at": "2026-11-20T12:00:05Z",
  "updated_by": "dashboard",
  "warnings": []
}
```

### `PUT /api/control/config`

Body: `{"decision": {...} | null, "bankroll": {...} | null}`.

* section omitted / `null` -> its stored overrides are kept;
* `{}` -> that section's overrides are cleared;
* fields -> they replace that section's overrides (send every override you want to keep in that
  section). Fields equal to the default are dropped. `market_overrides` replaces the whole dict.

Validation (422 with one item per problem, nothing is stored): unknown sections / fields, wrong
types, non-finite numbers (`NaN` / `Infinity`, which Python's JSON parser would otherwise accept and
which would silently disable a threshold), and ranges: `min_probability` (0, 1); `min_ev` [0, 1]; `max_sigma` (0, 0.5];
`min_data_completeness`, `min_event_link_confidence` [0, 1]; `min_odds` >= 1 and
`min_odds` < `max_odds` <= 1000; `min_minutes_before_start` [0, 10080];
`max_hours_before_start` (0, 720]; `market_overrides` keys must be market types with thresholds in
the same ranges; `disabled_markets` must be market types; `starting_bankroll` >= 0; `currency` 3
capital letters; `kelly_fraction`, `max_stake_fraction`, `max_match_exposure_fraction`,
`daily_loss_limit_fraction`, `weekly_loss_limit_fraction` (0, 1]; `max_bets_per_day` [0, 1000];
`stake_step` (0, 1000]. Response: same as `GET`.

```json
{"detail": [{"loc": ["body", "bankroll", "kelly_fraction"], "msg": "must be in (0, 1]", "type": "value_error"}]}
```

### `GET /api/control/betclic-login` · `POST /api/control/betclic-login`

POST opens the executor's visible Betclic login window (manual login incl. 2FA) in a background
thread and answers **202**; **501** if no launcher was configured, **409** while a window is already
open. GET reports the last run: `last_result` is `true` / `false` when the launcher reports whether
the login was completed (`null` while running or if unknown), `last_error` the exception type and
message if it crashed. Poll GET while `running` is `true`.

```json
{"configured": true, "running": true, "started": true, "last_started_at": "2026-11-20T12:00:07Z", "last_finished_at": null, "last_result": null, "last_error": null}
```

---

## Link review queue

Betclic events linked with low confidence (`link_status = pending_review`) are never bet on until
the owner decides. Manual decisions (`confirmed` / `rejected`) must never be overridden by the
odds collector.

### `GET /api/review/links`

| Param | Type | Notes |
|---|---|---|
| `status` | list of link statuses, default `pending_review` | e.g. `rejected` to audit past rejections |
| `window_hours` | float, default 24 | candidates must start within +- this of the event |
| `max_candidates` | int, default 5 | the currently linked match is always included |
| `limit`, `offset` | int | events by start time |

Candidate `score` = 0.8 x team-name similarity (best orientation, using team names and aliases,
`cs2bot.utils.names`) + 0.2 x start-time closeness. `teams_swapped: true` means Betclic lists our
team2 first.

```json
{
  "total": 2,
  "limit": 1,
  "offset": 0,
  "items": [
    {
      "event": {
        "id": 2, "bookmaker": "betclic", "external_id": "ev-103", "name": "Synth Team 6 - Synth Team 5",
        "team1_raw": "Synth Team 6", "team2_raw": "Synth Team 5", "competition_raw": null,
        "starts_at": "2026-11-20T15:15:00Z", "url": null, "is_live": false, "match_id": 103,
        "link_status": "pending_review", "link_confidence": 0.7, "teams_swapped": false,
        "first_seen_at": "2026-11-20T06:00:00Z", "last_seen_at": "2026-11-20T11:58:00Z"
      },
      "n_markets": 1,
      "current_match": {"id": 103, "scheduled_at": "2026-11-20T15:00:00Z", "status": "scheduled", "format": "bo3",
                        "team1": {"id": 5, "name": "Synth Team 5", "country": null, "logo_url": null, "hltv_rank": null},
                        "team2": {"id": 6, "name": "Synth Team 6", "country": null, "logo_url": null, "hltv_rank": null},
                        "tournament": null},
      "candidates": [
        {"match": {"id": 103, "...": "MatchBrief"}, "score": 0.9979, "name_score": 100.0, "time_diff_minutes": 15.0,
         "teams_swapped": true, "is_current": true},
        {"match": {"id": 101, "...": "MatchBrief"}, "score": 0.7128, "name_score": 85.71, "time_diff_minutes": 1245.0,
         "teams_swapped": false, "is_current": false}
      ]
    }
  ]
}
```

### `POST /api/review/links/{event_id}`

Body: `{"action": "confirm" | "reject" | "relink", "match_id": int?, "teams_swapped": bool?}`.

* `confirm`: link to `match_id` (default: the current match; 422 if there is none) with
  `link_status = confirmed`, `link_confidence = 1.0`. `teams_swapped` defaults to the current value
  for the same match (send the candidate's `teams_swapped` to accept the suggested orientation).
* `relink`: `match_id` required (422 if missing, 404 if unknown); `teams_swapped` defaults to the
  orientation detected from the team names.
* `reject`: `link_status = rejected`, `match_id = null` (no `match_id` / `teams_swapped` allowed).

If the linked match or the orientation changes, the event's market/selection mappings are cleared
(`is_mapped = false`, `markets_reset` = markets that were mapped) and `extra.remap_required = true`
is set, so no selection keyed for the old orientation can be priced or bet. If the app was built
with an `event_remapper` (the odds collector's `remap_event`), the markets are re-mapped right away
(`remapped: true`; `false` if the re-mapper failed, in which case the mappings stay cleared);
otherwise `remapped` is `null` and the collector re-maps them on its next poll (it re-maps every
market on every pass). `open_bets` warns about unsettled bets (real or paper) on the event.
404 if the event does not exist.

```json
{
  "event": {"id": 2, "external_id": "ev-103", "match_id": 103, "link_status": "confirmed", "link_confidence": 1.0,
            "teams_swapped": true, "...": "BookmakerEventRef"},
  "action": "confirm",
  "previous_match_id": 103,
  "previous_status": "pending_review",
  "previous_teams_swapped": false,
  "markets_reset": 1,
  "remapped": null,
  "open_bets": 0
}
```

---

## Jobs

### `GET /api/jobs`

| Param | Type | Notes |
|---|---|---|
| `name` | string | exact job name |
| `status` | list of `running`, `success`, `partial`, `failed` | |
| `limit`, `offset` | int | newest first |

```json
{
  "total": 9,
  "limit": 2,
  "offset": 0,
  "items": [
    {"id": 2, "job_name": "odds_collect", "status": "success", "started_at": "2026-11-20T11:58:00Z",
     "finished_at": "2026-11-20T11:59:00Z", "duration_s": 60.0, "items": 3, "error": null, "details": {}}
  ]
}
```

---

## Teams

### `GET /api/teams/{id}`

| Param | Type | Notes |
|---|---|---|
| `limit` | int 1..100, default 20 | recent finished matches |
| `window_days` | int, default 180 | window for map and player statistics |

`form` = results of the last 10 finished matches (newest first). `maps` = per-map record in the
window incl. picks, bans and deciders. `players` = current roster with stats in the window (one
stat line per map; HLTV preferred over demo / PandaScore when several sources exist). 404 if
unknown.

```json
{
  "team": {"id": 1, "name": "Synth Team 1", "slug": "synth-1", "country": null, "logo_url": null, "hltv_rank": null,
           "hltv_rank_updated_at": null, "pandascore_id": null, "hltv_id": null, "aliases": ["synth 1"]},
  "window_days": 180,
  "form": ["W", "W"],
  "recent_results": [
    {"match_id": 21, "scheduled_at": "2026-10-02T22:14:52Z",
     "opponent": {"id": 2, "name": "Synth Team 2", "country": null, "logo_url": null, "hltv_rank": null},
     "tournament": "Synthetic B-tier", "tier": "b", "format": "bo1", "team_score": 1, "opponent_score": 0, "result": "W",
     "maps": [{"map_number": 1, "map_name": "inferno", "team_rounds": 13, "opponent_rounds": 11, "won": true}]}
  ],
  "upcoming": [{"id": 101, "scheduled_at": "2026-11-21T12:00:00Z", "status": "scheduled", "format": "bo3", "...": "MatchBrief"}],
  "maps": [
    {"map_name": "inferno", "played": 4, "won": 1, "win_rate": 0.25, "rounds_for": 39, "rounds_against": 53,
     "avg_round_diff": -3.5, "picked": 0, "picked_by_opponent": 3, "deciders": 1, "bans": 2, "opponent_bans": 1,
     "last_played_at": "2026-10-02T22:14:52Z"}
  ],
  "players": [
    {"id": 1, "nickname": "s1_awper", "real_name": null, "country": null, "primary_role": "awper", "role_confidence": null,
     "hltv_id": null, "maps": 15, "kills": 235, "deaths": 193, "kd": 1.218, "kpr": 0.662, "avg_rating": null,
     "avg_adr": null, "last_played_at": "2026-10-02T22:14:52Z"}
  ]
}
```

---

## Static dashboard

When `api.serve_dashboard` is true and `api.dashboard_dist_dir` (default `dashboard/dist`,
resolved against the working directory, then the project root) contains `index.html`:

* `/` and every non-`/api` path without a matching file -> `index.html` (SPA routing,
  `Cache-Control: no-cache`);
* existing files are served (`/assets/*` with `Cache-Control: public, max-age=31536000, immutable`);
* a missing path whose last segment has a file extension -> 404 (not `index.html`);
* unknown `/api/*` paths -> JSON 404.

Otherwise `/` returns:

```json
{"name": "cs2bot API", "version": "0.1.0", "dashboard": "not built - run `npm install && npm run build` in dashboard/ and restart", "api_docs": "/api/docs"}
```

---

## Python API

```python
from cs2bot.api import ApiOptions, create_app, make_server, run

# FastAPI app
app = create_app(
    settings,
    session_factory,
    login_launcher=executor.open_login_window,
    executor_health_provider=lambda: cached_health,
    event_remapper=lambda session, event: remap_event(session, event, mapper, settings=settings),
    scheduler_status_provider=scheduler.status,
)

# uvicorn.Server: call server.run() in a thread, set server.should_exit = True to stop it
server = make_server(settings, session_factory)

# blocking
run(settings)
```

* `login_launcher: Callable[[], object]` - may block (runs in a daemon thread, one at a time);
  a `bool` return value is reported as `last_result` (e.g. `executor.open_login_window`).
* `event_remapper: Callable[[Session, BookmakerEvent], object]` - optional; called (in a fresh
  transaction, after the review decision is committed) when a review changed an event's match or
  orientation.
* `executor_health_provider: Callable[[], ExecutorHealth | None]` - must be fast (return a cached
  value); `None` falls back to `system_state["executor_health"]`
  (`ExecutorHealth.model_dump(mode="json")`).
* `scheduler_status_provider: Callable[[], Sequence[Mapping[str, object]]]` - optional;
  `cs2bot.flows.scheduler.Scheduler.status` (one dict per job: `name`, `lane`, `enabled`,
  `last_run`, `next_due`, `failures`). Shown under `/api/health` (`scheduler`, plus `scheduled` /
  `next_due_at` / `scheduler_failures` on each job). Must be fast; a failure only raises the
  `scheduler_status_error` alert.
* Settings read from `settings.api`: `host`, `port`, `cors_origins`, `allowed_hosts` (extra Host
  names, e.g. a LAN name when binding to `0.0.0.0`), `username`, `password`, `session_secret`,
  `session_max_age_s`, `login_max_failures`, `login_global_max_failures`, `login_window_s`,
  `serve_dashboard`, `dashboard_dist_dir`.
* `ApiOptions` - tunables without a setting: cookie name, page sizes, review window, health
  thresholds (`stuck_job_after_s`, `executor_health_stale_s`, `failing_job_threshold`,
  `scheduler_overdue_grace_s`). Its `login_*` fields (default `None` = use the settings) and
  `extra_allowed_hosts` (added to `api.allowed_hosts`) override the settings, mainly for tests.
