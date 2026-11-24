# Operating cs2bot

Day-to-day operation on the owner's Windows PC. Installation is in
[WINDOWS_SETUP.md](WINDOWS_SETUP.md); scope and design in [../PLAN.md](../PLAN.md) and
[ARCHITECTURE.md](ARCHITECTURE.md); the dashboard API in [API.md](API.md).

All commands below run in a PowerShell window in the repository folder (e.g. `C:\cs2bot`). The
`deploy\windows\*.cmd` files can also be double-clicked.

---

## The pieces

| Piece | Where it runs | Started by |
|---|---|---|
| PostgreSQL 16 (all data, the bet ledger) | Docker Desktop, container `cs2bot-postgres`, volume `cs2bot_pgdata`, `127.0.0.1:5432` only | `start.ps1` / `docker compose up -d` (restarts with Docker unless stopped) |
| The bot: API + dashboard + scheduler + executor | natively, `uv run cs2bot up`, one Python process | `start.cmd`, or the `cs2bot` Task Scheduler entry at sign-in |
| Dashboard | http://127.0.0.1:8000 (this PC only), served by the bot | with the bot |
| Bot browsers (Chromium) | Betclic executor: a **visible** window with the profile `data\browser\betclic`; odds collector and HLTV: separate browser contexts | by the bot when needed |

`cs2bot up` is the single long-running process. Stopping it (Ctrl+C or `stop.cmd`) stops all jobs;
nothing else keeps working in the background except PostgreSQL.

Only one copy can run: `start.ps1` holds a lock (a named Windows mutex) for as long as it starts and
runs the bot, so the autostart task, the daily wake task and a double-clicked `start.cmd` never start
a second bot; `update.ps1` and `restore-db.ps1` hold the same lock while they work.

## What runs when

The built-in scheduler runs jobs in **lanes**: each lane is one thread running its jobs one at a
time, so a slow data download never delays odds collection or betting. Intervals come from the
`CS2BOT_SCHEDULER__*` settings (defaults below); every run is recorded in the `job_runs` table and
shown on **Dashboard > Health** (the exact job names are listed there and at `GET /api/jobs`).

| Job | Default interval | Setting | Lane | What it does |
|---|---|---|---|---|
| Betclic odds collection | every 3 min | `ODDS_POLL_S=180` | fast | Loads Betclic's CS2 pages, stores prices (only when they change), links events to matches, maps market names. |
| Predictions | every 15 min | `PREDICT_MIN=15` | fast | For in-scope matches inside the betting window: veto distribution, per-map estimates, simulations, Claude context, priced outcomes. Re-predicts when the veto, line-up, news flags or model version changed. |
| Betting | after predictions / odds | - | fast | Price -> EV after tax -> decision rules -> stake (1/4 Kelly + caps) -> place (or dry run). Checks the kill switch / pause before every placement and the loss limits every cycle. |
| Settlement | every 30 min | `SETTLE_MIN=30` | fast | Settles finished bets (Betclic's bet history is the source of truth for money), closing odds and CLV, ledger, balance reconciliation. |
| Upcoming matches | every 30 min | `UPCOMING_SYNC_MIN=30` | slow | PandaScore + HLTV schedules, line-ups, vetoes once published. |
| Results | every 60 min | `RESULTS_SYNC_MIN=60` | slow | Finished matches, map scores, player stats, demo links. |
| News | every 30 min | `NEWS_POLL_MIN=30` | slow | HLTV news; Claude Haiku triages them, Claude Opus turns relevant ones into bounded context flags. |
| Demo processing | every 60 min | `DEMO_PROCESS_MIN=60` | slow | Downloads + parses pending demos (rounds, kills, economy, positions), then deletes the raw files. |
| Nightly retrain | daily 05:00 Warsaw | `RETRAIN_CRON="0 5 * * *"` | slow | Refits ratings, veto, round, kill and ensemble models on data up to that moment and swaps them in. |

The betting window is `CS2BOT_DECISION__MIN_MINUTES_BEFORE_START` (10) to
`CS2BOT_DECISION__MAX_HOURS_BEFORE_START` (48) before the scheduled start. Live (in-play) betting is
never done.

### Catch-up after the PC was off

When the bot starts, each job looks up its last run in `job_runs`. A job whose time passed while the
PC was off or asleep runs **once** right away (not once per missed slot); a missed 05:00 retrain runs
at the next start. A failing job backs off (never faster than its own interval) and never stops the
other jobs; three or more consecutive failures appear as an alert on the dashboard.

What cannot be caught up: bets on matches that started while the PC was off, and odds history from
that time (closing odds / CLV are then missing for those matches).

## Safety controls

Every component that can move money checks the controls immediately before acting.

| Control | Effect | How |
|---|---|---|
| **Kill switch** | Stops all betting at once, also between the steps of a placement that is in progress. Stays on until you switch it off. Data jobs keep running. | Dashboard (big red button), or `uv run cs2bot control kill-switch on` / `off` |
| **Pause / Resume** | Halts betting until resumed. The bot pauses **itself** when a loss limit is hit, when the Betclic login expired, or when the page selectors broke - the reason is shown on the dashboard. Resume clears the pause only; it never turns off the kill switch. | Dashboard, or `uv run cs2bot control pause` / `resume` |
| **Loss limits** | When the realised loss of the day (week) reaches `DAILY_LOSS_LIMIT_FRACTION` (`WEEKLY_...`) x the bankroll at the start of that day (week), betting pauses until you resume. Stakes are also sized so that even if every open bet loses, the day's loss stays within the limit. Days follow Warsaw time. | `CS2BOT_BANKROLL__DAILY_LOSS_LIMIT_FRACTION=0.05`, `..._WEEKLY_...=0.10`, dashboard Settings |
| **Caps** | Max stake per bet (2% of bankroll), max exposure per match (4%), max bets per day (10), never two selections of one market. | `CS2BOT_BANKROLL__*`, dashboard Settings |
| **Dry run** | Everything except the final click; bets are recorded as `dry_run` and settled on paper; no ledger entries, no effect on the limits. | `CS2BOT_EXECUTOR__DRY_RUN=true` in `.env`, restart |
| **Idempotency** | A bet is written to the database (and committed) before the click and is never placed twice for the same prediction, also after a crash. A placement whose outcome is unclear (`UNCERTAIN`) is never retried; settlement reconciles it with Betclic's bet history. | built in |

In an emergency: kill switch on the dashboard, or `stop.cmd`. Betclic itself is always the final
truth for money: compare with "Moje zakłady".

## Daily routine (5 minutes)

1. **Dashboard > Overview:** no red alerts; balance and open exposure look plausible; dry-run
   indicator as expected.
2. **Alerts** to act on:
   - *Betclic login required* -> `betclic-login.cmd` (or the button), then **Resume**.
   - *Trading paused* -> read the reason. Loss limit: resume only when you consciously want to
     continue. Selector error: see [Recalibrating selectors](#recalibrating-betclic-selectors).
   - *Job failing / overdue* -> Health page shows the error; check `logs\`.
   - *Link review pending* -> **Review** page: confirm or reject Betclic events the bot could not
     link with certainty (they are never bet until confirmed).
   - *Executor unhealthy / selectors failing* -> `uv run cs2bot executor health` (bot stopped) and
     [Recalibrating selectors](#recalibrating-betclic-selectors).
   - *Loss limit reached* -> the bot paused itself; see the safety controls above.
   - Once a day compare the dashboard bankroll with the balance shown on Betclic.
3. **Bets:** yesterday's settled bets; any bet stuck as `placing` / uncertain -> check it in "Moje
   zakłady".
4. Make sure the PC will be **on and awake** in the hours before today's matches.

Weekly: look at **Performance** (ROI, CLV, calibration by market), make sure backups exist
(`backups\`), copy them off the PC.

## Interpreting the dashboard

- **Model vs Betclic:** for every mapped outcome, the model probability `p`, Betclic's odds, and
  **EV after tax** = `p x 0.88 x odds - 1` (simplified; voids/pushes are refunded). The decision column
  says "bet" or the reasons for skipping (probability below threshold, EV too small, uncertainty
  `sigma` too high, data incomplete, event link not confident, too close to kick-off, caps ...).
  Most outcomes are skipped - that is by design.
- **`p_win_settled`** is the probability of winning given the bet is not refunded; it is what the
  thresholds and calibration use.
- **CLV (closing-line value):** how your price compares with Betclic's last price before the match.
  Consistently positive CLV is the best early sign of a real edge, long before ROI is meaningful.
- **Calibration:** in the reliability diagram, points near the diagonal mean "when the model says
  80%, it wins about 80%". Systematic points below the diagonal mean the model is overconfident:
  raise the thresholds.
- **Paper bets** (`dry_run`): read their `result` (won / lost / void), not the status.
- **Health:** job ages and statuses, data freshness, executor health (login, balance, selectors).

## Logs

| File | Content |
|---|---|
| `logs\cs2bot-YYYY-MM-DD.log` | Everything `cs2bot up` printed that day (one file per day; written by `start.ps1`; deleted after 30 days, see `start.ps1 -LogRetentionDays`). Starts / stops are marked with `=====` lines. JSON lines when `CS2BOT_LOG_JSON=true`. |
| `logs\backup.log` | Results of every backup run. |
| `data\screenshots\` | A screenshot of every placement attempt; on selector errors also the page HTML. |
| `docker compose logs postgres` | Database server log (slow queries over 2 s are logged). |

Secrets (API keys, passwords, cookies) are never logged.

## Backups and restore

- `backup-db.cmd` - backup now (`backups\cs2bot-YYYYMMDD-HHMMSS.dump`, compressed PostgreSQL
  custom format, verified by reading it back). Retention: 14 days, newest 3 always kept
  (`-KeepDays`, `-KeepAtLeast`).
- `backup-db.cmd -Schedule 04:30` - daily Task Scheduler entry `cs2bot-backup` (`-Unschedule`
  removes it).
- `update.ps1` takes a `before-update` backup, `restore-db.ps1` a `before-restore` backup.
- `restore-db.cmd [-BackupFile ...]` - bot must be stopped; type `RESTORE` to confirm; then
  `cs2bot db upgrade` runs. `restore-db.ps1 -List` lists the backups.
- Copy `backups\` off the PC regularly. Model artefacts (`data\models`) are rebuilt by the nightly
  retrain and need no backup; the Betclic browser profile (`data\browser\betclic`) holds your
  session and the placement journal - do not delete it while bets are open.

On Linux/macOS (development) use `docker exec cs2bot-postgres pg_dump -U cs2bot -d cs2bot -Fc > x.dump`.

## Tuning thresholds and limits

Two places, the dashboard winning:

1. **`.env`** - the defaults (`CS2BOT_DECISION__*`, `CS2BOT_BANKROLL__*`); restart to apply.
2. **Dashboard > Settings** - overrides stored in the database (`system_state.config_overrides`),
   effective at the next betting cycle, with an audit-log entry. The page shows effective values,
   defaults and overrides; clearing an override falls back to `.env`.

Main knobs (PLAN.md section 6):

| Setting | Default | Raise it to... |
|---|---|---|
| `decision.min_probability` | 0.75 | bet only on outcomes the model is surer about |
| `decision.min_ev` | 0.03 | demand a larger edge after tax (fewer bets) |
| `decision.max_sigma` | 0.05 | (lower it) skip outcomes with uncertain estimates |
| `decision.min_data_completeness` | 0.95 | skip matches with missing line-ups / history |
| `decision.market_overrides` | - | stricter thresholds for one market type (e.g. player kills) |
| `decision.disabled_markets` | - | switch market types off completely |
| `bankroll.kelly_fraction` | 0.25 | (lower it) smaller stakes |
| `bankroll.max_stake_fraction` / `max_match_exposure_fraction` | 0.02 / 0.04 | (lower it) cap risk per bet / per match |
| `bankroll.max_bets_per_day` | 10 | (lower it) fewer bets, less bot-like |

Rules of thumb: change one thing at a time; judge changes on CLV and calibration over a few hundred
bets, not on last week's P&L; never loosen thresholds to "win back" losses.

`CS2BOT_BETCLIC__TAX_FREE_PROMO=true` only while Betclic actually runs a "bez podatku" promotion
for these bets (it changes the EV calculation).

## Recalibrating Betclic selectors

Betclic changes its website now and then. The executor finds page elements through
`config\betclic_selectors.yaml` (ordered fallbacks per element; the file header is a step-by-step
DevTools guide).

1. Symptoms: `SELECTOR_ERROR` bets, a "selector" alert, the log line `selector 'x' matched fallback
   #n` (the primary locator no longer matches - fix it before the fallbacks run out).
2. Turn on the kill switch (or the dry run) until fixed.
3. Stop the bot; open the bot's browser with `uv run cs2bot executor login`, or open the saved
   `*_selector_error.html` from `data\screenshots\`.
4. Find the new selector (F12), add it at the top of that element's list, keep the old ones as
   fallbacks, set `verified: true`.
5. `uv run cs2bot executor health`, start the bot, watch one dry-run bet and its screenshot.
6. `update.ps1` keeps your edits (git stash) - consider sending them upstream.

## Recalibrating market rules

`config\betclic_markets.yaml` maps Betclic's Polish market names to the model's market types.

1. `uv run cs2bot odds unmapped` - raw market names that no rule matched, most frequent first. Raw
   Betclic payloads are also kept under `data\raw\betclic\<date>\` for inspection.
2. Add or adjust a rule (the file header explains patterns, selection kinds, required parameters).
   Specific rules before generic ones.
3. Restart the bot; the collector re-maps on its next pass (about 3 minutes).
4. Check the market on a match page of the dashboard (model p vs odds looks sane) before letting it
   bet. When unsure, leave a market unmapped or put it in `decision.disabled_markets`.

## Data retention

| Data | Kept |
|---|---|
| Matches, maps, rounds, players, odds history, predictions, decisions, bets, ledger | forever (the database; it grows with the backfill - plan 100-200 GB of disk) |
| Raw demo archives / `.dem` files | deleted right after parsing (`CS2BOT_DEMOS__KEEP_ARCHIVES=false`, `..._KEEP_DEM_FILES=false`) |
| HLTV page cache (`data\cache\hltv`) | 6 h for live pages, 30 days for finished matches |
| Raw Betclic payloads (`data\raw\betclic`) | for parser fitting; 14 days by default |
| Screenshots (`data\screenshots`) | until deleted by hand; keep those of open or disputed bets |
| Model artefacts (`data\models`) | one folder per retrain; old versions can be deleted |
| Logs (`logs\`) | 30 days |
| Backups (`backups\`) | 14 days, newest 3 always |

Check free space now and then (`uv run cs2bot doctor` reports it); Docker Desktop's disk image does
not shrink by itself after data is deleted.

## Command cheat sheet

| Task | Command |
|---|---|
| Start / stop | `deploy\windows\start.cmd` / `deploy\windows\stop.cmd` |
| Betclic login | `deploy\windows\betclic-login.cmd` |
| Status checks | `uv run cs2bot doctor`, `uv run cs2bot executor health`, `uv run cs2bot executor balance` |
| Safety | `uv run cs2bot control kill-switch on` / `off`, `control pause`, `control resume` |
| Data by hand | `uv run cs2bot ingest upcoming`, `ingest results`, `ingest news`, `ingest rankings`, `ingest match <hltv_id>`, `ingest backfill --since 2023-09-27`, `ingest hltv-browser` |
| Demos | `uv run cs2bot demos process --limit 20` |
| Odds | `uv run cs2bot odds collect --once`, `uv run cs2bot odds unmapped` |
| Models | `uv run cs2bot models train`, `models tune` |
| Predict / bet / settle by hand | `uv run cs2bot predict upcoming`, `predict match <id>`, `bet run --dry-run`, `settle` |
| Backtest | `uv run cs2bot backtest --from 2025-01-01 --to 2025-06-30` |
| Backup / restore / update | `backup-db.cmd`, `restore-db.cmd`, `update.cmd` |
| Autostart / power | `register-autostart.cmd`, `unregister-autostart.cmd`, `power-settings.cmd` |

Most hand-run commands that use a browser (`executor ...`, `ingest hltv-browser`, `odds collect`)
need the bot to be stopped, because the running bot holds the browser profiles.
