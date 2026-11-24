# Architecture & module contracts

This is the engineering companion to [PLAN.md](../PLAN.md). It defines the package layout, who
owns what, and the public interfaces packages use to talk to each other. **Code against these
interfaces, not against another package's internals.**

## Layers

```
            ┌──────────────────────────── data plane ─────────────────────────────┐
 sources →  │ ingest.pandascore   ingest.hltv   ingest.demos   odds (Betclic)       │ → PostgreSQL
            └──────────────────────────────────────────────────────────────────────┘
            ┌──────────────────────────── model plane ────────────────────────────┐
 DB →       │ datasets → features → models.ratings / models.veto / models.ensemble │
            │            models.round_model → models.map_sim → models.player_props │
            │            models.match_engine (→ MatchPrediction)  llm.context       │
            │ engine.pricing → engine.decision → engine.bankroll    backtest        │
            └──────────────────────────────────────────────────────────────────────┘
            ┌──────────────────────────── ops plane ──────────────────────────────┐
            │ services (glue) · flows (scheduler, `cs2bot up`) · cli · executor     │
            │ api + dashboard (localhost) · deploy (owner's Windows PC)            │
            └──────────────────────────────────────────────────────────────────────┘
```

## Foundation (shared; do not modify without coordination)

| Path | What it is |
|---|---|
| `src/cs2bot/config.py` | All settings (`get_settings()`), env prefix `CS2BOT_`, nested `__` |
| `src/cs2bot/domain/` | Pydantic contracts: enums, markets (`MarketSpec`), records, predictions (`MatchPrediction`), odds, betting, context, match |
| `src/cs2bot/db/models.py` | ORM schema (33 tables; migrations `0001` initial schema, `0002` one stake / payout / refund ledger entry per bet). `db/session.py`, `db/repo.py` (entity resolution), `db/state.py` (kill switch / structured pause reasons / overrides), `db/jobs.py` (job tracking) |
| `src/cs2bot/utils/` | `time` (UTC/Warsaw), `names` (team/map normalisation), `money` (Decimal), `http` (rate limit, retries), `logging` |
| `src/cs2bot/testing/synthetic.py` | Synthetic universe with ground truth (records, DB population, synthetic odds) |
| `tests/conftest.py` | Fixtures: `session`, `session_factory`, `engine` (SQLite), `pg_session`, `settings`, `synthetic_universe` |

Non-negotiable conventions:

* **UTC everywhere** (`utils.time.utcnow()`); DB columns reject naive datetimes. Local (Warsaw) time only for display and loss-limit day boundaries.
* **Decimal for money and odds** (`utils.money.D`); floats only for probabilities.
* **Orientation:** `team1`/`team2` always mean `matches.team1_id`/`team2_id`. Bookmaker events may be swapped (`bookmaker_events.teams_swapped`).
* **Map names** normalised with `utils.names.normalize_map_name` ("mirage", "dust2", ...).
* **No network in tests.** Use saved fixtures, `respx`, fakes. Browser tests run against local HTML and carry `@pytest.mark.browser`.
* **Never log secrets.** Settings secrets are `SecretStr`.
* Anything that can move money checks `db.state.is_trading_halted()` immediately before acting.

## Package ownership & public interfaces

### P1 · `cs2bot.ingest.pandascore` + `cs2bot.ingest.scope`
* `PandaScoreClient(settings: PandaScoreSettings)`: paginated, rate-limited REST client. `upcoming_matches()`, `running_matches()`, `past_matches(since: datetime)`, `match(id)`, `team(id)`, `tournament(id)`, all returning typed pydantic models of the API payload.
* `team(id)` / `tournament(id)` use the generic `/teams/{id}` / `/tournaments/{id}` endpoints.
* `sync_upcoming(session, client, settings, *, options=None, now=None) -> SyncReport`, `sync_past(session, client, settings, since, until=None, *, options=None, now=None) -> SyncReport`, `sync_match(session, client, settings, pandascore_match_id)`, `sync_modified(...)`; knobs in `PandaScoreSyncOptions` (`commit_every` for the backfill). Upsert tournaments/teams/players/matches/lineups/maps via `db.repo`, set `Match.in_scope` with `scope.is_in_scope(...)`.
* `scope.is_in_scope(tier: Tier, team1_rank: int | None, team2_rank: int | None, settings: ScopeSettings) -> bool`, `scope.refresh_match_scope(session, match, settings)`, `scope.refresh_upcoming_scope(session, settings) -> int` (run after a ranking sync).

### P2 · `cs2bot.ingest.hltv`
* `HLTVFetcher(settings: HLTVSettings)`: `get(path) -> str` HTML via Playwright (default) or httpx, disk cache with TTL, polite jittered rate limit, optional proxy. Raises `HLTVBlockedError` on challenge pages instead of retrying aggressively. No challenge-solving or fingerprint-spoofing tooling.
* Pure parsers (HTML -> pydantic): `parse_results_page`, `parse_matches_page` (upcoming), `parse_match_page` (teams, lineups, format, event, time, vetoes, maps + scores + mapstats ids, demo link, status), `parse_mapstats_page` (player stat lines incl. side splits, round history), `parse_ranking_page`, `parse_news_list`, `parse_news_article`.
* Sync into DB: `sync_results(session, fetcher, since) `, `sync_upcoming(...)`, `sync_match(session, fetcher, hltv_match_id)`, `sync_mapstats(...)`, `sync_rankings(...)`, `sync_news(...)`. Uses `db.repo.resolve_team/resolve_player/resolve_tournament`.

### P3 · `cs2bot.ingest.demos`
* `download_demo(url, dest_dir, *, downloader=None, timeout_s=...) -> Path` (`BrowserDownloader.from_settings(settings)` reuses the HLTV browser profile), `extract_archive(path, dest) -> list[Path]` (.rar/.zip/.dem/.zst), `find_rar_tools()`.
* `parse_demo(dem_path, settings: DemoSettings) -> ParsedDemo` using `demoparser2` (rounds, kills, player-round stats, utility, economy/buy types, position samples, trade detection).
* `persist_parsed_demo(session, map_played_id, parsed, steamid_to_player_id)`; `process_pending_demos(session_factory, settings, *, downloader=None, limit=5) -> ProcessReport`.
* `rebuild_position_aggregates(session, *, window_days=120, as_of=None)`, `rebuild_duel_aggregates(session, *, window_days=120, as_of=None)` (keyword arguments).

### P4 · `cs2bot.odds`
* `OddsSource` protocol: `fetch_cs2_events() -> list[CollectedEvent]`. `BetclicBrowserSource(settings)` (alias `BetclicSource`): browser-based (loads the Betclic CS2 pages, captures the site's own JSON responses) with a tolerant parser; raw payloads saved for parser fitting. `BetclicApiSource(settings)` is used instead when `betclic.offering_api_base` is set. `collect_once(session, source, mapper, *, settings, now=None)`, `remap_event(session, event, mapper, *, settings)` (the API's `event_remapper`), `list_unmapped(session)`; the mapping functions take the keyword-only arguments documented in docs/dev/reports/odds.md.
* `market_mapping.MarketMapper.from_yaml(path)` + `map(market_raw, selection_raw, event: CollectedEvent, swapped: bool) -> MappedSelection | None` (Polish market names, rules in `config/betclic_markets.yaml`).
* `event_matching.link_event(session, event_row) -> LinkResult` (teams + start time + competition, decides `teams_swapped`, confidence, `LinkStatus`; the collector flips `teams_swapped` with the names when Betclic lists a linked event's teams the other way round, and sends a CONFIRMED link whose names changed beyond recognition back to `PENDING_REVIEW`).
* `collector.collect_once(session, source, mapper) -> CollectReport` (upsert events/markets/selections; write `odds_snapshots` only on change).
* `quotes.latest_quotes(session, match_id, max_age_s) -> list[OddsQuote]`, `quotes.closing_odds(session, selection_id, kickoff) -> Decimal | None`.

### P5 · `cs2bot.models.ratings`, `cs2bot.models.veto`
* `MapRatingModel(config)`: Glicko-2 style per team **and per map** with hierarchical shrinkage to the team's overall rating, time decay, roster-continuity decay, LAN effect. `fit(records: Iterable[MapResultRecord])`, `update(record)`, `predict(team1_id, team2_id, map_name, as_of, *, is_lan=False) -> MapWinEstimate`, `rating(team_id, map_name | None, as_of)`, `save/load`.
* `VetoModel`: per-team ban/pick propensities with recency weighting. `fit(records: Iterable[VetoRecord], *, map_advantage=None)`, `predict_sequences(team1_id, team2_id, fmt, map_pool, *, first_veto=TeamSlot.TEAM1, known_steps=(), top_k: int | None = 50, as_of=None, map_advantage=None, template=None, preserve_marginals=True) -> list[MapSequenceProbability]` (deterministic once the veto is known).
* `MapRatingModel.fit(records, record_history=True)` + `WalkForwardRatings` are the leakage-free providers for historical rows; `ratings.tune(records, grid, *, holdout_fraction=0.25)` backs `cs2bot models tune`.

### P6 · `cs2bot.models.round_model`, `map_sim`, `player_props`, `match_engine`
* `EconomyParams` (CS2 constants: MR12, MR3 OT, loss bonus, rewards, OT money; all configurable).
* `RoundModel`: P(round win | side, buy matchup, strength diff, map) - `fit(rounds: Iterable[RoundRecord])` with sensible priors when data is thin.
* `MapSimulator.simulate(inp: MapSimInput, n_sims, rng) -> MapSimOutput` (round-by-round with economy) and `calibrate_strength(target_p, inp) -> float` so the simulated map-win probability matches the ensemble's estimate.
* `PlayerKillModel.fit(records: Iterable[PlayerMapRecord])`, `project(player_id, map_name, opponent_team_id, rounds_pmf, ...) -> PlayerProjection`; top-fragger probabilities by simulation.
* `build_match_prediction(...) -> MatchPrediction` combining veto distribution × per-map estimates × simulations, with `low/high` ±1σ scenarios.

### P7 · `cs2bot.datasets`, `cs2bot.features`, `cs2bot.models.ensemble`, `cs2bot.models.calibration`, `cs2bot.backtest`
* `datasets`: DB -> `MapResultRecord` / `VetoRecord` / `RoundRecord` / `PlayerMapRecord` (time-filtered, in-scope only); `load_match_context(session, match_id, *, settings, now, include_context_flags=True) -> MatchContext`, `data_completeness(session, ctx, *, now) -> float`, `load_match_actuals(session, match_id) -> MatchActuals | None`.
* `features`: leakage-safe features per (match, map) as of a timestamp (`FeatureStore.build/from_session`, `build_training_rows`, `features_for_match_map`, `RatingProvider`); player roles from demo data (`infer_roles`, `update_player_roles(session)`); meta (active pool detection, patch timeline, change-point detection on form).
* `MapWinEnsemble`: LightGBM + rating model stacking, `fit(map_results, *, store, rating_provider, as_of=None) -> FitReport`, `predict(...)` / `predict_for_sequences(ctx, sequences, *, as_of, store, rating_provider) -> dict[str, MapWinEstimate]` with sigma. A prediction from a `MatchContext` is orientation-free: it is averaged (logit space) with the prediction for `mirror_context(ctx)` (teams, lineups, veto slots, start sides swapped; `EnsembleConfig.symmetric`, default on), half the gap between the two is added to `sigma_model`.
* `calibration`: isotonic / Platt on time-ordered holdout; reliability metrics.
* `backtest`: walk-forward runner over historical odds (`run_backtest`, `run_db_backtest(session, *, settings, since, until, ...)`, `run_synthetic_backtest`, `PipelineTrainer`, `OddsHistory`, `LeakageError`); Brier, log-loss, ECE, ROI, CLV, drawdown, per-market breakdown. `PipelinePredictor` predicts exactly like `services.prediction` (same models, veto, map estimates, economy from `settings.models`); DB contexts carry no starting sides (they exist only for played maps); `cs2bot backtest` uses the ensemble and the tuned rating configuration by default, like the live bot.

### P8 · `cs2bot.engine`, `cs2bot.llm`
* `engine.pricing.price_outcome(spec, selection, prediction: MatchPrediction, *, sigma_floor) -> OutcomeProbability` for every `MarketType` (void/push aware).
* `engine.ev`: `effective_odds`, `expected_value`, `kelly_fraction` (Polish 12% stake tax, 10% tax on wins above 2280 PLN, refunds).
* `engine.decision.evaluate(..., trusted_markets=None) -> BetDecision` (all PLAN.md section 6 checks, every rejection reason recorded; `min_ev <= -1` switches the EV rule off - the default since 2026-11-24, "confidence only, odds ignored"; `require_trusted_calibration` + the calibration layer's trusted market set adds `untrusted_calibration`; the trusted set comes from `services.betting.trusted_markets(bundle, settings)` reading the `ConfidenceGuard` file, and a required-but-unknown verdict trusts nothing; `require_profitable_win` (default on) adds `unprofitable_win` for odds below `engine.ev.min_profitable_odds` - 1.137 with the 12 % stake tax, where a win pays back less than the stake), `decision.min_acceptable_odds(...)` (value for `BetOrder.min_acceptable_odds`), `decision.load_match_decision_info(...)`.
* `engine.bankroll`: `compute_state(session, settings, now) -> BankrollState`, `size_stake(...)` / `size_decision(...)` (base stake by `bankroll.staking_mode`: `confidence_tiers` - default, the highest `confidence_tiers` entry met by p(win | settled), odds ignored - or fractional `kelly`; then every cap: max stake, match exposure, daily / weekly loss guards, bets/day, balance, winnings-tax limit, stake step, Betclic minimum), `confidence_tier(p, tiers)`, `check_loss_limits(...)` (pauses);
* `engine.confidence`: `guard_pairs(pairs, adjuster)` / `guarded_outcome(...)` apply a `ProbabilityAdjuster` (the high-confidence guard's `adjust`) to every outcome before pricing - never raises a probability, a failing adjuster blocks the outcome (NaN); `engine.ledger`: stake/payout/refund/reconciliation entries and balance, `apply_placement(session, bet, result, settings)` (maps a `PlacementResult` onto the bet + ledger), `settle_bet(...)`, `reconcile(...)` (a mismatch beyond the tolerance pauses trading; a shortfall explained by unconfirmed placements is deferred), `pause_trading(...)`.
* `engine.settlement`: `resolve_outcome(spec, selection, actuals, ...) -> SettlementOutcome`, `resolve_bet(bet, actuals, ...) -> Resolution` (UNKNOWN, never a guess).
* `llm.context.ContextAnalyzer`: Claude Haiku news triage, Claude Opus match context flags (structured output) and bet rationale; `combine_flags(flags, settings.llm, *, now) -> (logit_shift, veto)` with hard bounds; `refresh_match_context(session, analyzer, ctx, *, now)` and `triage_pending_news(session, analyzer)` are what the services call. Stored flags keep `logit_shift` in the perspective of the team1 at write time: `load_active_flags` ignores the shift of a flag about a team no longer in the match, and the PandaScore / HLTV syncs call `deactivate_flags_after_team_change(session, match_id)` when they replace a participant (plain flags off, vetoes kept without shift, manual flags untouched).

### P9 · `cs2bot.executor`
* `BetExecutor` protocol: `place(order: BetOrder) -> PlacementResult`, `get_balance()`, `health_check()`, `close()`; `AccountExecutor` adds `is_logged_in()`, `open_login_window(timeout_s)`, `fetch_bet_history(since)`. `create_betclic_executor(settings, session_factory) -> ThreadBoundExecutor` (all calls on one worker thread, browser started lazily, halt check reads the DB). A bet is real only when BOTH `order.dry_run` and `settings.executor.dry_run` are false. The executor keeps a placement journal (`cs2bot-placements.jsonl` in the browser-profile directory) and never clicks a key twice. `BetOrder.event_starts_at` makes it refuse orders whose match has started.
* `BetclicExecutor`: Playwright, **visible** browser with a dedicated persistent profile on the owner's PC (the owner logs in by hand once, incl. 2FA; when the session expires the bot pauses and the dashboard asks for a new login), selectors from `config/betclic_selectors.yaml`, odds re-check, stake entry, confirmation, bet-id + screenshot capture, `DRY_RUN`. `open_login_window()` for the manual login; `auto_login(timeout_s=None) -> LoginAttempt` (cloud autonomy: fills AND submits the form with the saved credentials, stops at an SMS / 2FA step, detects wrong password / locked account; `AutoLoginExecutor` protocol, also on `ThreadBoundExecutor` and `FakeExecutor`). On Linux without `DISPLAY` a visible browser falls back to headless (`executor.headless_without_display`); the VPS runs it on Xvfb. `FakeExecutor` for tests.

### P10 · `cs2bot.api` and `dashboard/`
* `create_app(settings=None, session_factory=None, *, login_launcher=None, executor_health_provider=None, event_remapper=None, scheduler_status_provider=None, options=None) -> FastAPI`, `make_server(...)` (uvicorn, runs in a thread), `run(...)`. Binds to `127.0.0.1` (local only); a link review that changes match/orientation clears the event's mappings and sets `extra.remap_required` (the collector or the injected `event_remapper` re-maps). Executor health comes from the provider, else `system_state["executor_health"]` (written by `services.health`). optional session-cookie auth (required only if bound to a non-loopback address); serves the built dashboard from `dashboard/dist`; endpoints for matches, analyses, markets/predictions vs odds, decisions, bets, bankroll, calibration, health/jobs, kill switch, pause/resume, config overrides, link review queue.
* `dashboard/`: React + Vite + TypeScript + Recharts SPA consuming the API.

### P11 · local install on the owner's Windows PC (`deploy/windows/*.ps1` + `.cmd`, `scripts/dev.sh`, `scripts/test.sh`, `docker-compose.yml`, `.github/workflows/`, `docs/WINDOWS_SETUP.md`, `docs/OPERATIONS.md`, `CLAUDE.md`)
Docker Desktop runs **only PostgreSQL**; the Python app runs natively via `uv` (so Playwright can show a real
browser for the Betclic login). PowerShell helpers to install, start (`cs2bot up`), back up the database and
register a Task Scheduler entry at login; power settings guidance (PC must be awake before matches); CI
(ruff, mypy, pytest on Linux + Windows runners).

Contract with the scripts: `cs2bot up` runs in the foreground, logs to stdout/stderr, stops cleanly on Ctrl+C
within 90 s (exit 0 = normal stop, non-zero = crash/could not start); `cs2bot executor login` exits 0 only after
a completed login; `cs2bot doctor` exits non-zero on problems; `cs2bot db upgrade` is idempotent.

### P11b · cloud deployment on a Linux VPS (`Dockerfile`, `docker-compose.cloud.yml`, `deploy/linux/`, `docs/VPS_SETUP.md`)
One VPS in Poland runs three containers (PLAN.md 0b): `postgres` (internal network only), `app` (the
image's entrypoint starts a supervised Xvfb on `:99`, runs `cs2bot db upgrade`, then `cs2bot up` - scheduler,
visible Betclic executor on the virtual display, API on 0.0.0.0:8000 reachable only through Caddy; a first
training runs in the background so `/api/ping` answers at once) and `caddy` (HTTPS on 80/443, fixed
address on the `frontend` network so the app trusts only its `X-Forwarded-*`:
`CS2BOT_API__FORWARDED_ALLOW_IPS`). Docker restarts everything on failure and at boot; Docker's
healthcheck is `/api/ping`. Persistent: `data/` (browser profiles incl. the Betclic session and the
placement journal, models, the confidence guard), `logs/`, the PostgreSQL volume. Daily `backup.sh`
(pg_dump, retention, optional rclone), `restore.sh` (ends with a manual pause). The DEEP features
(`cs2bot.datasets.deep.DeepData`, `cs2bot.features.deep.DeepFeatureIndex`, `cs2bot.odds.movement`,
`cs2bot.odds.sharp`) and the high-confidence calibration (`cs2bot.models.confidence`) are documented in
docs/dev/reports/deep_data.md.

### P12 · `cs2bot.services`, `cs2bot.flows`, `cs2bot.cli` (integration, built last)
Wires everything: sync → train → predict → price → decide → stake → execute → settle, plus CLI commands.
* `services.registry`: `train_models(session, settings, *, as_of=None) -> TrainReport` (never sees data after `as_of`; artifacts in `settings.models.artifacts_dir/<version>/`, a `model_runs` row), `load_latest(settings) -> ModelBundle | None`, `ModelRegistry` (thread-safe swap after the nightly retrain; `retrain_unless_fresh(session_factory, max_age=...)` for the scheduled job). `build_feature_store(session, settings, *, until, ...)` is the ONE builder of the ensemble's feature store for training and live prediction (map results, player lines, rounds, vetoes, demo team stats and `DeepData` - match metadata, duels, clutches, coach changes, Betclic market history, optional sharp feed); the backtest's `HistoricalData` / `TrainingSet` carry the same inputs (`team_demo_stats`, `deep` cut at every cutoff).
* `services.backtesting`: `run_backtest_with_guard(session_factory, settings, *, since, until, sims, ensemble, retrain_every) -> GuardRefreshReport` (the `cs2bot backtest` command: DB backtest stored in `model_runs`, then the `ConfidenceGuard` written atomically to `models.confidence_guard_file`) and `refresh_confidence_guard(session_factory, settings, *, now=None, force=False)` (scheduler job `confidence_guard_refresh`: runs when the file is `models.guard_refresh_days` old and enough priced finished matches exist; attempts remembered in `system_state["confidence_guard"]`).
* `services.prediction`: `predict_match(session, match_id, bundle, settings, *, analyzer=None, now=None)`, `predict_upcoming(session_factory, settings, bundle, *, analyzer=None, now=None, force=False) -> PredictReport` (re-simulates only when veto, lineups, flags, completeness or model version changed). A match that can no longer be predicted (teams / lineups gone, an error) gets `matches.extra["prediction"]["stale"]` (`mark_prediction_stale`, `stale_reason`); the next valid prediction clears it.
* `services.betting`: `run_betting_cycle(session_factory, settings, executor, bundle, *, now=None, dry_run=None, should_stop=None) -> BettingReport` (bet row committed PENDING → halt re-read → PENDING→PLACING compare-and-swap committed → `executor.place`; crash-safe, idempotent; UNCERTAIN / closed executor / shutdown stop the rest of the cycle; a second open UNCERTAIN placement pauses trading; an unusable executor - FAILED with `executor.base.EXECUTOR_UNAVAILABLE_PREFIX` - stops the cycle and, with autonomy, pauses with `executor_unavailable`). Before pricing, the high-confidence guard (`decision.use_confidence_guard`; the guard file is read once per cycle, without one the no-history prior applies) lowers over-confident probabilities via `engine.confidence`; accepted outcomes of a match are sized most-confident first with confidence tiers (highest EV first with Kelly). Only in-scope matches whose stored prediction still fits the match row (`prediction_mismatch`: same teams, same format, not stale) are bet; each bet records `extra["teams_at_placement"]` and the local settlement refuses a bet whose match teams changed since.
* `services.settlement`: `settle_bets(session_factory, settings, executor=None, *, now=None) -> SettlementReport` (bet history first, then results; the bookmaker's outcome and plausible payout are the truth for money; closing odds/CLV; balance reconciliation, skipped while the history is unavailable).
* `services.ingestion` (job functions per source), `services.health` (executor health cache + `system_state`), `services.dbops` (Alembic in code), `services.doctor` (incl. Telegram, autonomy credentials, browser display).
* `services.autonomy` (cloud autonomy): `AutonomyService(session_factory, settings, executor=None, *, health_cache=None).run_once(now=None) -> AutonomyReport` lifts every automatic pause whose cause is resolved (loss limits at the next Warsaw day / week, unconfirmed bets once settled, login after an auto-login or a logged-in check, balance mismatch once booked, selector / executor errors after a passing health check), with a circuit breaker (`circuit_open`); never the kill switch or a `manual` pause. `pause_overview(session, settings)` is the read model (reasons, ETA, circuit breaker) for the API / CLI / Telegram. Pause reasons are `domain.control.PauseCode`s stored by `db.state.add_pause_reason` / `resolve_pause_reasons`.
* `services.telegram`: `TelegramClient` (Bot API over httpx; rate-limited, retried, `send` never raises, token redacted), `TelegramNotifier` (new bets, settlements, pause / resume / kill-switch / login events, failing jobs, read from the database with a cursor; `daily_report`), `TelegramCommands` (`/status /balance /bets /pause /resume /stop /go /help` from the configured chat only).
* `flows.scheduler`: built-in job scheduler (interval + daily jobs, catch-up of runs missed while the PC was off,
  based on `job_runs`). `flows.jobs.build_jobs(Runtime) -> list[Job]` (lanes `fast` incl. `autonomy_check`, `sync`,
  `hltv`, `train`, `notify` (`telegram_notify`, `telegram_daily_report`), `telegram` (`telegram_commands`);
  `Runtime.stop_event` is set at shutdown so a running betting cycle places nothing more; `Runtime.telegram`).
  `flows.supervisor.run_up(settings) -> int` = `cs2bot up`: starts the API/dashboard on `127.0.0.1`, the executor
  and the scheduler in one command.

## CLI (implemented by P12; names are fixed so docs and scripts can reference them)

| Command | Purpose |
|---|---|
| `cs2bot up` | Start the API + dashboard on 127.0.0.1 and the built-in scheduler (one command) |
| `cs2bot doctor` | Check environment: DB reachable, migrations applied, Chromium present, API keys set, disk space |
| `cs2bot db upgrade` / `db status` / `db seed-synthetic` | Migrations / demo data |
| `cs2bot ingest upcoming` / `results` / `backfill --since 2023-09-27` / `rankings` / `news` / `match <hltv_id>` / `scope` | Data ingestion (`--source all|pandascore|hltv`) |
| `cs2bot ingest hltv-browser` | Open HLTV in a visible window with the bot's browser profile (pass a challenge by hand) |
| `cs2bot demos process --limit N` / `demos aggregates` | Download + parse pending demos / rebuild aggregates and roles |
| `cs2bot odds collect [--once]` / `odds unmapped` | Betclic odds collection / list unmapped market names |
| `cs2bot models train` / `models tune` / `models list` | Fit ratings, veto, round, kill, ensemble models |
| `cs2bot predict upcoming` / `predict match <id>` | Produce MatchPredictions + priced outcomes |
| `cs2bot bet run [--dry-run] [--no-executor]` | Decide + stake + execute for upcoming matches |
| `cs2bot settle [--no-executor]` | Settle finished bets, closing odds, CLV, ledger |
| `cs2bot backtest --from YYYY-MM-DD --to YYYY-MM-DD` | Walk-forward backtest |
| `cs2bot executor login` / `executor health` / `executor balance` | Open the Betclic login window / checks |
| `cs2bot control kill-switch on|off` / `control pause` / `control resume` / `control status` | Safety controls (status lists every pause reason with its auto-resume ETA and the circuit breaker) |
| `cs2bot telegram test` | Send a Telegram test message |

## Data flow for one upcoming match

1. `ingest.*` keeps matches, lineups, vetoes, results, demos and news fresh.
2. `odds.collector` stores Betclic prices and links the event to the match.
3. `services.prediction` builds a `MatchContext`, gets per-map estimates (`models.ensemble` / `models.ratings`), the veto distribution (`models.veto`), context flags (`llm.context`), and calls `models.match_engine.build_match_prediction`.
4. `engine.pricing` prices every mapped Betclic outcome → `engine.decision` → `engine.bankroll.size_stake`.
5. Accepted bets → `bets` row (PENDING, idempotency key) → `executor.place` → ledger entries.
6. After the match: `services.settlement` settles bets, records closing odds / CLV, updates the ledger.
