# Operating cs2bot

Day-to-day operation. The bot runs either on a **Polish VPS, fully autonomous** (the main runtime
since 2026-11-24: Docker, Xvfb, Telegram; installation in [VPS_SETUP.md](VPS_SETUP.md), autonomy in
[Autonomous operation](#autonomous-operation-vps)) or on the owner's Windows PC (installation in
[WINDOWS_SETUP.md](WINDOWS_SETUP.md)). Scope and design in [../PLAN.md](../PLAN.md) and
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
time, so a slow data download never delays odds collection or betting. The lanes are `fast` (the
money path), `sync` (PandaScore and other HTTP / database work), `hltv` (everything that uses the
HLTV browser profile), `train` (the nightly retrain), `notify` (Telegram notifications and the daily
report) and `telegram` (the Telegram command poller). Intervals come from the
`CS2BOT_SCHEDULER__*` settings (defaults below); every run is recorded in the `job_runs` table and
shown on **Dashboard > Health** (the exact job names are listed there and at `GET /api/jobs`).

| Job | Default interval | Setting | Lane | What it does |
|---|---|---|---|---|
| Betclic odds collection | every 3 min | `ODDS_POLL_S=180` | fast | Loads Betclic's CS2 pages, stores prices (only when they change), links events to matches, maps market names. |
| Predictions | every 15 min | `PREDICT_MIN=15` | fast | For in-scope matches inside the betting window: veto distribution, per-map estimates, simulations, Claude context, priced outcomes. Re-predicts when the veto, line-up, news flags or model version changed. |
| Betting | every 3 min, right after the odds pass | `ODDS_POLL_S=180` | fast | Price -> EV after tax -> decision rules -> stake (1/4 Kelly + caps) -> place (or dry run). Checks the kill switch / pause before every placement and the loss limits every cycle. |
| Settlement | every 30 min | `SETTLE_MIN=30` | fast | Settles finished bets (Betclic's bet history is the source of truth for money), closing odds and CLV, ledger, balance reconciliation. |
| Upcoming matches | every 30 min | `UPCOMING_SYNC_MIN=30` | sync (PandaScore, scope refresh), hltv (HLTV) | PandaScore + HLTV schedules, line-ups, vetoes once published. |
| Results | every 60 min | `RESULTS_SYNC_MIN=60` | sync (PandaScore), hltv (HLTV) | Finished matches, map scores, player stats, demo links. |
| News | every 30 min | `NEWS_POLL_MIN=30` | hltv (HLTV news), sync (Claude triage) | HLTV news; Claude Haiku triages them, Claude Opus turns relevant ones into bounded context flags. |
| Demo processing | every 60 min | `DEMO_PROCESS_MIN=60` | hltv | Downloads + parses pending demos (rounds, kills, economy, positions), then deletes the raw files and rebuilds the position / duel aggregates and player roles when anything new was parsed. |
| Nightly retrain | daily 05:00 Warsaw | `RETRAIN_CRON="0 5 * * *"` (only `M H * * *`) | train | Refits ratings, veto, round, kill and ensemble models on data up to that moment and swaps them in. |
| HLTV rankings | every 12 h | - | hltv | Current HLTV ranking (the top-50 scope rule) and the rank history. |
| Executor health | every 15 min | - | fast | Login state, balance and selector checks of the Betclic browser (shown on Health). |
| Auto-resume (`autonomy_check`) | every 5 min | `CS2BOT_AUTONOMY__CHECK_INTERVAL_MIN=5` (only with `AUTONOMY__ENABLED=true`) | fast | Checks every automatic pause reason and lifts the resolved ones; auto-login when logged out; circuit breaker. See [Autonomous operation](#autonomous-operation-vps). |
| Telegram notifications (`telegram_notify`) | every 30 s | `CS2BOT_TELEGRAM__NOTIFY_INTERVAL_S=30` | notify | Sends new bets, settlements, pause / resume / kill-switch / login events and failing jobs. |
| Daily report (`telegram_daily_report`) | daily 21:00 Warsaw | `CS2BOT_TELEGRAM__DAILY_REPORT_TIME=21:00` | notify | P&L today and this week, bets placed / settled, balance, pause state. |
| Telegram commands (`telegram_commands`) | continuous (long polling) | `CS2BOT_TELEGRAM__COMMANDS_ENABLED=true` | telegram | `/status /balance /bets /pause /resume /stop /go /help` from your chat only. |

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
| **Pause / Resume** | Halts betting until resumed. The bot pauses **itself** when a loss limit is hit, when the Betclic login expired, when the page selectors broke, after two unconfirmed placements or a balance mismatch - every reason is stored with a code and shown on the dashboard (`cs2bot control status`, Telegram `/status`). With autonomy on, these automatic pauses lift themselves once the cause is resolved (see below); a **manual** pause never does. Resume clears every reason; it never turns off the kill switch. | Dashboard, Telegram `/pause` `/resume`, or `uv run cs2bot control pause` / `resume` |
| **Loss limits** | When the realised loss of the day (week) reaches `DAILY_LOSS_LIMIT_FRACTION` (`WEEKLY_...`) x the bankroll at the start of that day (week), betting pauses until you resume (with autonomy on: until the next Warsaw midnight / Monday 00:00). Stakes are also sized so that even if every open bet loses, the day's loss stays within the limit. Days follow Warsaw time. | `CS2BOT_BANKROLL__DAILY_LOSS_LIMIT_FRACTION=0.05`, `..._WEEKLY_...=0.10`, dashboard Settings |
| **Caps** | Max stake per bet (2% of bankroll), max exposure per match (4%), max bets per day (10), never two selections of one market. | `CS2BOT_BANKROLL__*`, dashboard Settings |
| **Dry run** | Everything except the final click; bets are recorded as `dry_run` and settled on paper; no ledger entries, no effect on the limits. | `CS2BOT_EXECUTOR__DRY_RUN=true` in `.env`, restart |
| **Idempotency** | A bet is written to the database (and committed) before the click and is never placed twice for the same prediction, also after a crash. A placement whose outcome is unclear (`UNCERTAIN`) is never retried; settlement reconciles it with Betclic's bet history. A **second** unconfirmed placement while the first is still open **pauses** the bot (the confirmation step is probably broken): check "Moje zakłady" and the receipt selectors, then resume. | built in |
| **Stale predictions** | A match is never bet on a prediction made for other teams / another format, after it left the Tier 1/2 scope, or while its prediction could not be refreshed (teams or lineups gone, an error) - the betting log notes "not bet: ...". | built in |

In an emergency: kill switch on the dashboard, Telegram `/stop`, or `stop.cmd`. Betclic itself is
always the final truth for money: compare with "Moje zakłady".

## Autonomous operation (VPS)

On the VPS nobody watches the bot. Three settings make it run on its own (all off by default, set in
the VPS `.env`):

```
CS2BOT_AUTONOMY__ENABLED=true          # automatic pauses lift themselves (auto-resume service)
CS2BOT_AUTONOMY__AUTO_LOGIN=true       # fill AND submit the Betclic login form when logged out
CS2BOT_BETCLIC__CREDENTIALS_SOURCE=env # with CS2BOT_BETCLIC__USERNAME / PASSWORD
CS2BOT_TELEGRAM__ENABLED=true          # with BOT_TOKEN (@BotFather) and CHAT_ID (@userinfobot)
```

**The bet rule** (owner decision 2026-11-24): confidence only, odds ignored - a pick is bet when the
calibrated probability is at least `decision.min_probability` (75%), whatever the odds (even 1.28).
`decision.min_ev = -1.0` switches the EV check off; set `0.0` for a break-even guard after the 12% tax.
Until the confidence-tier staking (`bankroll.staking_mode = "confidence_tiers"`) is wired into the stake
sizing (final review), stakes are still sized with fractional Kelly, which stakes nothing on a pick
whose EV after tax is not positive - so in practice those picks are recorded but not bet yet.

**Trusted calibration (optional):** `CS2BOT_DECISION__REQUIRE_TRUSTED_CALIBRATION=true` bets a market
type only when the calibration check of its backtest history trusts it (enough resolved predictions,
calibrated above 75%). `cs2bot backtest` writes that verdict to `data/models/confidence_guard.json`
(`models.confidence_guard_file`) and prints the trusted market types. With the setting on and no such
file, nothing is bet (an unknown verdict is not "trusted").

**Browser on the server:** the Betclic executor runs a *visible* Chromium (`CS2BOT_EXECUTOR__HEADLESS=false`)
on the Xvfb virtual display of the container (`DISPLAY=:99`) - less bot-like than headless. Without
any display it falls back to headless (`CS2BOT_EXECUTOR__HEADLESS_WITHOUT_DISPLAY=true`, a warning is
logged). The odds collector and HLTV use headless browsers. A crashed Chromium is started again by the
next job (same profile, same session). `cs2bot doctor` checks the display.

### Pause reasons and when they lift

Every automatic pause stores a machine-readable reason with its time. The `autonomy_check` job (every
5 minutes) checks each one and lifts it once its cause is resolved; every auto-resume is written to the
audit log and sent to Telegram.

| Reason code | Set when | Lifts automatically when |
|---|---|---|
| `loss_limit_daily` | the day's realised loss reached the daily limit | the next Warsaw midnight |
| `loss_limit_weekly` | the week's realised loss reached the weekly limit | Monday 00:00 Warsaw time |
| `uncertain_bet` | a second placement could not be confirmed while the first was still open | settlement has found / resolved every unconfirmed bet in Betclic's bet history |
| `login_required` | Betclic shows the bot as logged out | an auto-login succeeds, or you logged in (dashboard login button / VNC) |
| `2fa_required` | the auto-login reached an SMS / 2FA code step | you logged in by hand once (the bot retries the login every `TWOFA_RETRY_INTERVAL_MIN`, 240 min, in case the step disappears) |
| `balance_mismatch` | Betclic's balance differs from the ledger by more than the tolerance | the difference is booked as a reconciliation entry (Betclic's balance is read again; "auto-resume everything"); waits while unconfirmed placements could explain a shortfall |
| `selector_error` / `executor_unavailable` | the Betclic page layout changed / the executor failed | a full executor health check passes again |
| `manual` | you paused (dashboard, Telegram `/pause`, CLI) | **never** - only your resume |
| `circuit_open` | the same reason lifted itself `CIRCUIT_BREAKER_LIMIT` (3) times within `CIRCUIT_BREAKER_WINDOW_H` (24 h) and came back again | **never** - check what keeps happening, then resume |

The **kill switch** is not a pause reason: only you turn it off (dashboard, Telegram `/go`, CLI).
Logins that fail are retried every `LOGIN_RETRY_INTERVAL_MIN` (15 min, doubling after each failure, at
most once a day). A wrong password or a locked account (recognised from Betclic's message), or
`MAX_LOGIN_FAILURES` (3) submitted logins in a row that neither log in nor reach the SMS step (e.g. an
error text the bot does not recognise), stop the retries until **you resume** (`/resume`, dashboard or
`cs2bot control resume`) - a restart does not clear them, so a restarting container can never lock the
account by repeating a wrong password. Missing credentials are the exception: add them to `.env` and
restart. Your resume also restarts the circuit-breaker count.

### What still needs you

1. **An SMS / 2FA code.** The bot never reads, types or bypasses codes. Telegram says "Betclic asks for
   an SMS / 2FA code"; log in once by hand (the dashboard's "Open Betclic login" button shows the
   server's browser over the VNC-over-SSH tunnel described in VPS_SETUP.md). The pause lifts at the
   next check.
2. **Turning the kill switch off** after you (or something you asked for) turned it on: `/go`.

Also yours, by design: a manual pause, an open circuit breaker, and a stopped auto-login (wrong
password / locked account).

### Telegram

Messages (each can be switched off with `CS2BOT_TELEGRAM__NOTIFY_*`):

| Message | Meaning / what to do |
|---|---|
| `Bet placed: A vs B` (`DRY RUN bet` in dry-run mode) | market, selection, odds, stake, model probability `p` and EV |
| `Bet WON / LOST / VOID` | settlement with payout and P&L |
| `Daily report` | P&L today / this week, bets placed and settled, balance, pause state |
| `PAUSED by system [code]` | an automatic pause; with autonomy on it lifts itself (see the table) |
| `Auto-resume: ...` / `RESUMED automatically` | a reason was resolved / every reason is gone - betting continues |
| `CIRCUIT BREAKER OPEN for '...'` | a pause kept coming back: check the cause (logs, dashboard Health), then `/resume` |
| `Betclic asks for an SMS / 2FA code` | log in by hand once (see above) |
| `Betclic auto-login STOPPED (bad_credentials / account_locked / failed)` | fix the password in `.env` (restart) or check the account at Betclic, then `/resume` (a restart alone does not retry) |
| `KILL SWITCH ON by ...` | all betting stopped until `/go` |
| `Balance mismatch: ...` | money moved that the bot does not know (deposit, withdrawal, a bet placed by hand); booked automatically |
| `Unconfirmed placement (UNCERTAIN)` | check "Moje zakłady"; settlement reconciles it |
| `Job ... failed: ...` | a data job fails (at most one message per job every 6 h); see the Health page / logs |
| `cs2bot started` / `cs2bot is stopping` | the process started / stops (deploys, reboots) |

Commands (only from the numeric `CHAT_ID` - an `@channel` id gets notifications but no commands, and
usernames are never trusted; other chats are ignored and logged; commands older than 10 minutes are
ignored): `/status`, `/balance`, `/bets`, `/pause [reason]`, `/resume` (lifts every pause, never the
kill switch), `/stop` (kill switch ON), `/go` (kill switch OFF), `/help`. Test the setup with
`cs2bot telegram test`.

## Daily routine (5 minutes)

1. **Dashboard > Overview:** no red alerts; balance and open exposure look plausible; dry-run
   indicator as expected.
2. **Alerts** to act on:
   - *Betclic login required* -> `betclic-login.cmd` (or the button), then **Resume**.
   - *Trading paused* -> read the reason. Loss limit: resume only when you consciously want to
     continue. Selector error: see [Recalibrating selectors](#recalibrating-betclic-selectors).
   - *Job failing / overdue* -> Health page shows the error; check `logs\`.
   - *Link review pending* -> **Review** page: confirm or reject Betclic events the bot could not
     link with certainty (they are never bet until confirmed). A confirmed event whose team names
     Betclic later changed beyond recognition comes back here (`extra.review_reason`); one that only
     lists its teams the other way round keeps its link (the orientation follows the names).
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
  `cs2bot db upgrade` runs and the **kill switch is switched ON** (bets placed after the backup are
  missing from the restored database and could be staked again): compare the open bets with "Moje
  zakłady", run `uv run cs2bot settle`, check the bankroll, then switch the kill switch off.
  `restore-db.ps1 -List` lists the backups.
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
| `decision.min_ev` | -1.0 (odds ignored) | 0.0 = break-even guard after the 12% tax; higher = demand an edge (fewer bets) |
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
| Model artefacts (`data\models`) | one folder per retrain; the newest 5 are kept, older ones are deleted after each retrain |
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
| Safety | `uv run cs2bot control kill-switch on` / `off`, `control pause`, `control resume`, `control status` (every pause reason, auto-resume ETA, circuit breaker) |
| Telegram | `uv run cs2bot telegram test` (sends a test message) |
| Data by hand | `uv run cs2bot ingest upcoming`, `ingest results`, `ingest news`, `ingest rankings`, `ingest match <hltv_id>`, `ingest backfill --since 2023-09-27`, `ingest hltv-browser` |
| Demos | `uv run cs2bot demos process --limit 20` |
| Odds | `uv run cs2bot odds collect --once`, `uv run cs2bot odds unmapped` |
| Models | `uv run cs2bot models train`, `models tune` |
| Predict / bet / settle by hand | `uv run cs2bot predict upcoming`, `predict match <id>`, `bet run --dry-run`, `settle` |
| Backtest | `uv run cs2bot backtest --from 2025-01-01 --to 2025-06-30` (the live model: ensemble + tuned ratings; `--no-ensemble` for a quick ratings-only run). Days are UTC; the final veto is treated as known at decision time, which flatters map-level markets. |
| Backup / restore / update | `backup-db.cmd`, `restore-db.cmd`, `update.cmd` |
| Autostart / power | `register-autostart.cmd`, `unregister-autostart.cmd`, `power-settings.cmd` |

Most hand-run commands that use a browser (`executor ...`, `ingest hltv-browser`, `odds collect`)
need the bot to be stopped, because the running bot holds the browser profiles.
