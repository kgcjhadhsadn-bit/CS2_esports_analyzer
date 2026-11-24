# integration: implementation report

**Tests:** `.venv/bin/python -m pytest tests/services -q -n 2` - 40 passed, 0 failed (about 2 min); whole suite `.venv/bin/python -m pytest tests -q -n 2` - 2955 passed, 20 skipped (browser/PostgreSQL), 0 failed (about 17 min); `ruff check src tests`, `ruff format --check src tests` and `mypy` (190 files) clean.

## Summary

P12 wires every package into one running bot: ingestion jobs -> model training -> prediction -> pricing / decision / staking -> placement -> settlement, the built-in scheduler, `cs2bot up` (scheduler + executor + local dashboard in one process) and the full CLI from docs/ARCHITECTURE.md.

An earlier engineer had written `services/registry.py`, `prediction.py`, `betting.py`, `settlement.py`, `health.py` and the end-to-end tests. I verified them by running them and kept them, with these fixes:

- **Dry-run caps across cycles (bug).** `engine.bankroll.compute_state` ignores dry-run bets, so a second dry-run cycle saw an empty per-match exposure and backed the next outcome of a match whose paper exposure was already at the cap. New `services.betting.with_open_paper_bets(session, state)` reserves the open paper bets (unsettled, match not finished) in the state used for sizing in dry-run mode. The existing e2e test caught it.
- **Kill switch mid-cycle.** The e2e test expected a CANCELLED row when the kill switch is pressed after the first placement. The cycle in fact stops earlier and more safely: the fresh `compute_state` after each placement carries the kill switch, so the remaining outcomes are rejected at sizing ("halted") and no row is created. The test now asserts that. A new test covers the other path: the kill switch pressed between the PENDING row and `executor.place` cancels the row and sends nothing.
- Settlement / betting: lint fixes only.

New code:

- `services/ingestion.py`: one job function per source (PandaScore upcoming/results/match, HLTV upcoming/results/match/rankings/news, news triage, demos + aggregates + roles, scope refresh, odds collection, resumable backfill), `report_to_tracker` (report -> `job_runs`: PARTIAL when not ok, FAILED when not ok and nothing was done, so the scheduler backs off, e.g. while HLTV blocks us).
- `flows/jobs.py`: `Runtime` + `build_jobs`. Lanes: `fast` (odds_collect -> predict_upcoming -> bet_run -> settle_bets, executor_health), `sync` (PandaScore, news triage, scope), `hltv` (everything using the HLTV browser profile, so Playwright and the profile stay in one thread), `train` (nightly retrain as `Daily` parsed from `scheduler.retrain_cron`).
- `flows/supervisor.py`: `run_up` (checks, models, executor, analyzer, scheduler, API, clean bounded shutdown on Ctrl+C / SIGTERM / SIGBREAK).
- `services/dbops.py` (Alembic configured in code, migration status), `services/doctor.py` (all doctor checks).
- `cli.py`: every documented command plus `db status`, `ingest scope`, `demos aggregates`, `models list`, `control status`. Expected errors print one line and exit 1.

Tests added: `tests/services/test_services_jobs.py` (job wiring, fast-lane pass against the synthetic world, failure isolation, report mapping, retrain swap), `test_services_cli.py` (CliRunner smoke over every group: db, control, missing token/model errors, seed -> train -> predict -> bet -> settle -> tune -> backtest, doctor), `test_services_up.py` (`run_up` start/stop with FakeExecutor and a real uvicorn server on a free port; refusal when migrations are behind), plus the new kill-switch test in `test_services_e2e.py`.

Other changes outside the integration paths (applied foundation change requests):

- `config.py`: `DatabaseSettings.url` default `postgresql+psycopg://cs2bot:change-me@127.0.0.1:5432/cs2bot` (matches docker-compose / .env.example; ops request); `ExecutorSettings` gained `max_odds_rise_factor`, `potential_win_min_ratio`, `potential_win_max_ratio`, `journal_retention_days` (executor request; added to `executor.tuning.SETTINGS_FIELDS`; the executor test that simulated the future field was adjusted). Three executor tests built `BetOrder` subclasses with an `event_starts_at` keyword that `BetOrder` itself now has (added by the earlier integration step) and failed with "multiple values for keyword argument"; fixed; `scenario_n_simulations` comment aligned (sim request).
- `.gitignore`: `logs/`, `backups/`, `/models/` (ops request). A stray model bundle that had been committed at the repo root (`models/m20261124T020510/`, a test artifact) was deleted from the working tree.
- `.env.example`: the PostgreSQL container block and the `CS2BOT_HLTV__HEADLESS=false` hint (ops / hltv requests).
- `docs/ARCHITECTURE.md`: the signature/documentation requests of api, demos, engine, ensemble_backtest, executor, llm, odds, ops, pandascore, ratings_veto and datasets_features, the P12 section and the CLI table. `docs/dev/STATUS.md` updated.

## Public API

### `cs2bot.services.registry`
```python
train_models(session, settings, *, as_of: datetime | None = None, use_ensemble: bool | None = None,
             ensemble_config=None, save=True, record_run=True) -> TrainReport   # flushes; caller commits
load_latest(settings) -> ModelBundle | None          # newest bundle that loads; corrupt ones skipped
load_bundle(directory) -> ModelBundle ; save_bundle(bundle, root) -> Path ; list_bundles(settings) ; prune_bundles(settings, keep=5)
load_rating_config(settings) / save_rating_config(settings, config, report)  # `models tune` result used by the next training
class ModelRegistry(settings, bundle=None): get() / swap(bundle) / load_latest() /
      retrain(session_factory, *, as_of=None, use_ensemble=None, keep=5) -> TrainReport   # commits, swaps, prunes
class NotEnoughDataError(RuntimeError)
```
`ModelBundle` (version, trained_at, as_of, ratings, veto, round_model, kill_model, ensemble, path, metrics, model_run_id; `refresh(session, now=)` applies new results to the ratings; `feature_store(session, now=)`; `uses_ensemble`; `lock`).

### `cs2bot.services.prediction`
```python
predict_match(session, match_id, bundle, settings, *, analyzer=None, now=None, persist=True) -> MatchPrediction | None
predict_upcoming(session_factory, settings, bundle, *, analyzer=None, now=None, force=False,
                 max_age=timedelta(hours=6), match_ids=None) -> PredictReport
write_outcome_predictions(session, prediction, settings, *, analysis_id, model_run_id, now, replace) -> int
```
Writes `match_analyses` (payload + rationale, `is_latest`) and one `predictions` row per quoted outcome; skips matches without both teams, lineups, or that already started; re-simulates only when the fingerprint (model version, format, lineups, veto, sides, flags, completeness, LAN) changed or the prediction is older than `max_age`.

### `cs2bot.services.betting`
```python
run_betting_cycle(session_factory, settings, executor, bundle, *, now=None, dry_run=None, analyzer=None,
                  max_prediction_age=timedelta(hours=8), match_ids=None) -> BettingReport
with_open_paper_bets(session, state: BankrollState) -> BankrollState
idempotency_key(session, match_id, spec_key, selection_key, *, dry_run) -> str   # "cs2b-<mode>-<match>-<hash>-<attempt>"
enforce_one_selection_per_market(decisions, *, bet_specs) -> list[BetDecision]
cancel_stale_pending(session, *, now) -> int
```
`BettingReport`: counters (`decisions`, `accepted`, `bets_created`, `placed`, `dry_run_ok`, `failed`, `uncertain`, `cancelled`, `stale_pending_cancelled`), `halted_reason`, `paused_reason`, `loss_limit_hit`, `bets`, `notes`, `errors`, `.items`, `.as_dict()`. Real money moves only when neither `dry_run` nor `settings.executor.dry_run` is set (and the executor checks both again). LOGIN_REQUIRED / SELECTOR_ERROR pause trading; UNCERTAIN stays PLACING and is never retried. One cycle at a time per process.

### `cs2bot.services.settlement`
```python
settle_bets(session_factory, settings, executor=None, *, now=None, grace=timedelta(minutes=30),
            health_cache: HealthCache | None = None, check_health=True) -> SettlementReport
match_history_record(bet, records, *, claimed) -> tuple[BookmakerBetRecord | None, int]
```

### `cs2bot.services.health`
`HealthCache()` (`get()` is the API's `executor_health_provider`), `refresh_executor_health(session_factory, executor, cache=None) -> ExecutorHealth` (never raises; stores `system_state["executor_health"]`).

### `cs2bot.services.ingestion`
```python
pandascore_upcoming(session_factory, settings, *, client=None, now=None) -> SyncReport
pandascore_results(session_factory, settings, *, client=None, since=None, now=None) -> SyncReport   # default since = now - 2 days
pandascore_match(session_factory, settings, pandascore_match_id, *, client=None) -> SyncReport
hltv_upcoming / hltv_results(..., since=None) / hltv_rankings(..., ranking_date=None) / hltv_news(..., months=1)
      (session_factory, settings, *, fetcher=None, ...) -> SyncReport
hltv_match(session_factory, settings, hltv_match_id, *, fetcher=None) -> SyncReport
refresh_scope(session_factory, settings, *, now=None) -> int
triage_news(session_factory, analyzer, *, limit=200, now=None) -> TriageReport
process_demos(session_factory, settings, *, limit=5, downloader=None, parser_factory=None, now=None) -> DemoJobReport
rebuild_aggregates(session_factory) -> dict[str, int]
build_odds_source(settings) ; collect_odds(session_factory, settings, *, source=None, mapper=None) -> CollectReport
backfill(session_factory, settings, since, *, until=None, pandascore_client=None, hltv_fetcher=None,
         use_pandascore=None, use_hltv=True, resume=True, commit_every=20, max_pages=10_000) -> BackfillReport
report_to_tracker(tracker, report, *, fail_when_empty=True)
MissingCredentialError, JobFailedError, BACKFILL_STATE_KEY = "backfill"
```
Without an injected source, each call opens and closes its own client / fetcher / downloader. Commits on success (partial work of a blocked run included), rolls back on an exception.

### `cs2bot.services.dbops` / `cs2bot.services.doctor`
`alembic_config(url)`, `upgrade(url, revision="head")`, `migration_status(engine) -> MigrationStatus(reachable, current, heads, error; .at_head, .describe())`. `run_checks(settings, *, chromium_finder=None) -> list[Check(name, level: "ok"|"warn"|"fail", message)]`.

### `cs2bot.flows.jobs`
```python
@dataclass class Runtime(settings, session_factory, registry: ModelRegistry, executor=None, analyzer=None,
    health_cache=HealthCache(), odds_source_factory=None, hltv_fetcher_factory=None,
    pandascore_client_factory=None, demo_downloader_factory=None, mapper=None, hltv_enabled=True, demo_limit=5)
build_jobs(rt: Runtime) -> list[Job]
parse_daily_cron(expr: str, tz: str) -> Daily        # "M H * * *" only; else 05:00
FAST, SYNC, HLTV, TRAIN = "fast", "sync", "hltv", "train"
```
Jobs: `odds_collect`, `predict_upcoming`, `bet_run`, `settle_bets`, `executor_health` (fast); `ingest_upcoming_pandascore`, `ingest_results_pandascore` (need a token), `scope_refresh_upcoming`, `news_triage` (needs an enabled analyzer) (sync); `ingest_upcoming_hltv`, `ingest_results_hltv`, `ingest_rankings_hltv` (12 h), `ingest_news_hltv`, `demos_process` (hltv); `models_retrain` (train).

### `cs2bot.flows.supervisor`
```python
run_up(settings, *, session_factory=None, executor_factory=None, analyzer_factory=None, runtime_overrides=None,
       check_migrations=True, train_if_missing=True, start_api=True, stop_event=None, on_started=None,
       echo=print) -> int        # 0 normal stop, 1 crash, 2 not ready (DB / migrations / executor)
dashboard_url(settings, port=None) -> str ; UpHandles(runtime, scheduler, server, stop, url)
```
Wiring example (what `cs2bot up` does):
```python
from cs2bot.config import get_settings
from cs2bot.flows.supervisor import run_up
raise SystemExit(run_up(get_settings()))
```

### CLI (`cs2bot ...`)
`up [--no-api]`, `doctor`, `db upgrade|status|seed-synthetic`, `ingest upcoming|results [--since]|backfill --since [--until] [--no-resume]|rankings|news [--months]|match <hltv_id>|scope|hltv-browser [--url] [--wait-s]` (`--source all|pandascore|hltv` where it applies), `demos process [--limit]|aggregates`, `odds collect [--once] [--interval-s]|unmapped [--limit]`, `models train [--as-of] [--ensemble/--no-ensemble]|tune [--holdout] [--grid JSON]|list`, `predict upcoming [--force]|match <id>`, `bet run [--dry-run] [--no-executor]`, `settle [--no-executor]`, `backtest --from --to [--sims] [--ensemble] [--output]`, `executor login [--timeout-s]|health|balance`, `control kill-switch on|off [--reason]|pause [--reason]|resume|status`, `version`.

## Known limitations

- **Nothing ran against live services.** HLTV, Betclic and PandaScore are unreachable from this machine; jobs were exercised with fakes (StaticOddsSource, a blocked HLTV fetcher, FakeExecutor). The first real `cs2bot up` on the owner's PC is the first real run of the full chain.
- **SQLite + migrations:** migration 0001 uses PostgreSQL's `now()` as a server default, so a SQLite database created with `cs2bot db upgrade` cannot insert rows that rely on it (tests use `Base.metadata.create_all` + an Alembic stamp). Production uses PostgreSQL, where this works.
- The HLTV fetcher and the demo downloader are opened and closed per job (inside the `hltv` lane thread): a few seconds of browser start-up per job, in exchange for never sharing Playwright objects across threads.
- The scheduler's daily trigger supports only `M H * * *` for `retrain_cron`.
- `run_up` stops the scheduler with a bounded wait; a job still running after it (e.g. a long retrain or an LLM call) is abandoned when the process exits. Betting is crash-safe by design (rows are committed before every click and reconciled from the bet history).
- In dry-run mode the paper bets of a match count for the per-match cap and today's bet count, but paper losses never trigger the loss limits (by design: dry runs never touch real limits).
- `cs2bot bet run` / `settle` / `executor ...` must not run while `cs2bot up` runs (the Betclic profile lock refuses the second browser; the CLI prints that).
- The doctor's Chromium check starts the Playwright driver to find the bundled browser path (about a second).

## Foundation change requests

Applied: see the Summary (config defaults and executor knobs, .gitignore, .env.example, ARCHITECTURE/STATUS docs).

Not applied, with the reason:
- engine: partial unique index on `bankroll_ledger(bet_id, kind)` and dropping `Bet.dry_run`'s default; pandascore/hltv/demos: `Demo.map_number`, `steamid` columns, `matches.is_forfeit`: all need a migration 0002 and coordinated changes in the writers; the services always set `dry_run` explicitly and serialise betting cycles. Recommended for the final review together with a 0002 migration (which should also replace the PostgreSQL-only `now()` server defaults with `sa.func.now()`).
- datasets: `DEFAULT_ACTIVE_MAP_POOL` -> the latest pool of `config/patches.yaml`. Tried and reverted: the synthetic universe and tests of ratings_veto / features are pinned to the current default (three failures), and the Cache entry in patches.yaml is itself marked unverified. Production does not depend on it: `datasets.load_match_context` takes the pool from recent results / patches.yaml. Decide in the final review (and update the synthetic universe with it).
- engine: `BankrollSettings.stake_step` 0.10 -> 0.50 is a money decision for the owner (settable via `CS2BOT_BANKROLL__STAKE_STEP`).
- engine: `MatchActuals.player_teams`: not needed, `services.settlement` passes `player_teams` from the lineups.
- odds: `OddsQuote.market_page_name` / `market_group` and executor support for market tabs: a cross-package change (domain + executor + odds). Until then a parser-prefixed market name may not be found on the page, which ends as SELECTOR_ERROR (bot pauses, no money at risk). Should be done in the final review.
- odds/pandascore/llm/demos/ensemble: moving package knobs (`source:` section, sync options, LLMTuning, PipelineOptions, EnsembleConfig) into `config.py`: optional, defaults are safe; the YAML / dataclass defaults keep working.
- pandascore: stricter fuzzy matching in `db.repo` and the SQLite savepoint workaround: behaviour changes for every caller / test-only; left for the final review.
- api: `tier` filter on `GET /api/matches`, dashboard deposit/withdrawal action: features, not integration.

## New dependency requests

None.
