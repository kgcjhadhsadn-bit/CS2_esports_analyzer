# Build status

Legend: `done` = finished; `wip` = partly written, not finished; `-` = not started.
Test counts are the package's own suite as reported in `docs/dev/reports/<package>.md`.

| Package | Implemented | Tests | Reviewed | Notes |
|---|---|---|---|---|
| foundation (config, domain, db, utils, synthetic, scheduler) | done | 46 | by author | integration applied small changes (see reports/integration.md) |
| odds (Betclic collection, mapping, linking) | done | 374 | round 1 | unverified against live Betclic.pl |
| hltv | done | 166 | round 1 | parsers built from open-source scrapers, not live pages |
| engine (pricing, EV/Kelly, decision, bankroll, ledger, settlement) | done | 567 | rounds 1-2 | |
| sim (round model, map simulator, kill props, match engine) | done | 116 | round 1 | |
| executor (Betclic placement) | done | 390 | rounds 1-2 | selectors unverified against the live site |
| api (local dashboard backend) | done | 224 | round 1 | |
| ratings_veto | done | 183 | round 1 | |
| datasets_features | done | 71 | round 1 | |
| ensemble_backtest | done | 144 | round 1 | |
| pandascore | done | 168 | round 1 | no token available: fixtures from the documentation |
| demos | done | 313 | round 1 | |
| llm (Claude context layer) | done | 233 | round 1 | |
| dashboard (React) | done | 79 (vitest) | round 1 | |
| ops (Windows install, CI, docs) | done | 37 | round 1 | PowerShell scripts never run on Windows |
| integration (services, jobs, `cs2bot up`, CLI, end-to-end test) | done | 58 | round 1 | whole suite 2973 passed |
| final cross-cutting review (Lens A: money / safety path) | done | 15 | - | 6 bugs fixed (1 critical: confirmed link + Betclic team-order flip); migration 0002; see reports/final_review.md |
| final cross-cutting review (Lens B: data / model correctness, docs) | done | 12 | - | 6 issues fixed (high: ensemble predictions depended on which team is team1; stale context flags after a participant change; backtest != live economy / model config / post-match start sides; doctor shows uncalibrated bundles); docs aligned; see reports/final_review.md |
| cloud_autonomy (auto-login, structured pauses + auto-resume + circuit breaker, Telegram, headless/Xvfb, API/dashboard) | done | 74 + 5 (vitest) | round 1 | see reports/cloud_autonomy.md; Telegram / Betclic login unverified against the live services |
| cloud_deploy (Dockerfile, docker-compose.cloud.yml, Caddy, deploy/linux scripts, VPS_SETUP) | done | validation only (CI `docker` job) | round 1 | see reports/cloud_deploy.md; image never built here (no Docker daemon) |
| deep_data (deep features, odds movement / sharp benchmark, high-confidence calibration + trust, HLTV proxy fallback, PandaScore optional) | done | 58 + 16 (review) | round 1 | see reports/deep_data.md; real HLTV deep fields unverified |
| final cross-cutting review (Lens C: cloud runtime, deep data reaching the decision, confidence rule) | done | 69 + 2 (vitest) | - | 10 issues fixed (high: Kelly still sized the "odds ignored" picks - confidence tiers implemented; a win below 1.137 loses money - profitable-win floor; deep features never reached training - one store builder; guard never applied / never refreshed on the VPS - wired + weekly job); see reports/final_review.md (VPS owner checklist) |

## Verification on the build machine (2026-11-24, after Lens C)

- Whole suite `.venv/bin/python -m pytest tests -n 2`: 3241 passed, 21 skipped (browser / PostgreSQL
  without a database), 0 failed; ruff, ruff format, mypy (203 files) clean; dashboard lint, 86 vitest
  tests, `npm run build` OK; `docker compose -f docker-compose.cloud.yml config` renders.
- Bet rule in code = PLAN.md section 0: confidence only (p >= 0.75 after the high-confidence guard),
  odds ignored except the 1.137 "a win must be a win" floor, stakes by confidence tier with every cap.

## Earlier verification on the build machine (2026-11-24)

- Whole suite: all tests pass (SQLite), plus the `postgres`-marked tests against PostgreSQL 16.
- Alembic: `downgrade base` -> `upgrade head` (0002) -> `alembic check` clean on PostgreSQL 16.
- `cs2bot db seed-synthetic` + `cs2bot models train` on PostgreSQL: bundle trained incl. ensemble.
- ruff, ruff format, mypy (190 files): clean. Dashboard: `npm run build` OK.
- Not verifiable here: live Betclic.pl, HLTV, PandaScore, the Anthropic API, and the PowerShell scripts on Windows.
