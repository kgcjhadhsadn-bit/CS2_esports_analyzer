# cloud_deploy: implementation report

Validation (no Python tests in this package): `.venv/bin/python -m pytest tests/foundation -n 2` -> **37 passed, 0 failed**; `docker compose -f docker-compose.cloud.yml config -q` OK (and fails with a clear message when a required `.env` key is missing); `bash -n deploy/linux/*.sh` OK; `ci.yml` / `docker-compose.cloud.yml` parse with `yaml.safe_load`; `ruff check` + `ruff format --check` + `mypy` clean on `deploy/linux/selftest.py`; entrypoint and self-test run for real on this machine (Xvfb + Chromium + PostgreSQL 16, see "Validation done").

## Summary

The bot can now be deployed on one Linux VPS in Poland (PLAN.md 0 / 0b) with Docker Compose: PostgreSQL, the bot (`cs2bot up` with a visible Chromium on Xvfb) and Caddy (HTTPS). The Windows install is unchanged.

Files (all new except `ci.yml`):

| File | What |
|---|---|
| `Dockerfile` | Multi-stage. `node:22.23.5-bookworm-slim` builds `dashboard/dist` (`npm ci && npm run build`); `python:3.12.16-slim-bookworm` + `uv 0.11.32` (`ghcr.io/astral-sh/uv`): `uv sync --frozen --no-dev` (deps layer, then the editable project), `playwright install --with-deps chromium` (the version in uv.lock, 1.65.2, into `/ms-playwright`), `tini`, `xvfb`, `x11vnc`, `unar` + `libarchive-tools` (bsdtar), `curl`, fonts. Non-root user `cs2bot` (uid/gid from build args `APP_UID` / `APP_GID`, default 10001), `HEALTHCHECK` on `/api/ping`, `ENTRYPOINT tini -- cs2bot-entrypoint`, `CMD up`. A wrapper `/usr/local/bin/cs2bot-chromium` (= Playwright's Chromium + `--disable-gpu`) is the default `CS2BOT_BETCLIC__CHROMIUM_EXECUTABLE` (see limitations). |
| `.dockerignore` | Allow-list: only `pyproject.toml`, `uv.lock`, `README.md`, `alembic.ini`, `src/`, `config/`, `dashboard/` (no `node_modules`/`dist`), the entrypoint and the self-test. `.env`, `data/`, `.git` never reach the build context. |
| `docker-compose.cloud.yml` | Project `cs2bot-cloud`. `postgres:16.16-bookworm` (same as the Windows file; volume `cs2bot_cloud_pgdata`, `pg_isready` healthcheck, internal network only, nothing published). `app` (build, `depends_on` postgres healthy, `restart: unless-stopped`, `env_file: .env` + fixed overrides: DB URL built from `POSTGRES_*` with host `postgres`, `CS2BOT_API__HOST=0.0.0.0`, port 8000, required password / session secret / `CS2BOT_PUBLIC_HOST`, `CS2BOT_EXECUTOR__HEADLESS=false`, `DISPLAY=:99`; bind mounts `./data`, `./logs`, `./config:ro`; tmpfs `/tmp`; `shm_size 1gb`; `cap_drop: ALL`, `no-new-privileges`; only `127.0.0.1:${VNC_HOST_PORT:-5900}` published for vnc.sh; healthcheck with a 15 min start period; `stop_grace_period 2m`). `caddy:2.11.9-alpine` (only 80/443 published; volumes for certificates). Networks: `backend` (internal, no internet) and `frontend`. json-file log rotation 20m x 5 on every service. |
| `deploy/linux/Caddyfile` | Site `{$CS2BOT_PUBLIC_HOST}`, `import tls_{$CS2BOT_TLS_MODE:auto}` (`auto` = ACME certificate for a domain, `internal` = Caddy's CA for an IP), `default_sni` (IP clients without SNI), HTTP/1.1 + HTTP/2 only (no UDP), HSTS, gzip/zstd, 5 MB body limit, `reverse_proxy app:8000` (Host passed through). Auth = the app's own password. |
| `deploy/linux/docker-entrypoint.sh` | `up` (default): derives `CS2BOT_API__ALLOWED_HOSTS=["<public host>"]` and `CS2BOT_API__CORS_ORIGINS=["https://<public host>"]` from `CS2BOT_PUBLIC_HOST` (unless set), checks the password, checks `data/` + `logs/` are writable, starts a supervised `Xvfb :99` (stale lock removed), `cs2bot db upgrade` (10 tries, 6 s apart), then `cs2bot up` with its output also appended to `logs/cs2bot.log`. SIGTERM/SIGINT -> SIGTERM to the bot (clean shutdown), Xvfb stopped after the bot; if Xvfb dies the bot is stopped and the container exits 70 (Docker restarts it). `selftest`, `migrate`, anything else is `exec`'d. |
| `deploy/linux/selftest.py` | Image self-test (no DB, no internet): package + settings load, dashboard `index.html`, data dir writable, RAR tools, X display, executor not falling back to headless, Chromium 3x headful on Xvfb and 3x headless with a persistent profile + screenshot (like the executor). |
| `deploy/linux/common.sh` | Shared helpers: paths, `compose` (always `-f docker-compose.cloud.yml --env-file .env`), `env_get` / `env_set` (.env reader/writer, last assignment wins, quotes, comments), `is_true`, `service_health`, `wait_healthy`, `confirm`, `take_lock` (one flock shared by deploy/update/backup/restore, inherited by child scripts). |
| `deploy/linux/provision.sh` | Fresh Ubuntu 24.04/22.04, run as root, idempotent: apt upgrade, unattended-upgrades (auto reboot 04:45, `--no-auto-reboot`), Europe/Warsaw + NTP, ufw (SSH ports detected from the session / `sshd -T`, 80, 443), fail2ban (sshd, systemd backend), SSH keys-only when a key exists, 4 GB swap if RAM < 8 GB, Docker CE + compose plugin from Docker's repo (`daemon.json` log rotation + live-restore), user `cs2bot` (uid 10001 when free, docker group, owner's authorized_keys, a deploy key), checkout in `/opt/cs2bot` (from `--repo`, or a copy of the checkout it runs from), `data/ logs/ backups/`, `.env` via init-env.sh, systemd timers, logrotate. |
| `deploy/linux/init-env.sh` | Creates `.env` from `.env.example` (chmod 600) with the VPS values (`COMPOSE_FILE`, `APP_UID/GID`, `POSTGRES_*`, API host 0.0.0.0, autonomy + auto-login + Telegram on, `CREDENTIALS_SOURCE=env`, DRY RUN on, headful) and generated secrets (`POSTGRES_PASSWORD` hex, `CS2BOT_API__PASSWORD` 24 alphanumerics, `CS2BOT_API__SESSION_SECRET` hex); `CS2BOT_PUBLIC_HOST` = `--domain` or the machine's IPv4 (+ `CS2BOT_TLS_MODE` auto/internal). On an existing `.env` only missing/empty keys are filled. |
| `deploy/linux/deploy.sh` | Preflight (.env keys, TLS mode vs IP, DRY_RUN warning, empty Betclic/Telegram warnings, `data/ logs/` owner = `APP_UID`, disk), `compose build --pull app`, optional `--selftest` (`compose run --rm --no-deps app selftest`), `compose up -d --remove-orphans`, wait healthy (postgres 180 s, app `--timeout` 1200 s), status + URL. |
| `deploy/linux/update.sh` | Backup (`before-update`), refuses local edits outside `config/` (those are stashed and re-applied), `git pull --ff-only` (`--branch`), `deploy.sh --selftest`. |
| `deploy/linux/backup.sh` | `pg_dump -Fc -Z6` via `compose exec -T postgres` to `backups/cs2bot-YYYYMMDD-HHMMSS[-label].dump`, verified with `pg_restore --list`, retention (`--keep-days` / `BACKUP_KEEP_DAYS` 14, newest `--keep-at-least` 3 kept), optional `--with-data` / `BACKUP_INCLUDE_DATA` (tar of `data/models` + `data/browser` without caches), optional off-site `rclone copy` to `RCLONE_REMOTE` + remote retention `RCLONE_KEEP_DAYS` (30); log in `logs/backup.log`. |
| `deploy/linux/restore.sh` | `--list`; FILE or newest; typed `RESTORE` (or `--yes`); safety backup `before-restore`; stops the app; `compose cp` into the container; `dropdb --force` + `createdb`; `pg_restore --exit-on-error --no-owner`; migrates and sets a **manual pause** in a one-off container (`--no-pause` skips it); starts the app and waits; on any failure the app stays stopped. |
| `deploy/linux/logs.sh`, `status.sh`, `prune.sh`, `vnc.sh` | Logs per service / log files; overview (health, `cs2bot control status`, `--doctor`, settings, disk, latest backup age, timers); weekly clean-up (images/build cache/networks older than 7 days, never volumes); x11vnc on `:99` with a one-time password, `-once`, timeout, reachable only through `ssh -L 5900:127.0.0.1:5900`. |
| `deploy/linux/systemd/cs2bot-{backup,prune}.{service,timer}`, `deploy/linux/logrotate.conf` | Templates (`@DIR@`, `@USER@`) installed by provision.sh: backup daily 04:15 (Persistent), prune Sunday 05:40; logrotate daily, 14 files, copytruncate, for `logs/*.log`. |
| `deploy/linux/.gitattributes` | LF line endings. |
| `docs/VPS_SETUP.md` | Owner guide: choosing a VPS in Poland, SSH keys, domain, provision, every `.env` key (BotFather / userinfobot, `$` quoting), deploy, dashboard/Telegram/first login, backfill in a side container, dry-run smoke test and going live, the Betclic datacentre-IP caveat + Polish residential proxy, SMS code via dashboard button + VNC-over-SSH, backups/restore/off-site, updates, costs, honest risks, troubleshooting. |
| `.github/workflows/ci.yml` | New job `docker`: `bash -n` + `shellcheck -x -S warning` on the scripts, `docker compose config`, `caddy validate` (IP/internal and domain/auto), `docker compose build --pull app` (no push), image self-test, then a full smoke test: `deploy.sh`, HTTPS through Caddy (`/api/ping`, the dashboard page), login with Host/Origin/CSRF/cookie, authenticated `/api/health` (and 401 without), `backup.sh`, `restore.sh --yes`, `status.sh`; logs on failure; `down -v` always. Existing jobs unchanged. |

### Validation done here

- The entrypoint was run outside Docker against PostgreSQL 16 (temporary database, since dropped) with Xvfb: migrations, `cs2bot up`, `/api/ping` 200, Host `bot.example.pl` accepted / `evil.com` rejected (400), login with `Origin: https://bot.example.pl` 200 / foreign origin 403, `/api/health` with the cookie 200, dashboard HTML served; SIGTERM -> exit 0 in ~3 s, SIGINT -> exit 0, Xvfb killed -> bot stopped cleanly, exit 70, no leftover processes; stale `/tmp/.X99-lock` handled. Bugs found and fixed this way: `wait -n -p` leaves the variable unset when a signal interrupts it (`set -u` crash), and background jobs of a non-interactive bash ignore SIGINT (now always forwarded as SIGTERM).
- The self-test passes (headful + headless Chromium 3x each). **Finding:** headful Chromium on Xvfb failed `page.screenshot()` ("Unable to capture screenshot") in 3 of 5 launches with default flags and never with `--disable-gpu` (WebGL still available through SwiftShader) - hence the `cs2bot-chromium` wrapper.
- `init-env.sh` (fresh, re-run keeping an edited value, `--domain`), `env_get`/`env_set` edge cases, and deploy / backup / restore / status / logs / prune / vnc / update against a fake `docker` (call sequence checked), the unhealthy path, the lock between update -> backup -> deploy.

### Not validated here (Docker daemon unavailable, no Ubuntu VPS)

The image build itself, Caddy (syntax checked by hand; CI runs `caddy validate`), shellcheck (not installed, downloading binaries was not permitted; CI runs it), `provision.sh` on a real Ubuntu (it would modify this machine), Let's Encrypt issuance, x11vnc through the tunnel, live Betclic / HLTV from a VPS IP. The CI `docker` job covers everything except provision.sh, the certificate and the live sites.

## Public API

Everything is operated from the checkout on the VPS (`/opt/cs2bot`), as the `cs2bot` user unless noted. Exit code 0 = success, 1 = failure.

```text
sudo bash deploy/linux/provision.sh [--domain HOST] [--repo URL] [--branch main] [--dir /opt/cs2bot]
                                    [--user cs2bot] [--ssh-port N] [--no-swap] [--keep-password-login]
                                    [--no-auto-reboot] [--yes]                       # root, idempotent
deploy/linux/init-env.sh [--domain HOST] [--public-host IP] [--uid N --gid N] [--owner USER]
deploy/linux/deploy.sh   [--no-build] [--no-wait] [--timeout SECONDS(1200)] [--selftest]
deploy/linux/update.sh   [--no-backup] [--branch NAME] [--yes]
deploy/linux/backup.sh   [--label WORD] [--keep-days N] [--keep-at-least N(3)] [--no-prune] [--no-offsite]
                         [--with-data] [--quiet]
deploy/linux/restore.sh  --list | [FILE] [--yes] [--no-safety-backup] [--no-pause]
deploy/linux/logs.sh     [app|postgres|caddy|all] [--since 2h] [--tail N] [--no-follow] | --file | --backup
deploy/linux/status.sh   [--doctor]
deploy/linux/prune.sh    [--dry-run]
deploy/linux/vnc.sh      [--timeout SECONDS(600)] [--view-only] | --stop
```

Container entrypoint (`/usr/local/bin/cs2bot-entrypoint`, PID 1 = tini): `up` (default) | `selftest` | `migrate` | any command (`docker compose exec app cs2bot <cmd>`, `docker compose run --rm app cs2bot <cmd>`; for a one-off `run` container the entrypoint first starts its own Xvfb on `DISPLAY` when that display's socket is missing - a failure is only a warning - and then `exec`s the command). Environment it reads: `CS2BOT_PUBLIC_HOST`, `DISPLAY` (:99), `XVFB_SCREEN` (1920x1080x24), `CS2BOT_APP_DIR` (/app), `CS2BOT_LOG_FILE` (`/app/logs/cs2bot.log`, empty = off), `DB_UPGRADE_ATTEMPTS` (10). It never overrides `CS2BOT_API__ALLOWED_HOSTS` / `CS2BOT_API__CORS_ORIGINS` when they are set.

`.env` keys added for the VPS (besides every `CS2BOT_*` setting): `COMPOSE_FILE=docker-compose.cloud.yml`, `APP_UID`, `APP_GID`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_SHARED_BUFFERS` / `_EFFECTIVE_CACHE_SIZE` / `_WORK_MEM` / `_MAINTENANCE_WORK_MEM`, `CS2BOT_PUBLIC_HOST`, `CS2BOT_TLS_MODE` (`auto` | `internal`), `VNC_HOST_PORT` (5900), `BACKUP_KEEP_DAYS` (14), `BACKUP_INCLUDE_DATA`, `RCLONE_REMOTE`, `RCLONE_KEEP_DAYS` (30).

Contract with the app (what the deployment relies on): `cs2bot db upgrade` is idempotent; `cs2bot up` logs to stdout/stderr, serves `/api/ping` without auth, shuts down cleanly on SIGTERM within ~90 s (grace period 120 s) and exits non-zero on a crash; settings from env with `CS2BOT_` + `__`; relative paths resolve under `/app` (the WORKDIR and project root); Playwright browsers from `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`; `settings.betclic.chromium_executable` is honoured by the executor and the odds browser.

Deploy in 10 commands (owner's PC -> fresh Ubuntu VPS):

```bash
ssh root@<vps-ip>
apt-get update && apt-get install -y git
git clone <repo-url> /root/cs2bot-src
bash /root/cs2bot-src/deploy/linux/provision.sh --domain bot.example.pl   # or without --domain
sudo -iu cs2bot
nano /opt/cs2bot/.env                     # Anthropic key, Betclic login, Telegram token + chat id, bankroll
/opt/cs2bot/deploy/linux/deploy.sh        # build, start, wait healthy (DRY RUN)
/opt/cs2bot/deploy/linux/status.sh --doctor
cd /opt/cs2bot && docker compose run -d --name cs2bot-backfill \
  -e CS2BOT_HLTV__BROWSER_PROFILE_DIR=data/browser/hltv-backfill app cs2bot ingest backfill --since 2023-09-27
# after the dry-run smoke test: CS2BOT_EXECUTOR__DRY_RUN=false in .env, then deploy.sh again
```

## Known limitations

- **Not built here**: no Docker daemon on this machine, so the image, Caddy and shellcheck are validated only by the new CI job; base image tags were checked to exist on Docker Hub / ghcr.io (python 3.12.16-slim-bookworm, node 22.23.5-bookworm-slim, uv 0.11.32, caddy 2.11.9-alpine, postgres 16.16-bookworm). apt packages are not version-pinned (security updates), so a rebuild can pick up newer Debian packages.
- **Behind Caddy every request comes from Caddy's container address** (`api/main.py` runs uvicorn with `proxy_headers=False`): the per-IP login rate limit therefore acts per Caddy, i.e. 5 wrong passwords from anyone block dashboard logins for everyone for 5 minutes (the global limit and the generated 24-character password keep brute force infeasible); the audit log shows Caddy's IP; and the session cookie is not marked `Secure` (scheme seen by the app is http). HSTS on the Caddy side mitigates the cookie point. See FCR 1.
- **`--disable-gpu` wrapper**: the executor offers no way to pass Chromium flags, so the image points `CS2BOT_BETCLIC__CHROMIUM_EXECUTABLE` at `/usr/local/bin/cs2bot-chromium`. With a custom executable the odds collector's headless browser is the full Chromium in headless mode instead of the headless shell (works, slightly heavier). HLTV keeps Playwright's default browser. See FCR 2.
- The Docker healthcheck uses `/api/ping` (liveness only) because `/api/health` needs the dashboard password; job / executor health is on the dashboard and in `status.sh` (`cs2bot control status`, `--doctor`).
- The first model training runs before the API starts: a very long first training (> ~17 min) marks the container `unhealthy` (Docker does not restart it for that) and `deploy.sh` times out after 20 min while the bot keeps starting.
- **History backfill is manual** (no scheduler job exists): VPS_SETUP.md runs it in a side container with its own HLTV profile, next to the live bot; two HLTV readers from one IP raise the Cloudflare risk (proxy recommended).
- An IPv6-only `CS2BOT_PUBLIC_HOST` needs brackets in the Caddyfile site address; use IPv4 or a domain. `CS2BOT_TLS_MODE=internal` makes browsers warn once (no public CA for IPs).
- Docker-published ports bypass ufw; only Caddy's 80/443 and the loopback-only VNC port are published, so this is safe, but anyone adding `ports:` must know it.
- `restore.sh` restores the database only; `--with-data` archives (`data/models`, `data/browser`) are restored by hand (`tar -xzf ... -C /opt/cs2bot` with the bot stopped). Bets placed after the backup are missing from a restored ledger until settlement / reconciliation finds them in Betclic's history.
- `provision.sh` copies the checkout it runs from when no `--repo` is given; `update.sh` then needs read access to that checkout's `origin` (private repo: the printed deploy key or a token URL).
- Git file modes: `deploy/linux/*.sh` must be committed executable (100755; already so in the snapshot commits). If they ever lose the bit, run them with `bash` (systemd units already do).

## Foundation change requests

1. **`src/cs2bot/config.py` + `src/cs2bot/api/main.py` (+ `api/routes/auth.py`)**: add `APISettings.forwarded_allow_ips: list[str] = []` (or `trust_proxy_headers: bool = False`). When set, run uvicorn with `proxy_headers=True, forwarded_allow_ips=<value>` so the client IP (login rate limit, audit log) and the scheme (`Secure` session cookie) come from Caddy's `X-Forwarded-For` / `X-Forwarded-Proto`. `docker-compose.cloud.yml` would then pin Caddy's address (e.g. a fixed `ipv4_address` on the `frontend` network) and set `CS2BOT_API__FORWARDED_ALLOW_IPS` to it. Optionally `APISettings.cookie_secure: bool | None = None` (None = from the scheme) as a simpler alternative for the cookie.
2. **`src/cs2bot/config.py` + `src/cs2bot/executor/session.py` (`build_launch_options`) + `src/cs2bot/odds/browser.py` + `src/cs2bot/ingest/hltv/fetcher.py`**: add `ExecutorSettings.chromium_args: list[str] = []` (and the same for the odds / HLTV browsers, or one shared `browser_args`) passed as `args=` to Playwright's launch. The VPS would set `["--disable-gpu"]` and the `cs2bot-chromium` wrapper (Dockerfile) could be removed.
3. **`.env.example`**: append a commented "VPS (docker-compose.cloud.yml)" block listing `COMPOSE_FILE`, `APP_UID`, `APP_GID`, `CS2BOT_PUBLIC_HOST`, `CS2BOT_TLS_MODE`, `VNC_HOST_PORT`, `BACKUP_KEEP_DAYS`, `BACKUP_INCLUDE_DATA`, `RCLONE_REMOTE`, `RCLONE_KEEP_DAYS` with one-line explanations (init-env.sh already writes the needed ones; this is for discoverability). The "Local only ... A password is only required if you change the host" comment could mention that the VPS sets the host to 0.0.0.0 behind Caddy.
4. **Docs owned by others**: `README.md` and `CLAUDE.md` ("Where things are") should list `deploy/linux/`, `Dockerfile`, `docker-compose.cloud.yml` and `docs/VPS_SETUP.md`; `docs/ARCHITECTURE.md` a short "Cloud deployment" paragraph (container layout, entrypoint, `/api/ping` healthcheck, the env contract above); `docs/dev/STATUS.md` a `cloud_deploy` row. OPERATIONS.md already links VPS_SETUP.md; its VPS hints match (DISPLAY=:99, VNC-over-SSH).
5. **Optional (integration)**: a resumable `ingest_backfill` scheduler job (off by default, `scope.backfill_start`) so a fresh VPS loads its history without the manual side container.

## New dependency requests

None for the Python project or the dashboard. The image adds Debian packages (`tini`, `xvfb`, `x11vnc`, `unar`, `libarchive-tools`, `curl`, fonts, plus Playwright's own `--with-deps` set) and the VPS gets Docker CE, ufw, fail2ban, unattended-upgrades and rclone from apt.

## Review round 1

Independent review of every file of the package against the brief, `config.py`, the CLI, the API's
Host / CSRF / password rules and the cloud_autonomy settings.

### What was verified

- **Env names**: every `CS2BOT_*` name in the Dockerfile, compose file, scripts, CI and VPS_SETUP.md
  was checked programmatically against `Settings` (sections and fields). All are real settings except
  the deploy-only variables (`CS2BOT_PUBLIC_HOST`, `CS2BOT_TLS_MODE`, `CS2BOT_APP_DIR`, `CS2BOT_LOG_FILE`,
  `CS2BOT_BACKUP_DIR`, `CS2BOT_REPO_URL`, `CS2BOT_BRANCH`, the internal lock / sourcing guards), which
  `extra="ignore"` drops.
- **CLI names** used by the scripts and docs exist with those options (`db upgrade`, `up`, `doctor`,
  `control status|pause --reason|resume|kill-switch`, `telegram test`, `ingest backfill --since`,
  `odds unmapped`, `models train|list`, `version`).
- **API host / password rule**: compose forces `CS2BOT_API__HOST=0.0.0.0` and makes
  `CS2BOT_API__PASSWORD` + `CS2BOT_API__SESSION_SECRET` required (`${VAR:?}`; `docker compose config`
  fails without them, re-checked here); the entrypoint refuses to start without them too. The Host
  allow-list / https origin are derived from `CS2BOT_PUBLIC_HOST`; the healthcheck's
  `127.0.0.1:8000` is always allowed (loopback + API port).
- **Exposure**: rendered compose config - only Caddy publishes 80/443; PostgreSQL has no ports and sits
  on the `internal` backend network; the API port 8000 is never published (only Caddy reaches it); the
  VNC port is published on the host's 127.0.0.1 only. ufw allows SSH/80/443.
- **DISPLAY / Xvfb**: `DISPLAY=:99` in compose, supervised Xvfb in the entrypoint, executor
  `headless=false`; `effective_headless` cannot fall back because DISPLAY is always set.
- **Volumes**: `./data` (browser profiles incl. the Betclic session and placement journal, models,
  caches, screenshots, demos), `./logs`, `./config:ro`; named volumes for PostgreSQL and Caddy.
- **Proxies**: `CS2BOT_BETCLIC__PROXY_URL` is optional and empty by default; `CS2BOT_HLTV__PROXY_URL`
  with `CS2BOT_HLTV__PROXY_MODE=after_block` (default) is the configurable HLTV fallback.
- Ran: `bash -n` on every script; `yaml.safe_load` on compose + CI; `docker compose config` (and its
  failure on a missing secret); `init-env.sh` fresh / re-run / as root; deploy.sh and restore.sh (success,
  pause failure, pg_restore failure) and `wait_healthy` (restart loop) against a fake `docker`; the
  entrypoint with real Xvfb and a fake `cs2bot` (SIGTERM forwarding, derived hosts/origins, no leftover
  Xvfb) and its new one-off-command display path (piped stdin preserved, existing display reused);
  the new CI `init-env.sh` step locally; ruff / ruff format / mypy on `selftest.py`; tests/foundation
  (37 passed).

### Issues found

1. **major, fixed** - `restore.sh` restarted the bot with betting enabled on a restored ledger that
   misses every bet placed after the backup (exposure, daily / weekly loss limits and bets-per-day
   computed too low; the kill switch / pause state reverts to the backup's). It now migrates the
   restored database and sets a **manual pause** (never auto-resumed) in a one-off container before
   starting the bot; if that fails the bot stays stopped with instructions. `--no-pause` opts out.
   VPS_SETUP.md section 13 explains it; CI asserts `paused: yes` after the restore.
2. **major, fixed** - `init-env.sh` run as root without `--uid` wrote `APP_UID=0` / `APP_GID=0`
   (reproduced): the image build fails (`useradd` cannot create a second uid 0) and the intent is that
   the bot never runs as root. It now uses the checkout owner's uid (or 10001) and repairs an existing
   0; `deploy.sh` refuses `APP_UID`/`APP_GID` 0 or non-numeric values. CI step added.
3. **minor, fixed** - `restore.sh`: a failing `dropdb` / `createdb` aborted via `set -e` without a
   message and without restarting the bot; now explicit (database unchanged -> bot restarted; database
   missing -> bot stays stopped, clear message).
4. **minor, fixed** - `wait_healthy` waited the whole timeout (20 min for the app) when the container
   was crash-looping (health resets to `starting` after every restart); more than one restart while
   waiting now fails at once.
5. **minor, fixed** - entrypoint: a SIGTERM arriving after the traps were installed but before the bot's
   pid was known was lost (the bot then ran until Docker's SIGKILL); it is now remembered and delivered.
6. **minor, fixed** - one-off containers (`docker compose run`, e.g. the backfill) had no X display, so
   `CS2BOT_HLTV__HEADLESS=false` (visible HLTV browser, PLAN 0b) crashed there; the entrypoint now starts
   an Xvfb for them (warning only on failure) and still `exec`s the command. (A first version of this
   change computed the socket path wrongly - caught by the test and fixed before finishing.)
7. **minor, fixed** - `deploy.sh` accepted a `CS2BOT_PUBLIC_HOST` with a scheme / port / path, which
   yields a broken Caddy `default_sni`, Host allow-list and CSRF origin; it now requires a bare domain or
   IPv4.
8. **docs, fixed** - VPS_SETUP.md: Betclic proxy presented as optional with the owner's observation
   that Betclic works from a Polish VPN (start without a proxy); HLTV proxy fallback and
   `CS2BOT_HLTV__PROXY_MODE` documented (settings table + backfill section); visible-HLTV option and its
   VNC caveat; restore pause. `status.sh` also shows the HLTV proxy mode.

### Remaining concerns

- Still not built or run here (no Docker daemon, no shellcheck): the image, Caddy, Let's Encrypt and
  the full compose smoke test are validated only by the CI `docker` job; `provision.sh` only by reading.
- `cs2bot up` installs its SIGTERM handler only after the first model training (`flows/supervisor.py`):
  a `docker stop` during a long first training kills the process without a clean shutdown (harmless for
  training, noted for the integration owner).
- Visible HLTV on the same Xvfb display can cover the Betclic window in VNC (no window manager); kept
  headless by default, documented as an option.
- FCR 1 (proxy headers: per-Caddy login rate limit, non-`Secure` cookie) and FCR 2 (Chromium args
  instead of the `cs2bot-chromium` wrapper) from the implementer still stand.
