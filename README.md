# CS2 Esports Analyzer (cs2bot)

A pre-match analyzer for **Counter-Strike 2 Tier 1/2 matches** that prices every CS2 market on
**Betclic.pl** with a statistical / simulation model and bets automatically - only when the model
is confident **and** the price has positive expected value after the Polish 12% stake tax.

It runs **entirely on the owner's Windows PC**: PostgreSQL in Docker Desktop, the Python app natively
(so the Betclic login can happen in a real, visible browser window), and a local dashboard at
http://127.0.0.1:8000.

## What it does

- **Collects data:** PandaScore (schedules, line-ups, results), HLTV (matches, vetoes, map and player
  stats, rankings, news) and HLTV demo files (rounds, kills, economy, positions, duels).
- **Models every match** down to maps, vetoes, rounds and players: per-map ratings, a veto model, a
  round-by-round economy simulator, player kill projections and an ensemble with calibration.
  Claude reads the news and adds bounded context adjustments and a written rationale for each bet.
- **Prices every Betclic market** (match / map winners, handicaps, totals, rounds, pistols, player
  kills, ...) and compares the model with the odds, after tax.
- **Bets selectively:** probability, EV, uncertainty and data-quality thresholds; 1/4 Kelly stakes
  with hard caps, daily / weekly loss limits, a kill switch, pause / resume, dry run.
- **Places bets** on the owner's Betclic account through a dedicated browser profile, settles them,
  tracks the ledger, ROI and closing-line value, and shows everything on the dashboard.

## Honest constraints

- **No system wins every bet.** CS2 has real variance; even a true 90% outcome loses one time in ten.
  Success means calibrated probabilities and long-run profit (Brier score, ROI, CLV), not a perfect
  record. Losing streaks will happen.
- **Betclic's terms forbid bots and automated betting.** If detected, Betclic can restrict or close
  the account and withhold the balance. The owner has accepted this risk. Not legal advice.
- **The bookmaker margin (6-10%) and the 12% stake tax** make most bets unprofitable; the bot
  analyses everything but bets rarely.
- **HLTV has no official API** and is protected by Cloudflare; scraping is slow and polite and can
  still be blocked. Betclic's website changes break selectors from time to time.
- **The PC must be on and awake** before matches.
- **18+ only. Gamble responsibly**, and only with money you can afford to lose.

## Architecture

```
 PandaScore   HLTV pages   HLTV demos   News   Betclic odds (browser)
      \           |            |          |          |
       +----- ingestion jobs (built-in scheduler, catch-up after the PC was off) ----+
                                   |
                         PostgreSQL (Docker Desktop)
                                   |
   datasets -> features -> ratings / veto / round sim / kill props / ensemble -> calibration
                                   |                     ^
                       Claude context layer (news flags, rationale)
                                   |
   pricing (every market) -> decision rules -> bankroll (1/4 Kelly, caps, loss limits)
                                   |
   Betclic executor (Playwright, visible window, dry run)    API + React dashboard (127.0.0.1)
```

| Document | Content |
|---|---|
| [PLAN.md](PLAN.md) | Scope, decisions, constraints, model design, build phases |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Package layout, module contracts, CLI names |
| [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) | Step-by-step installation on the owner's Windows PC |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | What runs when, safety controls, daily routine, tuning, backups |
| [docs/API.md](docs/API.md) | Local dashboard API |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Dashboard (React) development and build |
| [docs/dev/](docs/dev/) | Build status, work-package briefs and implementation reports |

## Quick start (Windows)

Full guide with explanations: [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md).

```powershell
winget install --id Git.Git -e                 # then open a new PowerShell window
git clone <repository URL> C:\cs2bot
cd C:\cs2bot
deploy\windows\install.cmd                     # Docker Desktop, Node.js, 7-Zip, uv, .env, database, dashboard
deploy\windows\betclic-login.cmd               # log in to Betclic once in the bot's browser (incl. 2FA)
deploy\windows\start.cmd                       # start; dashboard at http://127.0.0.1:8000
```

The bot starts in **dry run** (`CS2BOT_EXECUTOR__DRY_RUN=true` in `.env`: everything except the final
click). Going live: [WINDOWS_SETUP.md, section 8](docs/WINDOWS_SETUP.md#8-going-live-real-bets).

Other helpers in `deploy\windows\`: `stop`, `backup-db` (`-Schedule 04:30` for daily backups),
`restore-db`, `update`, `register-autostart` / `unregister-autostart`, `power-settings`.

## CLI cheat sheet

Run as `uv run cs2bot <command>` from the repository folder.

| Command | Purpose |
|---|---|
| `cs2bot up` | Start the API + dashboard on 127.0.0.1 and the built-in scheduler (one command) |
| `cs2bot doctor` | Check DB, migrations, Chromium, API keys, archive tool, disk space, config files, dry-run state |
| `cs2bot db upgrade` / `db seed-synthetic` | Migrations / synthetic demo data |
| `cs2bot ingest upcoming` / `results` / `backfill --since 2023-09-27` / `rankings` / `news` / `match <hltv_id>` | Data ingestion |
| `cs2bot ingest hltv-browser` | Open HLTV in a visible window with the bot's browser profile (pass a challenge by hand) |
| `cs2bot demos process --limit N` | Download + parse pending demos |
| `cs2bot odds collect [--once]` / `odds unmapped` | Betclic odds collection / list unmapped market names |
| `cs2bot models train` / `models tune` | Fit ratings, veto, round, kill, ensemble models |
| `cs2bot predict upcoming` / `predict match <id>` | Produce match predictions + priced outcomes |
| `cs2bot bet run [--dry-run]` | Decide + stake + execute for upcoming matches |
| `cs2bot settle` | Settle finished bets, closing odds, CLV, ledger |
| `cs2bot backtest --from YYYY-MM-DD --to YYYY-MM-DD` | Walk-forward backtest |
| `cs2bot executor login` / `executor health` / `executor balance` | Open the Betclic login window / checks |
| `cs2bot control kill-switch on\|off` / `control pause` / `control resume` | Safety controls |

## Development

Python 3.12 managed by [uv](https://docs.astral.sh/uv/); Node.js LTS for the dashboard. Linux/macOS
helpers live in `scripts/`.

```bash
uv sync                                  # install everything (incl. dev tools)
scripts/dev.sh                           # PostgreSQL in Docker + migrations + cs2bot up
scripts/dev.sh --db-only --seed          # database with synthetic demo data only
scripts/test.sh                          # ruff check, ruff format --check, mypy, pytest
scripts/test.sh --pg                     # ... plus the PostgreSQL tests (throw-away database)

uv run pytest -q -n 4                    # tests
uv run ruff check src tests              # lint
uv run ruff format --check src tests     # formatting
uv run mypy                              # types

cd dashboard && npm ci && npm run dev    # dashboard dev server (proxies /api to 127.0.0.1:8000)
```

Conventions: UTC-aware datetimes, `Decimal` for money and odds, `team1`/`team2` always in the
match's own orientation, no network access in tests (fixtures, respx, fakes), secrets never logged.
See [CLAUDE.md](CLAUDE.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

CI (`.github/workflows/ci.yml`): lint + types, tests on Linux (with PostgreSQL and Chromium) and
Windows, and the dashboard build.
