# ops: implementation report

Validation (no Python tests in this package): `.venv/bin/python -m pytest tests/foundation -n 2` -> **37 passed, 0 failed**; `docker compose -f docker-compose.yml config -q` OK; `.github/workflows/ci.yml`, `docker-compose.yml` and `config/*.yaml` parse with `yaml.safe_load`; `bash -n scripts/dev.sh scripts/test.sh` OK; all `deploy/windows/*.ps1` pass a static check (pure ASCII, balanced brackets outside strings/comments, no PowerShell 7-only syntax).

## Summary

P11 makes cs2bot installable and operable by a non-developer on the owner's Windows 10/11 PC, fully local, as PLAN.md specifies: Docker Desktop runs only PostgreSQL, the Python app runs natively via uv, the dashboard is served by `cs2bot up` on http://127.0.0.1:8000.

An earlier engineer had written `docker-compose.yml`, `deploy/windows/common.ps1`, `install.ps1`, `start.ps1` and `send-ctrl-c.ps1`. I reviewed them, kept what was correct and fixed or extended the rest:

- **common.ps1:** container check by exact name (`docker inspect`; a `docker ps --filter name=` filter is a regex). `Invoke-Checked` now pipes to `Out-Host`, so a program's output can never become a caller's return value. `Invoke-ProcessToFile` resolves full program paths and rejects arguments containing spaces. New helpers for Task Scheduler (`New-ScriptTaskAction`, `Register-UserTask`) and windows.
- **start.ps1:** a supervisor loop (`-Background` / `-RestartOnCrash`) restarts a crashed bot with back-off (30 s, then 1, 2, 4, 8 and 10 min), at most 5 times per hour. Windows Task Scheduler's own "restart on failure" only fires when a task cannot be *launched*, not when it exits with an error. The migrations output now goes to the log. A stop requested while Docker is still starting is honoured. Console output is UTF-8 in both modes.
- **New scripts:** `stop`, `betclic-login`, `backup-db`, `restore-db`, `update`, `register-autostart`, `unregister-autostart` and `power-settings`, plus double-click `.cmd` launchers for every script.
- **Also new:** `scripts/dev.sh` and `scripts/test.sh` (Linux/macOS), the CI workflow, `docs/WINDOWS_SETUP.md`, `docs/OPERATIONS.md`, a rewritten `README.md` and a root `CLAUDE.md`.
- **Line endings:** per-directory `.gitattributes` force CRLF for `*.ps1` / `*.cmd` and LF for `*.sh`.

Design decisions worth knowing:

- **Hidden autostart:** the task runs `conhost.exe powershell.exe -WindowStyle Hidden -File start.ps1 -Background`. With Windows Terminal as the default terminal (Windows 11), `powershell -WindowStyle Hidden` alone leaves a visible window (microsoft/terminal#12464). Starting conhost explicitly avoids the hand-off, and the bot still gets a (hidden) console, which a graceful Ctrl+C stop needs. VBScript wrappers were avoided because Microsoft is deprecating VBScript.
- **Graceful stop:** `stop.ps1` writes `data\run\stop.requested` and sends Ctrl+C to the bot's console (helper `send-ctrl-c.ps1`: FreeConsole / AttachConsole / GenerateConsoleCtrlEvent, run with CREATE_NO_WINDOW). It waits up to 120 s, then ends the process tree with `taskkill /T /F`. `start.ps1` reads the marker: no restart and exit code 0. stop.ps1 never deletes the marker (that would race with start.ps1); start.ps1 deletes it when it acts on it and at every start.
- **Backups:** `pg_dump --format=custom` runs inside the container and streams straight to a file through `Start-Process -RedirectStandardOutput` (binary-safe in PS 5.1). It is verified with `pg_restore --list` before it gets its final name. Restore copies the file into the container (`docker cp`), runs `dropdb --force` and `createdb`, then `pg_restore --exit-on-error`. A seekable file avoids the "could not find block ID" problem of restoring a dump that was written to a pipe.
- **Betclic login while the bot runs:** `cs2bot executor login` cannot open the profile while `cs2bot up` holds it, so `betclic-login.ps1` calls `POST /api/control/betclic-login` and polls `GET` until it finishes.
- **CI action versions:** `actions/checkout@v7`, `actions/setup-node@v7` and `actions/cache@v6` use floating major tags. `astral-sh/setup-uv` no longer publishes major tags, so it is pinned to `v10.3.3` (Oct 2026; v10.4.0 is only days old).

## Public API

Everything lives in `deploy/windows/` (PowerShell 5.1, `Set-StrictMode -Version Latest`, `$ErrorActionPreference='Stop'`, ASCII only). Each `X.ps1` has an `X.cmd` launcher (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0X.ps1" %*`; it pauses on error, or always for the interactive ones). Exit codes are 0 for success and 1 for failure unless noted.

| Script | Parameters | Does |
|---|---|---|
| `install.ps1` | `[-Yes] [-SkipPrerequisites] [-NoEditEnv]` | winget: Git, Docker Desktop, Node.js LTS, 7-Zip; uv via astral.sh installer; Docker start; `.env` from `.env.example` with random DB password (`POSTGRES_PASSWORD` appended, URL patched); `uv sync --frozen`; `uv run playwright install chromium`; `docker compose up -d` + wait healthy; `uv run cs2bot db upgrade`; dashboard `npm ci` + `npm run build` (if `dashboard/package.json`); `uv run cs2bot doctor` (warning only). Exit 2 = Docker Desktop just installed, reboot and rerun. |
| `start.ps1` | `[-Background] [-RestartOnCrash] [-MaxRestartsPerHour 5] [-AllowSleep] [-NoBrowser] [-LogRetentionDays 30]` | No-op if the bot runs; Docker + Postgres healthy; `cs2bot db upgrade` (logged); `cmd /d /c "uv run cs2bot up 2>&1"` with output to `logs\cs2bot-YYYY-MM-DD.log` (daily files, age retention); keep-awake via `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)` while running; supervisor restarts in background mode. |
| `stop.ps1` | `[-TimeoutSeconds 120] [-Force] [-NoForce] [-StopDatabase]` | Stop marker + Ctrl+C to the bot's console, wait, then force-kill the tree; optional `docker compose stop`. |
| `send-ctrl-c.ps1` | `-ProcessId <int>` | Internal helper of stop.ps1 (exit 0 sent, 2 attach failed, 3 generate failed). |
| `betclic-login.ps1` | `[-TimeoutMinutes 15]` | Bot running -> `POST /api/control/betclic-login` + poll `GET`; else `uv run cs2bot executor login`. |
| `backup-db.ps1` | `[-KeepDays 14] [-KeepAtLeast 3] [-Label word] [-NoPrune] [-Schedule HH:mm] [-Unschedule] [-Background]` | `backups\cs2bot-YYYYMMDD-HHMMSS[-label].dump`, verified, pruned; `-Schedule` registers daily task `cs2bot-backup` (StartWhenAvailable); log `logs\backup.log`. |
| `restore-db.ps1` | `[-BackupFile path] [-List] [-NoSafetyBackup] [-Force]` | Refuses while the bot runs; typed `RESTORE` confirmation; safety backup `-before-restore`; drop/create/restore; `cs2bot db upgrade`. |
| `update.ps1` | `[-Yes] [-NoBackup] [-NoRestart]` | Stops the bot; `git stash` of local edits (asked); backup `-before-update`; `git pull --ff-only`; `stash pop`; `uv sync --frozen`; playwright chromium; `compose up -d`; migrations; dashboard; restarts the bot (via task `cs2bot` if registered, else new window). |
| `register-autostart.ps1` | `[-DelayMinutes 1] [-Visible] [-StartNow]` | Task `cs2bot`: AtLogOn (current user, Interactive, Limited), delay, no time limit, priority 5, IgnoreNew, StartWhenAvailable, battery OK, RestartCount 3 / 5 min; action `conhost.exe powershell ... start.ps1 -Background` (or `-RestartOnCrash -NoBrowser`, minimised, with `-Visible`). |
| `unregister-autostart.ps1` | `[-StopBot] [-All]` | Removes task `cs2bot` (`-All`: also `cs2bot-backup`, `cs2bot-wake`). |
| `power-settings.ps1` | `[-ShowOnly]` | Shows sleep / hibernate / wake-timer values (locale-independent `powercfg /query` parsing) and the wake task; menu, each change confirmed (default No): never sleep on AC (old values saved to `data\run\power-settings-backup.json`), restore, wake timers on AC, daily wake task `cs2bot-wake` (WakeToRun, runs `start.ps1 -Background`), remove it. DC values are never changed. |
| `common.ps1` | dot-sourced | Paths (`$RepoRoot`, `$LogDir`=logs, `$BackupDir`=backups, `$RunDir`=data\run, `$StopMarker`), names (`cs2bot-postgres`, `cs2bot_pgdata`, tasks `cs2bot` / `cs2bot-wake`), sets `UV_FROZEN=1`, `PYTHONUTF8=1`, `PYTHONUNBUFFERED=1`; helpers `Invoke-Checked`, `Invoke-Quiet`, `Get-NativeOutput`, `Invoke-ProcessToFile`, `Get-EnvFileValue`, `Start-DockerDesktop`, `Start-Postgres`, `Get-BotProcess`, `Stop-ProcessTree`, `Build-Dashboard`, `New-ScriptTaskAction`, `Register-UserTask`, ... |

Runtime folders `logs\`, `backups\` and `data\run\` are created on demand, each with its own `.gitignore` (`*`).

**Other files**

- `docker-compose.yml`: service `postgres`, image `postgres:16.16-bookworm`, container `cs2bot-postgres`, `127.0.0.1:${POSTGRES_PORT:-5432}:5432`, volume `cs2bot_pgdata`, `pg_isready` healthcheck over TCP, `restart: unless-stopped`, UTF8 / `C.UTF-8` / data checksums, UTC. Desktop tuning comes from `.env`: `POSTGRES_SHARED_BUFFERS` (1GB), `POSTGRES_EFFECTIVE_CACHE_SIZE` (3GB), `POSTGRES_WORK_MEM` (32MB), `POSTGRES_MAINTENANCE_WORK_MEM` (256MB); also `max_connections=50`. Credentials are `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`, defaulting to `cs2bot` / `change-me` / `cs2bot`, which matches `.env.example`'s `CS2BOT_DATABASE__URL`.
- `scripts/dev.sh [--db-only] [--seed] [--stop]`: `compose up -d --wait`, `uv sync --frozen`, `cs2bot db upgrade`, optional `db seed-synthetic`, `exec uv run cs2bot up`.
- `scripts/test.sh [--quick] [--fix] [--pg] [-- pytest args]`: ruff check, format --check, mypy, then pytest (`-n ${PYTEST_WORKERS:-auto}`). `--pg` creates `cs2bot_test` in the compose Postgres, runs the non-postgres tests in parallel and the postgres tests serially (`-p no:xdist`). The `pg_session` fixture drops and re-creates the schema of a single database, so these tests must never run in parallel.
- `.github/workflows/ci.yml`, run on push to main, on pull requests and by hand, with concurrency group per ref and cancel-in-progress:
  - `lint`: ruff check, ruff format --check, mypy.
  - `test-linux`: a postgres:16.16 service with `CS2BOT_TEST_DATABASE_URL=postgresql+psycopg://cs2bot:cs2bot@localhost:5432/cs2bot_test`, `playwright install --with-deps chromium` (browsers cached by `uv.lock` hash) and a symlink `/opt/pw-browsers/chromium` plus `PLAYWRIGHT_CHROMIUM_EXECUTABLE`. It runs `-m "not postgres"` with `-n auto`, then `-m postgres` serially.
  - `test-windows`: `-m "not browser and not postgres"`.
  - `dashboard`: skipped with a notice when `dashboard/package.json` is missing; otherwise Node `lts/*`, `npm ci`, `npm run lint --if-present`, `npm run test --if-present`, `npm run build`.

**Contract the scripts rely on (for the integration engineer, P12)**

1. `uv run cs2bot up` runs in the foreground until stopped and logs to stdout/stderr (start.ps1 captures both into `logs\`). Do not add a separate log file. On **Ctrl+C (SIGINT / KeyboardInterrupt) it shuts down cleanly within 90 s**: start.ps1 force-kills it after that, and stop.ps1 after 120 s. Exit code 0 means a deliberate, normal end. **A non-zero exit means a crash**: under `-Background` it is restarted with back-off.
2. Bot processes are found by command line matching `cs2bot(\.exe|\.cli)?["']?\s+up(\s|"|$)` among python/pythonw/cs2bot/uv/cmd processes. Keep `up` as the command name; `python -m cs2bot.cli up` also matches.
3. `uv run cs2bot executor login` exits 0 only when the login completed. It is only called while the bot is NOT running.
4. `uv run cs2bot doctor` exits non-zero when it finds problems (install.ps1 shows a warning).
5. `uv run cs2bot db upgrade` is idempotent and is called on every start.
6. API: `GET /api/ping` -> `{"status":"ok"}`. `POST /api/control/betclic-login` (with header `X-Requested-With: cs2bot`) returns 202 / 409 / 501 / 401, and `GET` returns `running`, `last_result` and `last_error`. A `login_launcher` must be wired in `run_up` for the running-bot path.

Wiring example (owner):

```powershell
deploy\windows\install.cmd
deploy\windows\betclic-login.cmd
deploy\windows\register-autostart.ps1 -StartNow
deploy\windows\backup-db.ps1 -Schedule 04:30
```

### Self-review checklist (PowerShell scripts)

PowerShell is not available on this machine, and running a portable pwsh binary was not permitted. The scripts were therefore checked statically: a tokenizer for strings, here-strings and comments, with bracket balance, an ASCII check and a scan for PowerShell 7-only syntax. I also reviewed them by hand against the items below:

- [x] Windows PowerShell 5.1 syntax only: no `&&` / `||` / `??` / `?.`, no ternary, no `-AsByteStream`, no `$IsWindows`, `Join-Path` with two arguments, no `clean {}` blocks.
- [x] Pure ASCII in every `.ps1` (PS 5.1 reads BOM-less files as ANSI). `.gitattributes` forces CRLF for `.ps1` / `.cmd`.
- [x] `Set-StrictMode -Version Latest` and `$ErrorActionPreference = 'Stop'` in every script. `$global:LASTEXITCODE` is initialised. No undefined variables: every Verb-Noun call resolves to a defined function or a built-in cmdlet (cross-checked by grep).
- [x] Every native call is checked through `$LASTEXITCODE` (`Invoke-Checked`, `Invoke-Quiet`, `Get-NativeOutput`, `Invoke-ProcessToFile`, or an explicit check after `& uv` / `& git`). stderr is never redirected under `Stop` (that would throw in PS 5.1), except inside helpers that switch to `Continue` or merge with `cmd /c ... 2>&1`.
- [x] Functions whose return value is used emit nothing else to the pipeline: native output goes to `Out-Host`, `.NET` results to `[void]`, `Start-Process` to `| Out-Null`.
- [x] Paths are built with `Join-Path` and tested with `Test-Path -LiteralPath`. Quoted `-File "..."` arguments and `"%~dp0..."` handle paths with spaces. Arguments to `Start-Process -ArgumentList` contain no spaces (this is enforced).
- [x] Binary data (pg_dump) never passes through a PowerShell pipeline or `>`.
- [x] Idempotent: installer steps are skipped when done; `.env` is never overwritten; tasks are registered with `-Force`; start is a no-op when the bot runs; stop is a no-op when it does not; folders are created on demand.
- [x] Destructive actions ask first. Restore requires typing `RESTORE` (case-sensitive) and makes a safety backup. Update asks before stopping the bot and before stashing. Every power-settings change asks with default No. Retention never deletes the newest N backups, and the safety backup before a restore uses `-NoPrune`, so it cannot delete the file being restored.
- [x] Secrets are never printed. `.env` is read only for non-secret values or to check that a value is filled in. The generated DB password goes only into `.env`.
- [x] Ctrl+C safety: the `finally` blocks use .NET calls, the log writer is disposed, the keep-awake request is cleared, and background mode exits via `[Environment]::Exit` with the right code.
- [x] Locale independence: no parsing of translated text (powercfg values are read from their hex numbers, Docker state with `--format` templates).
- [x] No administrator rights needed. "Access denied" from Task Scheduler is explained, and the scripts warn when run elevated.

## Known limitations

- **Scripts not run on Windows:** none of the PowerShell scripts were executed (no Windows or PowerShell here; running a portable pwsh in the scratchpad was denied). They have only been checked statically and by review, so the first run on the owner's PC is their real test. The riskiest parts are Ctrl+C delivery to a console started by Task Scheduler, `Start-Process` stdin/stdout redirection for pg_dump / pg_restore, and `conhost.exe` launch behaviour. If Ctrl+C does not reach the bot, stop.ps1 falls back to a forced end after 120 s, which is safe for money: bets are committed before the click and reconciled afterwards.
- **Integration commands missing:** `cs2bot up`, `doctor`, `executor login`, `ingest hltv-browser` and the other CLI commands come from the integration package and do not exist yet. Until then `start.ps1 -Background` retries with back-off and gives up after 5 failures per hour, and `install.ps1` reports the missing doctor as a warning.
- **Wake-up:** the daily wake task needs wake timers enabled and the user signed in (sleep, not shut down). Some "Modern Standby" PCs ignore wake timers. Autostart begins at sign-in, not at power-on.
- **Supervisor scope:** a hard crash of `start.ps1` itself (e.g. the hidden console being closed) is not restarted. Task Scheduler's own restart setting only covers launch failures.
- **CI never run:** the workflow has not run on GitHub (no access from here). The action versions were checked against the actions' git tags. The dashboard job uses `--if-present` for `lint` / `test`, so missing scripts are skipped rather than failing.
- **Manual pins:** the Postgres image tag (`16.16-bookworm`) and `setup-uv@v10.3.3` must be bumped by hand.
- **Update edge cases:** `update.ps1` needs a branch with an upstream. A `git stash pop` conflict leaves resolution to the owner; the stash is kept and the message says so.
- **Restore requirement:** restore needs `POSTGRES_USER` to be a superuser (true for the official image) because it uses `dropdb --force` / `createdb`.

## Foundation change requests

1. **`.gitignore`** (root): add `logs/` and `backups/`. The scripts already put a `*` `.gitignore` into each runtime folder they create, but a root entry protects database dumps even if a folder is created by hand.
2. **`.env.example`**: add a commented block documenting the variables Docker Compose reads from `.env`. install.ps1 appends `POSTGRES_PASSWORD` itself.

   ```
   # --- PostgreSQL container (docker-compose.yml) ---
   # POSTGRES_USER=cs2bot
   # POSTGRES_PASSWORD=change-me   # must equal the password in CS2BOT_DATABASE__URL
   # POSTGRES_DB=cs2bot
   # POSTGRES_PORT=5432            # also change the port in CS2BOT_DATABASE__URL
   # POSTGRES_SHARED_BUFFERS=1GB   # 512MB on 8 GB RAM PCs
   # POSTGRES_EFFECTIVE_CACHE_SIZE=3GB
   ```

   Consider also `# CS2BOT_HLTV__HEADLESS=false` with a comment (fewer Cloudflare challenges on the desktop).
3. **`src/cs2bot/config.py`** `DatabaseSettings.url` default is `postgresql+psycopg://cs2bot:cs2bot@localhost:5432/cs2bot`. It disagrees with `docker-compose.yml` / `.env.example` (password `change-me`, host `127.0.0.1`). Align it to `postgresql+psycopg://cs2bot:change-me@127.0.0.1:5432/cs2bot` so a missing `.env` line still reaches the compose database. `127.0.0.1` also avoids an IPv6 `::1` attempt on Windows, where the port is published on IPv4 loopback only.
4. **Browser tests** in `tests/ingest/hltv/conftest.py`, `tests/ingest/demos/test_demos_browser.py`, `tests/odds/test_odds_browser.py`: read the Chromium path from `PLAYWRIGHT_CHROMIUM_EXECUTABLE` (default `/opt/pw-browsers/chromium`), as `tests/executor/conftest.py` already does. CI currently works around this with a symlink at `/opt/pw-browsers/chromium`.
5. **Integration (P12)**: honour the "Contract the scripts rely on" above. In particular: clean Ctrl+C shutdown within 90 s on Windows, exit code 0 only for a normal end, `executor login` exit code, and wiring `login_launcher` in `run_up` so `betclic-login.ps1` works while the bot runs. Optionally treat `signal.SIGBREAK` like SIGINT in `run_up`; the scripts do not send Ctrl+Break, because it would put PowerShell 5.1 into its debugger.
6. **docs/ARCHITECTURE.md** P11 section (owner of that file): list the actual deliverables (`deploy/windows/*.ps1` + `.cmd`, `scripts/dev.sh`, `scripts/test.sh`, `docs/OPERATIONS.md`, root `CLAUDE.md`) and the `cs2bot up` contract above.

## New dependency requests

None. The Windows prerequisites (Docker Desktop, Git, Node.js LTS, 7-Zip, uv) are installed by `install.ps1` via winget or the official uv installer; no Python or npm packages were added.
