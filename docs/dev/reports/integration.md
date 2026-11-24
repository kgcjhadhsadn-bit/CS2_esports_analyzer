# integration: implementation report

**Tests:** `.venv/bin/python -m pytest tests/services -q -n 2` - 58 passed, 0 failed (about 2 min); whole suite `.venv/bin/python -m pytest tests -q -n 2` - 2973 passed, 20 skipped (browser/PostgreSQL), 0 failed (about 20 min, after review round 1); `ruff check src tests`, `ruff format --check src tests` and `mypy` (190 files) clean.

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
                  max_prediction_age=timedelta(hours=8), match_ids=None,
                  should_stop: Callable[[], bool] | None = None) -> BettingReport      # should_stop: review round 1
with_open_paper_bets(session, state: BankrollState) -> BankrollState
idempotency_key(session, match_id, spec_key, selection_key, *, dry_run) -> str   # "cs2b-<mode>-<match>-<hash>-<attempt>"
enforce_one_selection_per_market(decisions, *, bet_specs, blocked_outcomes=None) -> list[BetDecision]
cancel_stale_pending(session, *, now, older_than=timedelta(minutes=2)) -> int    # compare-and-swap per row
transition_status(session, bet, *, expected: BetStatus, new: BetStatus, **values) -> bool   # review round 1
is_unconfirmed_failure(bet) -> bool                                                         # review round 1
STOP_CYCLE_STATUSES = {UNCERTAIN, KILL_SWITCH, INSUFFICIENT_BALANCE}; PAUSING_STATUSES = {LOGIN_REQUIRED, SELECTOR_ERROR}
```
`BettingReport`: counters (`decisions`, `accepted`, `bets_created`, `placed`, `dry_run_ok`, `failed`, `uncertain`, `cancelled`, `stale_pending_cancelled`), `halted_reason`, `paused_reason`, `loss_limit_hit`, `bets`, `notes`, `errors`, `.items`, `.as_dict()`. Real money moves only when neither `dry_run` nor `settings.executor.dry_run` is set (and the executor checks both again). PENDING -> PLACING is a committed compare-and-swap before `executor.place`. LOGIN_REQUIRED / SELECTOR_ERROR pause trading; UNCERTAIN stays PLACING, is never retried and stops placing for the rest of the cycle (as do KILL_SWITCH, INSUFFICIENT_BALANCE, a closed executor - recorded FAILED, nothing was sent -, a result that cannot be recorded, an unexpected error after a bet row was created, and `should_stop()`). An outcome whose unconfirmed placement settlement could not find (`extra["unconfirmed_not_found"]`) is never bet again, nor another selection of its market. One cycle at a time per process.

### `cs2bot.services.settlement`
```python
settle_bets(session_factory, settings, executor=None, *, now=None, grace=timedelta(minutes=30),
            health_cache: HealthCache | None = None, check_health=True) -> SettlementReport
match_history_record(bet, records, *, claimed) -> tuple[BookmakerBetRecord | None, int]
history_row_latest_time(placed_at) -> datetime          # review round 1: minute / date-only history rows
plausible_bookmaker_payout(bet, outcome, amount) -> bool  # review round 1
UNCONFIRMED_NOT_FOUND_KEY = "unconfirmed_not_found"; ALERTS_KEY = "settlement_alerts"
```
`SettlementReport` gained `history_failed` (review round 1). Each bet is processed in its own transaction, read `FOR UPDATE` and skipped when its status changed meanwhile. A bookmaker-settled bet takes the bookmaker's outcome and, when plausible, its payout (ADJUSTMENT entry for the difference to the local tax rules; `bet.payout` / `pnl` follow). The balance reconciliation is skipped while the history is unavailable and real bets are open. UNKNOWN / ambiguous / cash-out alerts are raised once per bet (`extra["settlement_alerts"]`).

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
    pandascore_client_factory=None, demo_downloader_factory=None, mapper=None, hltv_enabled=True, demo_limit=5,
    stop_event=threading.Event())   # stop_event: review round 1 (set by run_up at shutdown)
build_jobs(rt: Runtime) -> list[Job]
parse_daily_cron(expr: str, tz: str) -> Daily        # "M H * * *" only; else 05:00
FAST, SYNC, HLTV, TRAIN = "fast", "sync", "hltv", "train"
```
Sources made by `hltv_fetcher_factory` / `pandascore_client_factory` / `demo_downloader_factory` are created in the lane thread and closed after each job (review round 1). Jobs: `odds_collect`, `predict_upcoming`, `bet_run`, `settle_bets`, `executor_health` (fast); `ingest_upcoming_pandascore`, `ingest_results_pandascore` (need a token), `scope_refresh_upcoming`, `news_triage` (needs an enabled analyzer) (sync); `ingest_upcoming_hltv`, `ingest_results_hltv`, `ingest_rankings_hltv` (12 h), `ingest_news_hltv`, `demos_process` (hltv); `models_retrain` (train).

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

## Review round 1

Independent review with the focus on the betting cycle and settlement: double placement after crashes, restarts and concurrent processes, halt checks, ledger arithmetic, exposure and loss limits, dry-run versus live separation, and the thread confinement of Playwright objects in the scheduler lanes. Every fix has a test in `tests/services/test_services_betting_safety.py` (18 tests; the settlement ones were checked to fail against the code before the review); one assertion was added to the crash test in `test_services_e2e.py`.

**Verified, no change needed.** A crash between the PENDING commit and the PLACING commit leaves a row that was never sent (cancelled and re-decided under a new key); a crash after the PLACING commit leaves a PLACING row that blocks its outcome, counts as exposure and is confirmed from the bet history (both covered by e2e tests). Idempotency keys (mode prefix + attempt number, unique column) and the executor's placement journal agree. Halt checks: cycle start, sizing (`compute_state` carries kill switch / pause), a fresh-session read right before every placement, and the executor's own checks. `check_loss_limits` runs every cycle. Dry run vs live: separate key prefixes, `include_dry_run` only in dry mode, `compute_state` ignores paper bets, reconciliation is live-only, paper bets settle on paper. Ledger arithmetic (`apply_placement`, `settle_bet`, `reconcile`) matches the engine contract and the e2e test (`ledger == 1000 - stakes + payouts == bookmaker balance`). Playwright: the executor is a `ThreadBoundExecutor` (one worker thread, also for the dashboard's login launcher); the odds browser source runs its own thread per fetch; the HLTV fetcher and the demo downloader are opened and closed inside the `hltv` lane; nothing Playwright-bound is closed from another thread.

**Issues found and fixed:**

1. *(critical)* **Cross-process status race.** The stale-PENDING clean-up of one process (e.g. `cs2bot bet run` next to `cs2bot up`) loaded PENDING rows and wrote CANCELLED unconditionally, so it could overwrite a row the other process had just moved to PLACING and handed to the executor: exposure released and the outcome re-decidable, i.e. a double bet. The cycle also sent rows another process had cancelled. Now `transition_status` is a compare-and-swap UPDATE (`... WHERE status = expected`) for PENDING -> PLACING and PENDING -> CANCELLED; the loser does nothing (the placing side stops the cycle).
2. *(critical)* **Re-bet after "not found".** A PLACING bet missing from the history after the grace period became FAILED, and FAILED is non-blocking, so the next cycle bet the same outcome under a new idempotency key - a real double bet whenever the history page missed the row (the executor skips unreadable rows). Settlement now flags it (`extra["unconfirmed_not_found"]`) and the betting cycle never backs that outcome (nor another selection of its market) again; the alert text says so. A bet that does exist still shows up as a balance shortfall in the same pass's reconciliation, which pauses.
3. *(major)* **Date-only history rows never matched.** The executor parses a row that shows only the day as local midnight (its documented convention), but the matcher required the row time to be within 2 minutes before the attempt, so such a bet was declared FAILED after the grace period with its money at risk. `history_row_latest_time` applies the minute / whole-day resolution (DST-aware).
4. *(major)* **Reconciliation without the bet history double-counted payouts.** With the history unavailable, payouts Betclic had already credited were booked as a RECONCILIATION (flag + pause) and later again by the local settlement. The reconciliation is now skipped (note in the report) while real bets are open and the history could not be read.
5. *(major)* **Bookmaker payout ignored.** The ledger always used the locally computed payout; every tax-rule difference (void keeping the stake tax, winnings-tax rounding) left a wrong `pnl` on the bet (and in the loss limits) and paused the bot through a reconciliation mismatch. A plausible payout from the history (0 < won <= stake x odds, void <= stake, lost = 0) is now followed with an ADJUSTMENT entry and the bet's `payout` / `pnl`; an implausible one (misread row) is not booked and is alerted.
6. *(major)* **Shutdown in the middle of a cycle.** After Ctrl+C the cycle kept placing until the executor was closed under it, and `ExecutorClosedError` (raised before anything is sent) was recorded as UNCERTAIN: PLACING rows, "verify on Betclic" alerts and blocked outcomes for bets that were never sent. `Runtime.stop_event` (set by `run_up`) is passed as `should_stop` and checked before every placement; `ExecutorClosedError` / `ExecutorThreadError` are recorded FAILED and stop the cycle.
7. *(major)* **UNCERTAIN did not stop the cycle.** After an unconfirmed click the cycle went on clicking every other accepted outcome on a page that had just misbehaved. UNCERTAIN (and the executor's KILL_SWITCH / INSUFFICIENT_BALANCE answers) now stop placing for the rest of the cycle, without pausing.
8. *(major)* **Failure to record a placement result.** A `LedgerConflictError` returned the stale bankroll state (the new bet not reserved) and kept placing; any other exception from `apply_placement` escaped. Now the bet stays PLACING (exposure, settlement confirms it), the audit is best effort, and the cycle stops. An unexpected error after a bet row was created also stops the cycle; the stop flag lives on a per-cycle object, so it survives an exception in a match (it used to be the return value of the match function).
9. *(minor)* **Dry-run sizing.** A paper bet the executor answered FAILED still reserved its stake for the rest of the match; after a dry-run row turned real (UNCERTAIN in dry mode) the paper overlay was dropped. Both size against `_sizing_state` now.
10. *(minor)* **Frozen cycle clock.** Odds freshness and minutes-before-kick-off checks used the cycle start for matches decided late in a long cycle (each placement takes a while). The cycle clock now advances with real time from the given `now`.
11. *(minor)* **Concurrent settlement.** Two settlement runs (CLI `settle` next to `up`) could both book one payout (the ledger's one-entry-per-bet rule is code, not a constraint). Each bet is now read `FOR UPDATE` and skipped when its status changed.
12. *(minor)* UNKNOWN / ambiguous / cash-out alerts were written to the audit log on every settlement pass (every few minutes); now once per bet.
13. *(minor)* Sources made by the job factories (HLTV fetcher, PandaScore client, demo downloader) were never closed (one leaked browser / HTTP client per job); now closed in the lane thread after each job.
14. *(minor)* A failure in the closing-odds / CLV update aborted `settle_bets` before the balance reconciliation; it is now recorded as an error and the pass continues.

**Remaining concerns:**

- The ledger's one-entry-per-(bet, kind) rule is still enforced in code only (migration 0002 needed, see the foundation change requests). The row locks of item 11 cover settlement on PostgreSQL; the PostgreSQL path itself was not exercised here (no `CS2BOT_TEST_DATABASE_URL`).
- An outcome blocked by item 2 cannot be unblocked from the dashboard; it stays blocked for that match (a missed bet, not a money risk).
- A bet the history shows with an unreadable (UNKNOWN) status is still settled locally once the match is finished; if Betclic has not credited it yet the reconciliation pauses the bot (safe, but noisy).
- INSUFFICIENT_BALANCE stops a cycle but does not pause; each following cycle sends one more order that fails the same way until the reconciliation (or the owner) catches up.
- The stale-PENDING threshold (2 minutes on the cycle clock) can still cancel a PENDING row of a cycle running in another process; thanks to item 1 that process then sends nothing and stops.
- Nothing ran against live Betclic / HLTV / PandaScore (unchanged).

**Foundation change requests (new):**

- `domain.betting.BookmakerBetRecord`: add the time resolution of `placed_at` (`"second" | "minute" | "day"`) set by the executor, instead of the "local midnight = whole day" convention that `services.settlement.history_row_latest_time` has to mirror.
- `api`: an owner action that clears `extra["unconfirmed_not_found"]` on a FAILED bet after checking Betclic by hand.
