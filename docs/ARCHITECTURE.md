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
| `src/cs2bot/db/models.py` | ORM schema (33 tables). `db/session.py`, `db/repo.py` (entity resolution), `db/state.py` (kill switch / pause / overrides), `db/jobs.py` (job tracking) |
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
* `sync_upcoming(session, client, settings) -> SyncReport`, `sync_past(session, client, settings, since) -> SyncReport`: upsert tournaments/teams/players/matches/lineups/maps via `db.repo`, set `Match.in_scope` with `scope.is_in_scope(...)`.
* `scope.is_in_scope(tier: Tier, team1_rank: int | None, team2_rank: int | None, settings: ScopeSettings) -> bool`.

### P2 · `cs2bot.ingest.hltv`
* `HLTVFetcher(settings: HLTVSettings)`: `get(path) -> str` HTML via Playwright (default) or httpx, disk cache with TTL, polite jittered rate limit, optional proxy. Raises `HLTVBlockedError` on challenge pages instead of retrying aggressively. No challenge-solving or fingerprint-spoofing tooling.
* Pure parsers (HTML -> pydantic): `parse_results_page`, `parse_matches_page` (upcoming), `parse_match_page` (teams, lineups, format, event, time, vetoes, maps + scores + mapstats ids, demo link, status), `parse_mapstats_page` (player stat lines incl. side splits, round history), `parse_ranking_page`, `parse_news_list`, `parse_news_article`.
* Sync into DB: `sync_results(session, fetcher, since) `, `sync_upcoming(...)`, `sync_match(session, fetcher, hltv_match_id)`, `sync_mapstats(...)`, `sync_rankings(...)`, `sync_news(...)`. Uses `db.repo.resolve_team/resolve_player/resolve_tournament`.

### P3 · `cs2bot.ingest.demos`
* `download_demo(fetcher_or_client, url, dest_dir) -> Path`, `extract_archive(path, dest) -> list[Path]` (.rar/.zip/.dem/.zst).
* `parse_demo(dem_path, settings: DemoSettings) -> ParsedDemo` using `demoparser2` (rounds, kills, player-round stats, utility, economy/buy types, position samples, trade detection).
* `persist_parsed_demo(session, map_played_id, parsed, steamid_to_player_id)`; `process_pending_demos(session_factory, ..., limit)`.
* `aggregates.rebuild_position_aggregates(session, window_days)`, `aggregates.rebuild_duel_aggregates(session, window_days)`.

### P4 · `cs2bot.odds`
* `OddsSource` protocol: `fetch_cs2_events() -> list[CollectedEvent]`. `BetclicSource(settings)`: browser-based (loads the Betclic CS2 pages, captures the site's own JSON responses) with a tolerant parser; raw payloads saved for parser fitting.
* `market_mapping.MarketMapper.from_yaml(path)` + `map(market_raw, selection_raw, event: CollectedEvent, swapped: bool) -> MappedSelection | None` (Polish market names, rules in `config/betclic_markets.yaml`).
* `event_matching.link_event(session, event_row) -> LinkResult` (teams + start time + competition, decides `teams_swapped`, confidence, `LinkStatus`).
* `collector.collect_once(session, source, mapper) -> CollectReport` (upsert events/markets/selections; write `odds_snapshots` only on change).
* `quotes.latest_quotes(session, match_id, max_age_s) -> list[OddsQuote]`, `quotes.closing_odds(session, selection_id, kickoff) -> Decimal | None`.

### P5 · `cs2bot.models.ratings`, `cs2bot.models.veto`
* `MapRatingModel(config)`: Glicko-2 style per team **and per map** with hierarchical shrinkage to the team's overall rating, time decay, roster-continuity decay, LAN effect. `fit(records: Iterable[MapResultRecord])`, `update(record)`, `predict(team1_id, team2_id, map_name, as_of, *, is_lan=False) -> MapWinEstimate`, `rating(team_id, map_name | None, as_of)`, `save/load`.
* `VetoModel`: per-team ban/pick propensities with recency weighting. `fit(records: Iterable[VetoRecord])`, `predict_sequences(team1_id, team2_id, fmt, map_pool, *, first_veto=TeamSlot.TEAM1, known_steps=(), top_k=50) -> list[MapSequenceProbability]` (deterministic once the veto is known).

### P6 · `cs2bot.models.round_model`, `map_sim`, `player_props`, `match_engine`
* `EconomyParams` (CS2 constants: MR12, MR3 OT, loss bonus, rewards, OT money; all configurable).
* `RoundModel`: P(round win | side, buy matchup, strength diff, map) - `fit(rounds: Iterable[RoundRecord])` with sensible priors when data is thin.
* `MapSimulator.simulate(inp: MapSimInput, n_sims, rng) -> MapSimOutput` (round-by-round with economy) and `calibrate_strength(target_p, inp) -> float` so the simulated map-win probability matches the ensemble's estimate.
* `PlayerKillModel.fit(records: Iterable[PlayerMapRecord])`, `project(player_id, map_name, opponent_team_id, rounds_pmf, ...) -> PlayerProjection`; top-fragger probabilities by simulation.
* `build_match_prediction(...) -> MatchPrediction` combining veto distribution × per-map estimates × simulations, with `low/high` ±1σ scenarios.

### P7 · `cs2bot.datasets`, `cs2bot.features`, `cs2bot.models.ensemble`, `cs2bot.models.calibration`, `cs2bot.backtest`
* `datasets`: DB -> `MapResultRecord` / `VetoRecord` / `RoundRecord` / `PlayerMapRecord` (time-filtered, in-scope only).
* `features`: leakage-safe features per (match, map) as of a timestamp; player roles from demo data; meta (active pool detection, patch timeline, change-point detection on form).
* `MapWinEnsemble`: LightGBM + rating model stacking, `fit(...)`, `predict(...) -> MapWinEstimate` with sigma.
* `calibration`: isotonic / Platt on time-ordered holdout; reliability metrics.
* `backtest`: walk-forward runner over historical odds; Brier, log-loss, ECE, ROI, CLV, drawdown, per-market breakdown.

### P8 · `cs2bot.engine`, `cs2bot.llm`
* `engine.pricing.price_outcome(spec, selection, prediction: MatchPrediction, *, sigma_floor) -> OutcomeProbability` for every `MarketType` (void/push aware).
* `engine.ev`: `effective_odds`, `expected_value`, `kelly_fraction` (Polish 12% stake tax, 10% tax on wins above 2280 PLN, refunds).
* `engine.decision.evaluate(...) -> BetDecision` (all PLAN.md section 6 checks, every rejection reason recorded).
* `engine.bankroll`: `compute_state(session, settings, now) -> BankrollState`, `size_stake(...)` (1/4 Kelly, caps, exposure, loss limits, bets/day); `engine.ledger`: stake/payout/refund/reconciliation entries and balance.
* `llm.context.ContextAnalyzer`: Claude Haiku news triage, Claude Opus match context flags (structured output) and bet rationale; `combine_flags(flags, settings) -> (logit_shift, veto)` with hard bounds.

### P9 · `cs2bot.executor`
* `BetExecutor` protocol: `place(order: BetOrder) -> PlacementResult`, `get_balance()`, `health_check()`.
* `BetclicExecutor`: Playwright, **visible** browser with a dedicated persistent profile on the owner's PC (the owner logs in by hand once, incl. 2FA; when the session expires the bot pauses and the dashboard asks for a new login), selectors from `config/betclic_selectors.yaml`, odds re-check, stake entry, confirmation, bet-id + screenshot capture, `DRY_RUN`. `open_login_window()` for the manual login. `FakeExecutor` for tests.

### P10 · `cs2bot.api` and `dashboard/`
* `create_app(settings, session_factory) -> FastAPI`: binds to `127.0.0.1` (local only); optional session-cookie auth (required only if bound to a non-loopback address); serves the built dashboard from `dashboard/dist`; endpoints for matches, analyses, markets/predictions vs odds, decisions, bets, bankroll, calibration, health/jobs, kill switch, pause/resume, config overrides, link review queue.
* `dashboard/`: React + Vite + TypeScript + Recharts SPA consuming the API.

### P11 · local install on the owner's Windows PC (`deploy/`, `docker-compose.yml`, `.github/workflows/`, `docs/WINDOWS_SETUP.md`)
Docker Desktop runs **only PostgreSQL**; the Python app runs natively via `uv` (so Playwright can show a real
browser for the Betclic login). PowerShell helpers to install, start (`cs2bot up`), back up the database and
register a Task Scheduler entry at login; power settings guidance (PC must be awake before matches); CI
(ruff, mypy, pytest on Linux + Windows runners).

### P12 · `cs2bot.services`, `cs2bot.flows`, `cs2bot.cli` (integration, built last)
Wires everything: sync → train → predict → price → decide → stake → execute → settle, plus CLI commands.
`flows.scheduler`: built-in job scheduler (interval + daily jobs, catch-up of runs missed while the PC was off,
based on `job_runs`). `cs2bot up`: starts the API/dashboard on `127.0.0.1` and the scheduler in one command.

## Data flow for one upcoming match

1. `ingest.*` keeps matches, lineups, vetoes, results, demos and news fresh.
2. `odds.collector` stores Betclic prices and links the event to the match.
3. `services.prediction` builds a `MatchContext`, gets per-map estimates (`models.ensemble` / `models.ratings`), the veto distribution (`models.veto`), context flags (`llm.context`), and calls `models.match_engine.build_match_prediction`.
4. `engine.pricing` prices every mapped Betclic outcome → `engine.decision` → `engine.bankroll.size_stake`.
5. Accepted bets → `bets` row (PENDING, idempotency key) → `executor.place` → ledger entries.
6. After the match: `services.settlement` settles bets, records closing odds / CLV, updates the ledger.
