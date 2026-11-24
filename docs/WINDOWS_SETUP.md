# Installing and running cs2bot on your Windows PC

This guide takes you from a normal Windows 10 / 11 PC to a running bot, step by step. You do not
need to be a programmer. Each step says **what to do** and **what it does**, so you know what is
happening on your PC.

Everything runs **on your own PC**. Nothing is uploaded to a server, and the dashboard can only be
opened on this PC (`http://127.0.0.1:8000`).

> **Read this first.** No betting system wins every bet. Even a true 90% chance loses one time in
> ten, and the bookmaker margin plus the Polish 12% stake tax work against you. Betclic's terms
> forbid bots, so **your Betclic account and its balance are at risk** if Betclic notices the
> automation. Only use money you can afford to lose. You must be 18 or older. See
> [Going live](#8-going-live-real-bets) before you switch off the dry run.

**Contents**

1. [What you need](#1-what-you-need)
2. [Get the code](#2-get-the-code)
3. [Run the installer](#3-run-the-installer)
4. [Fill in your settings (.env)](#4-fill-in-your-settings-env)
5. [First start](#5-first-start)
6. [First Betclic login](#6-first-betclic-login)
7. [Check the dashboard and run the dry-run smoke test](#7-check-the-dashboard-and-run-the-dry-run-smoke-test)
8. [Going live (real bets)](#8-going-live-real-bets)
9. [Keep the PC awake before matches](#9-keep-the-pc-awake-before-matches)
10. [Start automatically at sign-in](#10-start-automatically-at-sign-in)
11. [Backups](#11-backups)
12. [Updating](#12-updating)
13. [Troubleshooting](#13-troubleshooting)
14. [Uninstalling](#14-uninstalling)

Day-to-day operation (what runs when, safety controls, tuning) is described in
[OPERATIONS.md](OPERATIONS.md).

---

## 1. What you need

| What | Why |
|---|---|
| Windows 10 (22H2) or Windows 11, 64-bit | Docker Desktop needs it. |
| 16 GB RAM recommended (8 GB works with smaller database settings, see [Troubleshooting](#the-pc-is-slow-or-runs-out-of-memory)) | The database, the bot and its browser run side by side. |
| **100-200 GB free disk space** | The match database grows over time, and HLTV demo files (several hundred MB each) are stored temporarily while they are parsed. Raw demos are deleted right after parsing, but the history backfill processes thousands of them. |
| Virtualisation enabled in the BIOS/UEFI ("Intel VT-x" / "AMD-V" / "SVM") | Docker Desktop uses WSL 2, which needs it. Most PCs have it on; Task Manager > Performance > CPU shows "Virtualization: Enabled". |
| A PandaScore account on the highest plan, with an API token | Match schedules, line-ups and results. |
| An Anthropic API key (console.anthropic.com) | Claude reads the news and writes the bet explanations. |
| Your Betclic.pl account and the phone used for its 2FA / SMS codes | You log in once by hand in the bot's own browser window. |
| A normal Windows user account (no need to be Administrator) | Installers ask for permission themselves. |

**Never paste your tokens, keys or passwords into a chat, an e-mail or a screenshot.** They only go
into the `.env` file on your PC (step 4).

## 2. Get the code

1. **Install Git** (the program that downloads and updates the code). Open **PowerShell** (Start
   menu > type `PowerShell` > Windows PowerShell) and run:

   ```powershell
   winget install --id Git.Git -e
   ```

   *What it does:* installs Git from Microsoft's package manager. If `winget` is unknown, install
   "App Installer" from the Microsoft Store, or download Git from https://git-scm.com/download/win.
   Close PowerShell and open a new window afterwards.

2. **Download the bot** into a short folder **outside OneDrive** and without Polish letters in the
   path, for example `C:\cs2bot`:

   ```powershell
   git clone <repository URL> C:\cs2bot
   cd C:\cs2bot
   ```

   Use the repository address you were given (on GitHub: green "Code" button > HTTPS). For a
   private repository a GitHub sign-in window opens once.

   *Why not OneDrive / Documents:* OneDrive syncs every log, browser-profile and database file and
   locks them while syncing, which breaks the bot.

## 3. Run the installer

In File Explorer open `C:\cs2bot\deploy\windows` and **double-click `install.cmd`**
(or in PowerShell: `powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1`).

*What the `.cmd` files are:* small launchers for the PowerShell scripts next to them. Windows blocks
running PowerShell scripts by default; the launcher allows it for that one run only
(`-ExecutionPolicy Bypass`). No system setting is changed.

The installer is safe to run again at any time; every step that is already done is skipped:

| Step | What it does |
|---|---|
| Prerequisites | Checks for Git, **Docker Desktop**, **Node.js LTS** (builds the dashboard), **7-Zip** (HLTV demos are `.rar` archives) and **uv** (installs Python and the bot's Python packages). Missing ones are installed with `winget` after asking you; uv comes from its official installer (astral.sh). |
| Disk space | Warns if a drive has less than 100 GB free. |
| Docker Desktop | Starts it and waits until its engine runs. |
| `.env` | Creates your settings file from `.env.example`, with a **random database password**, and opens it in Notepad (step 4). |
| Python packages | `uv sync` - downloads Python 3.12 and all packages into `.venv` (about 1 GB the first time). |
| Browser | `playwright install chromium` - the bot's **own** Chromium, separate from your normal browser. |
| Database | Starts PostgreSQL in Docker (`docker compose up -d`), waits until it is healthy, creates the tables (`cs2bot db upgrade`). |
| Dashboard | Builds the web dashboard (`npm ci`, `npm run build` in `dashboard\`). |
| Check | Runs `cs2bot doctor` and prints the next steps. |

**If Docker Desktop was just installed:** the installer stops and asks you to restart Windows (Docker
sets up WSL 2), open Docker Desktop once from the Start menu, accept its terms (you can skip the
sign-in) and wait for "Engine running". Then double-click `install.cmd` again; it continues where it
stopped. Recommended in Docker Desktop: Settings > General > "Start Docker Desktop when you sign in".

*Where is the database?* Inside Docker Desktop's virtual disk (by default on drive C:). To keep it on
another drive: Docker Desktop > Settings > Resources > Advanced > Disk image location (do this
before the database gets big). PostgreSQL is reachable **only from this PC** (`127.0.0.1:5432`).

## 4. Fill in your settings (.env)

The installer opens `C:\cs2bot\.env` in Notepad (later: `notepad C:\cs2bot\.env`). Each line is
`NAME=value`, no spaces around `=`, no quotes needed. Save with Ctrl+S.

| Setting | What to put there |
|---|---|
| `CS2BOT_PANDASCORE__API_TOKEN` | Your PandaScore token. |
| `ANTHROPIC_API_KEY` | Your Anthropic API key (`sk-ant-...`). The `deploy\windows` scripts hand it to the bot. If you also run `uv run cs2bot ...` commands by hand and want Claude in those, write the key as `CS2BOT_LLM__API_KEY=sk-ant-...` instead (that name is read from `.env` in every case). |
| `CS2BOT_BANKROLL__STARTING_BANKROLL` | The money (PLN) the bot may work with, e.g. `1000.00`. Should match what is on the Betclic account for the bot. |
| `CS2BOT_BANKROLL__MAX_STAKE_FRACTION` | Largest single stake as a share of the bankroll (`0.02` = 2%). |
| `CS2BOT_BANKROLL__MAX_MATCH_EXPOSURE_FRACTION` | Most money at risk on one match (`0.04` = 4%). |
| `CS2BOT_BANKROLL__DAILY_LOSS_LIMIT_FRACTION` / `..._WEEKLY_...` | When the day's (week's) losses reach this share, the bot pauses itself (`0.05` / `0.10`). |
| `CS2BOT_BANKROLL__MAX_BETS_PER_DAY` | Upper limit of bets per day (`10`). Low numbers look less like a bot to Betclic. |
| `CS2BOT_DECISION__MIN_PROBABILITY`, `..._MIN_EV` | How sure the model must be (`0.75`) and the minimum expected value after tax. The owner's rule since 2026-11-24 is "confidence only, odds ignored": `MIN_EV=-1.0` switches the EV check off; `0.0` would demand at least break-even after the 12% stake tax. Raising them means fewer bets. |
| `CS2BOT_EXECUTOR__DRY_RUN` | **Leave `true` for now.** `true` = bets are simulated, no money moves. |
| `CS2BOT_BETCLIC__CREDENTIALS_SOURCE` | `none` (default): you type your Betclic login yourself each time a login is needed. `keyring`: the login window pre-fills it from Windows Credential Manager (entry `cs2bot-betclic`). Avoid `env` (it stores the password in plain text in `.env`). The bot never submits the form or 2FA for you (unless you turn on `CS2BOT_AUTONOMY__AUTO_LOGIN`, meant for the unattended VPS; even then it never types an SMS / 2FA code). |
| `CS2BOT_HLTV__PROXY_URL` | Optional residential proxy for the big HLTV history download, so your home IP is not rate-limited by HLTV (`http://user:pass@host:port`). |
| `CS2BOT_API__PASSWORD` | Leave empty. A dashboard password is only needed if you ever make the dashboard reachable from other devices (not recommended). |

Leave the `POSTGRES_PASSWORD` line at the bottom and the password inside `CS2BOT_DATABASE__URL`
exactly as the installer wrote them: they must match each other, and the database keeps the
password it was created with.

The settings take effect the next time the bot starts. Thresholds and limits can also be changed
later on the dashboard's Settings page (those changes are stored in the database and override
`.env`; see [OPERATIONS.md](OPERATIONS.md#tuning-thresholds-and-limits)).

## 5. First start

Double-click **`deploy\windows\start.cmd`**.

*What it does:* starts Docker Desktop if needed, starts the database and waits until it is ready,
applies database updates, then runs **`cs2bot up`**, which starts the dashboard and the built-in
scheduler (odds, data, predictions, betting, settlement) in one process. Everything the bot prints
is also written to `logs\cs2bot-<date>.log` (one file per day, kept for 30 days). While it runs,
Windows does not go to sleep on its own.

The dashboard opens in your browser at **http://127.0.0.1:8000** after a few seconds.

- **Keep the window open** (you may minimise it). Closing it stops the bot abruptly.
- **To stop the bot:** press **Ctrl+C** in that window (it finishes what it is doing, closes its
  browser and exits; when Windows asks "Terminate batch job (Y/N)?", answer `Y`), or double-click
  **`stop.cmd`** from anywhere.
- The first start after installation spends a while downloading history (PandaScore, HLTV). The
  first predictions appear once enough data has arrived and the models are trained.

## 6. First Betclic login

The bot places bets in **its own Chromium window with its own profile** (stored in
`data\browser\betclic`), separate from your normal browser. You log in there once:

1. Double-click **`deploy\windows\betclic-login.cmd`** (works whether the bot is running or not; if
   it is running, the bot opens the window itself - the same as the "Betclic login" button on the
   dashboard).
2. In the Chromium window that opens: log in to Betclic.pl, **tick "remember me"**, confirm the SMS /
   2FA code on your phone.
3. Wait until your balance appears in the page header. Done - the session is stored in the profile.
4. While you are logged in there, check two Betclic account settings once:
   - when the odds change, Betclic must **ask before accepting** a bet (do not enable "accept all odds
     changes automatically");
   - **no quick-bet / one-click betting** mode.

When the session expires (Betclic logs you out every now and then), the bot **pauses betting by
itself** and the dashboard shows "Betclic login required". Log in again the same way, then press
**Resume** on the dashboard.

## 7. Check the dashboard and run the dry-run smoke test

With `CS2BOT_EXECUTOR__DRY_RUN=true` the bot does everything except the final "Postaw zakład"
click: it opens the match, adds the outcome to the bet slip, types the stake, finds the button and
clears the slip again. Use this to confirm that the bot works with the live Betclic site **before**
any money is at risk:

1. **Health check of the installation.** In a PowerShell window in `C:\cs2bot`:

   ```powershell
   uv run cs2bot doctor
   ```

   It checks the database, migrations, the bot's Chromium, the API keys, 7-Zip, disk space, the
   config files and the dry-run state.

2. **Dashboard > Health:** every data job should turn green over the first hours (odds every few
   minutes, upcoming matches every 30 minutes, results hourly, ...). Red / "failing" jobs show their
   error message; see [Troubleshooting](#13-troubleshooting).

3. **Betclic page check** (bot stopped: `stop.cmd`, then):

   ```powershell
   uv run cs2bot executor health
   ```

   It opens the bot's browser and checks the login, the balance and the key page elements
   ("selectors"). Anything reported as not found must be fixed before going live (see
   [Betclic selectors broken](#betclic-selectors-broken-after-a-site-update)).

4. **Let it run for a day or two in dry run.** Watch **Bets** on the dashboard: simulated bets appear
   with status `dry_run` and are settled "on paper" after the match, so you see what the bot *would*
   have won or lost. Each attempt saves a screenshot (`data\screenshots\`); open a few and check that
   the right match, market, outcome and stake were in the bet slip.

5. **Markets the bot cannot read yet:** `uv run cs2bot odds unmapped` lists Betclic market names
   that no rule understood; such markets are never bet. See
   [Unmapped markets](#unmapped-markets).

## 8. Going live (real bets)

You decided to go live without a long test period. When the smoke test above looks right:

1. Stop the bot (`stop.cmd`).
2. Open `.env` (`notepad C:\cs2bot\.env`) and change the line to

   ```
   CS2BOT_EXECUTOR__DRY_RUN=false
   ```

   and save.
3. Start the bot (`start.cmd`). The window shows **"LIVE MODE"** in yellow, and the dashboard's
   dry-run indicator disappears.

To go back to simulation at any time, set it to `true` again and restart. To stop all betting
immediately without stopping the bot, use the **kill switch** on the dashboard (or
`uv run cs2bot control kill-switch on`).

**The risks, plainly:**

- **No system wins every bet.** CS2 has real variance (clutches, eco-round upsets, overtime, off
  days). The bot only bets when its calibrated probability is high (75%+; since 2026-11-24 the odds are
  not a criterion, so at low odds the 12% stake tax can make a bet lose money on average), and
  high-probability bets still lose regularly, and losing streaks happen. Judge it over hundreds
  of bets (ROI, closing-line value), never over a few days.
- **Betclic's terms of service forbid bots and automated betting.** If Betclic detects it, it can
  limit or close the account and **withhold the balance**. This is the account you use, so keep
  only the bot's bankroll on it, keep the number of bets per day low and do not run several
  automations on it. (Betting with a licensed operator is legal in Poland; automation is a matter
  between you and Betclic. This is not legal advice.)
- **Polish tax.** Betclic deducts **12% of every stake** (a 100 PLN stake plays as 88 PLN), and
  winnings above 2280 PLN from one bet are taxed a further 10%, withheld by Betclic. The bot's
  expected-value calculation already includes both; Betclic's "bez podatku" promotions can be
  switched on with `CS2BOT_BETCLIC__TAX_FREE_PROMO=true` while they last. This is not tax advice.
- **The website changes.** After a Betclic redesign the bot may fail to find buttons. It then stops
  that bet *before* any money is at risk and shows an alert; it does not guess.
- **18+ only. Gamble responsibly.** Decide your bankroll in advance and never top it up to chase
  losses. Betclic offers deposit limits and self-exclusion in the account settings. If betting stops
  being fun, get help - in Poland, e.g. through the national addiction-prevention centre (Krajowe
  Centrum Przeciwdziałania Uzależnieniom, kcpu.gov.pl).

## 9. Keep the PC awake before matches

The bot only works while the PC is **on, signed in and awake**. Pre-match bets are placed in the
hours before each match, and the data and odds jobs need time before that. Most Tier 1/2 matches
are played between late morning and late evening Warsaw time.

- While `start.cmd` runs the bot, Windows does **not** go to sleep on its own (the screen may still
  turn off - that is fine). This does not prevent sleep when you close a laptop lid, press the power
  button or choose Sleep.
- **Locking the screen (Win+L) is fine.** Signing out or shutting down stops the bot.
- **`deploy\windows\power-settings.cmd`** shows your sleep settings and, **only if you confirm**, can:
  turn off automatic sleep on mains power (saves your old values to restore later), allow wake
  timers, and create a daily wake-up that wakes the PC from sleep at a time you choose and starts
  the bot. Trade-offs: more electricity, and the PC switches itself on at that time. Some PCs with
  "Modern Standby" ignore wake timers - test it once.
- After the PC was off, the scheduler **catches up**: every job that missed its time runs once right
  away (see [OPERATIONS.md](OPERATIONS.md#catch-up-after-the-pc-was-off)).

## 10. Start automatically at sign-in

Double-click **`deploy\windows\register-autostart.cmd`** (add `-StartNow` in PowerShell to start it
immediately).

*What it does:* creates a Windows Task Scheduler entry named **cs2bot** that runs `start.ps1` in the
background one minute after you sign in. It runs as you, without administrator rights, with no
visible window (a window may flash for a moment), and writes everything to `logs\`. If the bot
crashes, it is restarted automatically (after 30 s, then 1, 2, 4, 8, 10 minutes; it gives up after 5
crashes within an hour - check the log then).

- Stop the background bot: `stop.cmd` (it stays stopped until the next sign-in, or until you run
  `start.cmd`).
- Remove the autostart: `unregister-autostart.cmd`.
- Autostart begins at **sign-in**, not at power-on. If you want the PC to sign in by itself after a
  power cut, Windows can do that (automatic sign-in), but anyone with access to the PC then gets
  into your account and Betclic session - only do it if the PC is physically safe.

## 11. Backups

The database holds the bet history, the money ledger and years of collected match data. Back it
up:

- **Now:** double-click `deploy\windows\backup-db.cmd`. It writes
  `backups\cs2bot-YYYYMMDD-HHMMSS.dump`, checks the file can be read back, and deletes backups
  older than 14 days (the newest 3 are always kept). The bot may keep running.
- **Every day automatically:** in PowerShell in `C:\cs2bot`:

  ```powershell
  deploy\windows\backup-db.cmd -Schedule 04:30
  ```

  (runs at 04:30, or soon after you next sign in if the PC was off; results in `logs\backup.log`;
  remove with `-Unschedule`).
- **Copy the `backups` folder to an external drive or a cloud folder now and then.** A dead disk
  takes the database and its backups with it.
- **Restore:** stop the bot, then `deploy\windows\restore-db.cmd` (newest backup) or
  `restore-db.cmd -BackupFile backups\cs2bot-....dump`. You must type `RESTORE` to confirm; a
  safety backup of the current database is made first. Bets placed on Betclic after the backup
  are not in the restored database - compare with "Moje zakłady".

## 12. Updating

Double-click **`deploy\windows\update.cmd`**.

*What it does:* stops the bot (after asking), backs up the database, downloads the new version
(`git pull`), updates the Python packages, the bot's Chromium, the database tables and the
dashboard, then starts the bot again if it was running. If you edited files of the repository
yourself (for example `config\betclic_selectors.yaml`), they are put aside before the update and
re-applied afterwards (`git stash`); you are told if they conflict. Your `.env`, logs, backups and
`data\` folder are never touched.

After an update, compare `.env` with `.env.example` - new settings may have been added (the
defaults apply until you set them).

## 13. Troubleshooting

First look at the newest file in `logs\` and at **Dashboard > Health**. `uv run cs2bot doctor`
checks most of the setup in one go.

### Docker is not running / "Docker did not become ready"

- Open Docker Desktop from the Start menu and wait for "Engine running" (the first start after
  Windows boots can take a few minutes), then start the bot again.
- "WSL 2 installation is incomplete" / "Virtualization not enabled": run `wsl --update` in
  PowerShell, restart Windows, and make sure virtualisation is enabled in the BIOS/UEFI.
- Docker Desktop asks you to accept new terms after an update: open it once and accept.

### Port 5432 is in use

Another PostgreSQL (installed separately at some point) already uses port 5432. Either stop that
service (Services app > `postgresql-x64-..` > Stop, startup type Manual), or move the bot's
database to another port: add `POSTGRES_PORT=5433` to `.env` **and** change `:5432/` to `:5433/`
in `CS2BOT_DATABASE__URL`, then start again.

### "Executable doesn't exist" / the bot's browser is missing

The bot's Chromium is not installed (or a Playwright update needs a newer one). In `C:\cs2bot`:

```powershell
uv run playwright install chromium
```

### HLTV blocks the bot (Cloudflare "Just a moment...", HTTP 403/429)

HLTV protects its site with Cloudflare. The bot never tries to trick it: when a challenge does not
clear by itself, it backs off (15 minutes, doubling up to 12 hours) and the Health page shows the
HLTV jobs as blocked.

- **Slow down.** Raise `CS2BOT_HLTV__MIN_DELAY_S` / `CS2BOT_HLTV__MAX_DELAY_S` (e.g. `8` / `15`).
  The big history backfill is where blocks happen most.
- **Pass the check by hand once:** stop the bot, run `uv run cs2bot ingest hltv-browser` - it opens
  HLTV in a visible window with the bot's own browser profile; complete the check, close the window
  and start the bot. The clearance cookie is kept in `data\browser\hltv`.
- `CS2BOT_HLTV__HEADLESS=false` makes the HLTV browser a normal visible window, which Cloudflare
  challenges less often (it opens on your desktop while HLTV pages load).
- For the large backfill, an optional **residential proxy** (`CS2BOT_HLTV__PROXY_URL`) keeps the
  load off your home IP - otherwise an HLTV block also affects your normal browsing of HLTV.

### Betclic selectors broken after a site update

Symptoms: alerts like "selector error", bets ending as `SELECTOR_ERROR`, `cs2bot executor health`
reporting elements as not found. The bot stops each such bet **before** the final click and saves a
screenshot plus the page HTML in `data\screenshots\`. The page elements the bot looks for are listed
in **`config\betclic_selectors.yaml`**; the top of that file explains, step by step, how to find a new
selector with Chrome's developer tools (F12) and test it. After editing: restart the bot, run
`uv run cs2bot executor health` and watch one dry-run bet. Until it is fixed, switch on the kill
switch or the dry run.

### Login expired / "Betclic login required"

The bot paused itself because Betclic logged it out. Run `betclic-login.cmd` (or press "Betclic
login" on the dashboard), log in incl. 2FA, then press **Resume** on the dashboard. If it happens
every day, make sure "remember me" is ticked at the login.

### Unmapped markets

A Betclic market whose name no rule understands is stored but **never bet**.
`uv run cs2bot odds unmapped` lists them (most frequent first). The rules are in
**`config\betclic_markets.yaml`** (Polish market names, explained at the top of the file); add or
adjust a rule, restart the bot, and the collector maps the market on its next pass. A wrongly mapped
market is worse than an unmapped one - when in doubt, leave it unmapped.

### "Running scripts is disabled on this system"

Use the `.cmd` launchers (they allow the script for that one run), or run
`powershell -ExecutionPolicy Bypass -File deploy\windows\<script>.ps1`.

### "The bot is already running" / "Another start.ps1 is already starting the bot"

`start.cmd` found a running bot (maybe the autostart one, which has no window). The dashboard is at
http://127.0.0.1:8000; stop it with `stop.cmd`.

Only one start script runs at a time, so two bots can never run side by side. "Another start.ps1 is
already starting the bot" means the autostart task is still waiting for Docker Desktop after
sign-in (it waits up to 15 minutes), or an update / restore is running. Wait a few minutes and open
the dashboard; if nothing comes up, look at the newest file in `logs\`, run `stop.cmd` and start
again.

### The dashboard shows a notice instead of the web page

The dashboard was not built (Node.js missing during installation). Run `install.cmd` again.

### The PC is slow or runs out of memory

The database is tuned for a PC with 16 GB RAM. On 8 GB add these lines to `.env` and restart:

```
POSTGRES_SHARED_BUFFERS=512MB
POSTGRES_EFFECTIVE_CACHE_SIZE=1500MB
```

Then run `docker compose up -d` in `C:\cs2bot` (or just restart the bot).

## 14. Uninstalling

1. `unregister-autostart.cmd` (in PowerShell add `-All -StopBot` to also remove the backup / wake
   entries and stop the bot).
2. Back up what you want to keep (`backup-db.cmd`, then copy `backups\`).
3. Delete the database: in `C:\cs2bot` run `docker compose down -v` (**this deletes all data**).
4. Delete the `C:\cs2bot` folder. Docker Desktop, Git, Node.js, 7-Zip and uv can be removed in
   Windows Settings > Apps.
