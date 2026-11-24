# Running cs2bot on a VPS in Poland

This guide takes you from "no server" to a bot that runs on its own, 24 hours a day, on a rented
Linux server (a **VPS**) in Poland. You do not need to be a programmer: every step says **what to
type** and **what it does**. Plan about one hour for the first installation.

The bot runs fully on its own (PLAN.md 0b): it collects data, trains its models, predicts, bets,
settles and lifts its own automatic pauses. You follow it on **Telegram** and on the **dashboard**
(a web page protected by a password, over HTTPS). Only two things ever need you: an **SMS code** if
Betclic asks for one, and switching the **kill switch** off after you switched it on.

> **Read this first.** No betting system wins every bet. Even a true 90% chance loses one time in
> ten, and the Polish 12% stake tax works against you on every bet. Betclic's terms forbid bots, so
> **your Betclic account and its balance are at risk** if Betclic notices the automation. Only use
> money you can afford to lose. You must be 18 or older. See [Risks](#16-risks-plainly).

**Contents**

1. [What you get](#1-what-you-get)
2. [Rent a VPS](#2-rent-a-vps)
3. [SSH key: log in without a password](#3-ssh-key-log-in-without-a-password)
4. [Domain name (recommended)](#4-domain-name-recommended)
5. [Prepare the server (provision.sh)](#5-prepare-the-server-provisionsh)
6. [Fill in your settings (.env)](#6-fill-in-your-settings-env)
7. [Start the bot (deploy.sh)](#7-start-the-bot-deploysh)
8. [Dashboard, Telegram and the first Betclic login](#8-dashboard-telegram-and-the-first-betclic-login)
9. [Load the match history (backfill)](#9-load-the-match-history-backfill)
10. [Dry-run smoke test, then going live](#10-dry-run-smoke-test-then-going-live)
11. [Betclic and the server's IP address](#11-betclic-and-the-servers-ip-address)
12. [When Betclic asks for an SMS code (VNC over SSH)](#12-when-betclic-asks-for-an-sms-code-vnc-over-ssh)
13. [Backups and restore](#13-backups-and-restore)
14. [Updates and day-to-day commands](#14-updates-and-day-to-day-commands)
15. [Costs](#15-costs)
16. [Risks, plainly](#16-risks-plainly)
17. [Troubleshooting](#17-troubleshooting)

Day-to-day operation (pause reasons, Telegram messages, jobs, tuning) is described in
[OPERATIONS.md](OPERATIONS.md). The Windows-PC installation ([WINDOWS_SETUP.md](WINDOWS_SETUP.md))
stays available as an alternative.

---

## 1. What you get

```
 your phone / PC                                 the VPS (Ubuntu, Warsaw)
 ---------------                                 -------------------------------------------------
 browser  --- https://bot.example.pl --->  :443  Caddy (HTTPS, automatic certificate)
                                                   |
                                                   v  (internal network only)
 Telegram <--- messages, /status /pause ---        cs2bot  (scheduler + Betclic browser on a
                                                   |        virtual screen + dashboard API)
                                                   v  (internal network, no internet access)
                                                 PostgreSQL (all data, the bet ledger)
```

- Everything runs in **Docker** containers that restart by themselves after a crash or a reboot.
- Only ports **80 and 443** (the dashboard) and **SSH** are reachable from the internet. The
  database and the bot's API are not.
- The Betclic browser is a real, visible Chromium on a **virtual screen** (Xvfb) inside the server,
  which looks less like a bot than an invisible ("headless") browser. You can look at that screen
  through an encrypted SSH tunnel ([section 12](#12-when-betclic-asks-for-an-sms-code-vnc-over-ssh)).
- The database is backed up **every night** (04:15), security updates install **automatically**.

Files on the server: everything lives in **`/opt/cs2bot`** (the code, `.env` with your secrets,
`data/` with the browser profiles and models, `logs/`, `backups/`).

## 2. Rent a VPS

| What | Recommendation | Why |
|---|---|---|
| Location | **Poland, Warsaw region** | Betclic.pl only answers Polish IP addresses. |
| CPU / RAM | **at least 4 vCPU and 8 GB RAM** (16 GB is comfortable) | Model training, simulations, PostgreSQL and two browsers run at the same time. |
| Disk | **at least 200 GB SSD/NVMe** | The match database grows with the history; HLTV demo files (hundreds of MB each) are stored while they are parsed. |
| System | **Ubuntu 24.04 LTS** (22.04 also works) | The scripts are written and tested for it. |
| Extras | none needed; a provider snapshot/backup option is a plus | |

Examples: OVHcloud (VPS or Public Cloud in its Warsaw region), or Polish hosting companies that sell
VPS in Polish data centres. Check that the server really is in Poland before you pay. While
ordering, most providers let you paste an **SSH public key** - do [section 3](#3-ssh-key-log-in-without-a-password)
first and paste it there.

After ordering you receive the server's **IP address** (e.g. `51.68.10.20`) and a login (`root` or
`ubuntu`).

## 3. SSH key: log in without a password

SSH is the encrypted remote login to the server. A key is safer than a password, and the setup
script switches password logins off once a key works.

**On Windows 10 / 11** (PowerShell; Linux / macOS: the same commands in a terminal):

```powershell
ssh-keygen -t ed25519            # press Enter three times (default file, optional passphrase)
type $env:USERPROFILE\.ssh\id_ed25519.pub
```

The second command prints one line starting with `ssh-ed25519`. That is your **public key**:

- paste it into the provider's "SSH keys" field when ordering, **or**
- after ordering, log in once with the password and add it:

  ```powershell
  ssh root@51.68.10.20
  ```
  then on the server: `mkdir -p ~/.ssh && nano ~/.ssh/authorized_keys`, paste the line, save with
  Ctrl+O, Enter, Ctrl+X.

Test it: `ssh root@51.68.10.20` must log you in **without asking for the server password** (it may
ask for the key's passphrase). If your provider gave you the user `ubuntu`, use `ssh ubuntu@...` and
put `sudo` in front of the commands below.

The private key (`id_ed25519`, without `.pub`) never leaves your PC. Keep a copy somewhere safe.

## 4. Domain name (recommended)

With a domain (e.g. `bot.example.pl`) the dashboard gets a real HTTPS certificate from Let's Encrypt
automatically and your browser shows a normal padlock.

1. Buy a domain at any registrar (a `.pl` domain costs a few złoty per year in the first year).
2. In the registrar's DNS settings create an **A record**: name `bot` (or `@` for the bare domain),
   value = the server's IP address.
3. Wait until `ping bot.example.pl` shows the server's IP (minutes to a few hours).

**Without a domain** the dashboard works on `https://<IP address>` with a certificate from Caddy's
own authority: the connection is encrypted, but the browser warns once ("not private") because it
does not know that authority - choose "Advanced" -> "Continue". This is fine for you alone; you can
add a domain later ([section 17](#change-the-domain-later)).

## 5. Prepare the server (provision.sh)

Log in (`ssh root@51.68.10.20`) and run:

```bash
apt-get update && apt-get install -y git
git clone <YOUR-REPOSITORY-URL> /root/cs2bot-src
bash /root/cs2bot-src/deploy/linux/provision.sh --domain bot.example.pl
```

Leave out `--domain ...` if you have no domain. For a private GitHub repository, clone with a
personal access token (`https://<token>@github.com/<owner>/<repo>.git`) or see the deploy-key hint
the script prints at the end.

*What it does* (it takes 5-10 minutes and can be run again at any time - it only fixes what is
missing):

1. installs all system updates and turns on **automatic security updates** (if one needs a reboot,
   the server reboots at **04:45** and every service comes back by itself; `--no-auto-reboot` turns
   that off);
2. sets the time zone to **Europe/Warsaw** and turns on automatic time sync (pre-match bets and
   HTTPS need an exact clock);
3. turns on the **firewall** (only SSH, 80 and 443 come in), **fail2ban** (blocks addresses that keep
   guessing SSH passwords) and, if your SSH key is installed, switches **password logins off**;
4. adds a 4 GB **swap file** if the server has less than 8 GB RAM;
5. installs **Docker** from Docker's official repository;
6. creates the user **`cs2bot`** (the bot's files belong to it; your SSH key works for it too:
   `ssh cs2bot@<server>`) and copies the code to **`/opt/cs2bot`**;
7. creates **`/opt/cs2bot/.env`** (readable only by `cs2bot`) with the server settings and freshly
   generated passwords for the database and the dashboard;
8. schedules the **nightly backup** (04:15) and a **weekly Docker clean-up** (Sunday 05:40), and
   log rotation for `logs/`.

From now on, work as the `cs2bot` user:

```bash
sudo -iu cs2bot          # or log in directly: ssh cs2bot@51.68.10.20
cd /opt/cs2bot
```

## 6. Fill in your settings (.env)

```bash
nano /opt/cs2bot/.env
```

(Ctrl+W searches, Ctrl+O saves, Ctrl+X quits.) The file is the same as on Windows plus a block
"VPS settings" at the end. Fill in or check these lines:

| Setting | What to put | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | your Claude API key (console.anthropic.com) | news / context layer and bet rationales |
| `CS2BOT_PANDASCORE__API_TOKEN` | empty, or a PandaScore token | optional; HLTV is the main source |
| `CS2BOT_BETCLIC__CREDENTIALS_SOURCE` | `env` (already set) | the bot logs in by itself with the next two lines |
| `CS2BOT_BETCLIC__USERNAME` / `CS2BOT_BETCLIC__PASSWORD` | your Betclic.pl login | see "quotes" below |
| `CS2BOT_BETCLIC__PROXY_URL` | empty, or `http://user:pass@host:port` | only if Betclic blocks the server's IP ([section 11](#11-betclic-and-the-servers-ip-address)) |
| `CS2BOT_TELEGRAM__BOT_TOKEN` | token from **@BotFather** | in Telegram: open @BotFather, send `/newbot`, choose a name; it answers with the token |
| `CS2BOT_TELEGRAM__CHAT_ID` | your chat id from **@userinfobot** | open @userinfobot, send any message, copy the number after "Id". Then open **your new bot** and press **Start** (a bot cannot write to you before that) |
| `CS2BOT_BANKROLL__STARTING_BANKROLL` | the money on the Betclic account reserved for the bot, e.g. `1000.00` | |
| `CS2BOT_BANKROLL__*_LIMIT_*`, `MAX_BETS_PER_DAY` | daily -5% / weekly -10% loss limits, bets per day | defaults follow PLAN.md |
| `CS2BOT_DECISION__MIN_PROBABILITY` | `0.75` | the bet rule: bet when the model is at least 75% sure, whatever the odds |
| `CS2BOT_EXECUTOR__DRY_RUN` | **`true`** | keep it `true` until the smoke test in [section 10](#10-dry-run-smoke-test-then-going-live) passed |
| `CS2BOT_AUTONOMY__ENABLED`, `CS2BOT_AUTONOMY__AUTO_LOGIN`, `CS2BOT_TELEGRAM__ENABLED` | `true` (already set) | unattended operation |
| `CS2BOT_PUBLIC_HOST`, `CS2BOT_TLS_MODE` | set by the script (`bot.example.pl` + `auto`, or the IP + `internal`) | |
| `CS2BOT_API__PASSWORD` | generated - this is your **dashboard password** | user name: `CS2BOT_API__USERNAME` (`admin`) |
| `POSTGRES_PASSWORD`, `CS2BOT_API__SESSION_SECRET`, `APP_UID`, `APP_GID`, `COMPOSE_FILE` | generated - leave them alone | changing `POSTGRES_PASSWORD` later does not change the existing database's password |
| `RCLONE_REMOTE` (optional) | e.g. `b2:cs2bot-backups` | off-site copy of the backups ([section 13](#13-backups-and-restore)) |

**Quotes:** if a value contains a `$` sign (or spaces), wrap it in **single quotes**:
`CS2BOT_BETCLIC__PASSWORD='my$ecret pass'`. Docker would otherwise treat `$ecret` as a variable.

**Secrets stay here.** `.env` is readable only by `cs2bot` (`chmod 600`), is never part of the
repository or of the Docker image, and the bot never writes these values into its logs.

## 7. Start the bot (deploy.sh)

```bash
/opt/cs2bot/deploy/linux/deploy.sh
```

*What it does:* checks `.env`, builds the bot's Docker image (the first time 5-15 minutes: Python,
Chromium, the dashboard), starts PostgreSQL, the bot and Caddy, waits until the bot answers and shows
the dashboard address. On every start the bot first updates the database structure
(`cs2bot db upgrade`), then runs `cs2bot up`.

Running `deploy.sh` again is harmless: it rebuilds what changed and restarts only the containers
whose image or settings changed. **After every change to `.env`, run `deploy.sh` again** (or
`docker compose up -d`) so the bot picks it up.

Check it (in `/opt/cs2bot`; `COMPOSE_FILE` in `.env` makes plain `docker compose` use the server file):

```bash
deploy/linux/status.sh            # services, trading state, disk, backups, timers
deploy/linux/status.sh --doctor   # plus the full self-check of the bot (cs2bot doctor)
deploy/linux/logs.sh              # follow the bot's output (Ctrl+C stops following, not the bot)
```

## 8. Dashboard, Telegram and the first Betclic login

1. **Dashboard:** open `https://bot.example.pl` (or `https://<IP>`), log in with `admin` and the
   `CS2BOT_API__PASSWORD` from `.env` (`grep API__PASSWORD /opt/cs2bot/.env`). The **Health** page
   shows every job, the trading state and alerts.
2. **Telegram:** you get "cs2bot started. Betting mode: DRY RUN ..." when the bot starts. Test it any
   time:

   ```bash
   docker compose exec app cs2bot telegram test
   ```

   Send `/status` to your bot: it answers with the trading state. `/help` lists the commands
   (`/status /balance /bets /pause /resume /stop /go`).
3. **Betclic login:** the bot logs in by itself (auto-login) when it needs to. Do the very first
   login with your own eyes once anyway, to see that it works and to check two account settings:
   press **"Open Betclic login"** on the dashboard, then look at the server's browser through VNC
   ([section 12](#12-when-betclic-asks-for-an-sms-code-vnc-over-ssh)). In Betclic's account
   settings make sure that
   - Betclic **asks before accepting** a changed price (do not enable "accept all odds changes");
   - **quick-bet / one-click betting** is off.

## 9. Load the match history (backfill)

The models learn from past matches. A new database is empty, so load the history from the CS2
release (September 2023) once. It runs in a **separate container next to the bot** with its own
HLTV browser profile, takes **several days** (HLTV is read slowly on purpose) and can be stopped and
resumed at any time:

```bash
cd /opt/cs2bot
docker compose run -d --name cs2bot-backfill \
  -e CS2BOT_HLTV__BROWSER_PROFILE_DIR=data/browser/hltv-backfill \
  app cs2bot ingest backfill --since 2023-09-27
docker logs -f cs2bot-backfill        # progress (Ctrl+C stops following)
```

- Stop it: `docker stop cs2bot-backfill && docker rm cs2bot-backfill`. Start the same command again
  later: it continues where it stopped.
- When it is finished (`docker ps -a` shows it as "Exited (0)"): `docker rm cs2bot-backfill`. The
  **nightly retrain** (05:00) trains on the new data; to train right away:
  `docker compose exec app cs2bot models train` and then `docker compose restart app`.
- HLTV may block a server that reads too much (Cloudflare). The bot then waits and retries by
  itself. A Polish residential proxy for HLTV (`CS2BOT_HLTV__PROXY_URL` in `.env`) helps with the
  backfill; see OPERATIONS.md for slowing down.

## 10. Dry-run smoke test, then going live

With `CS2BOT_EXECUTOR__DRY_RUN=true` the bot does everything except the final "Postaw zakład" click:
it opens the match, puts the outcome on the bet slip, types the stake, finds the button and clears
the slip again. Let it run like this for a day or two:

1. `deploy/linux/status.sh --doctor` shows no `FAIL`.
2. Dashboard **Health**: the data jobs turn green over the first hours; the executor check shows
   "logged in" and finds the page elements.
3. Telegram shows **"DRY RUN bet"** messages, the dashboard **Bets** page shows bets with status
   `dry_run`, settled "on paper" after the match. Each attempt saves a screenshot in
   `/opt/cs2bot/data/screenshots/`; look at a few (e.g. copy them to your PC with
   `scp cs2bot@51.68.10.20:/opt/cs2bot/data/screenshots/*.png .`) and check that the right match,
   market, outcome and stake were on the slip.
4. `docker compose exec app cs2bot odds unmapped` lists Betclic markets no rule understands yet
   (these are never bet).

**Going live:** in `.env` change the line to `CS2BOT_EXECUTOR__DRY_RUN=false`, save, run
`deploy/linux/deploy.sh`. Telegram says "Betting mode: LIVE - real money". To go back, set it to
`true` and run `deploy.sh` again. To stop all betting at once without stopping anything else: the
**kill switch** (dashboard, Telegram `/stop`; `/go` turns it off again).

**The bet rule** (your decision, 2026-11-24): the bot bets when its calibrated probability is at least
75%, **whatever the odds** - also at 1.28. Stakes follow the confidence tiers (1% / 1.5% / 2% of the
bankroll). At very low odds the 12% stake tax can make such a bet lose money on average even when it
wins most of the time; you chose this knowingly.

## 11. Betclic and the server's IP address

Betclic.pl only works from Polish IP addresses, and it **may block data-centre addresses even in
Poland** (a VPS address is a data-centre address). Signs: the executor check on the Health page fails
with an access / error page, the odds collector finds no events, Telegram reports login problems
although the password is right, or screenshots show a block page.

The fix is a **Polish residential proxy** (an address of a normal Polish home connection, rented
from a proxy provider; prefer a "static" / "ISP" residential address so Betclic always sees the same
IP). Put it into `.env`:

```
CS2BOT_BETCLIC__PROXY_URL=http://username:password@proxy-host:port
```

and run `deploy/linux/deploy.sh`. All Betclic traffic of the bot (odds and placement) then goes
through it; HLTV and everything else does not. Use a provider and an address in Poland, and keep the
same address: frequent IP changes on one account look suspicious.

## 12. When Betclic asks for an SMS code (VNC over SSH)

The bot never reads, types or bypasses SMS / 2FA codes. If Betclic asks for one during an automatic
login, the bot **pauses betting** and Telegram says *"Betclic asks for an SMS / 2FA code"*. Then:

1. Dashboard: press **"Open Betclic login"** (the bot opens the login page in its browser).
2. On the server (as `cs2bot`): start the screen viewer

   ```bash
   /opt/cs2bot/deploy/linux/vnc.sh
   ```

   It prints a one-time password and starts a VNC server that listens **only on the server itself**
   (127.0.0.1), for one connection, for at most 10 minutes.
3. On your PC, open an SSH tunnel in a second window (leave it open):

   ```powershell
   ssh -N -L 5900:127.0.0.1:5900 cs2bot@51.68.10.20
   ```

4. Open a VNC viewer (e.g. TigerVNC Viewer or RealVNC Viewer, free) and connect to
   **`localhost:5900`** with the printed password. You see the bot's browser: log in, type the SMS
   code, tick "remember me", wait until your balance shows. Close the viewer (VNC stops by itself)
   and the SSH window.
5. The pause lifts at the next check (within 5 minutes); Telegram confirms.

The VNC port is never opened to the internet: the firewall does not allow it and Docker publishes it
only on the server's own 127.0.0.1. The SSH tunnel is the only way in.

## 13. Backups and restore

- **Nightly** at 04:15 (and at the next boot if the server was off): a compressed database dump in
  `/opt/cs2bot/backups/cs2bot-<date>-<time>-nightly.dump`, checked after writing. Kept 14 days
  (`BACKUP_KEEP_DAYS`), the newest 3 always.
- **By hand:** `deploy/linux/backup.sh` (`--with-data` also saves `data/models` and the browser
  profiles, i.e. your Betclic session - keep such files private).
- **Restore:** `deploy/linux/restore.sh --list`, then `deploy/linux/restore.sh backups/<file>.dump`.
  It asks you to type `RESTORE`, saves the current database first (`...-before-restore.dump`), stops
  the bot, restores and starts it again.
- **Off-site copy (recommended):** a backup on the same server does not survive the loss of the
  server. Set up any rclone storage (Backblaze B2, an S3 bucket, Google Drive, another server via
  SFTP) as the `cs2bot` user with `rclone config`, then put its name into `.env`, e.g.
  `RCLONE_REMOTE=b2:cs2bot-backups` (`RCLONE_KEEP_DAYS=30` deletes older remote copies). Every
  backup is then copied there automatically. Many providers also sell server snapshots - a cheap
  extra.

## 14. Updates and day-to-day commands

**Update the bot** to the newest version of the code:

```bash
/opt/cs2bot/deploy/linux/update.sh
```

It backs up the database, pulls the code (`git pull`), rebuilds the image, runs the image self-test
(browser on the virtual screen, dashboard, tools) and only then restarts the bot. Your edits in
`config/` (Betclic selectors) are kept. **Security updates of Ubuntu** install by themselves.

| Task | Command (as `cs2bot`, in `/opt/cs2bot`) |
|---|---|
| Overview | `deploy/linux/status.sh` (`--doctor` for the full check) |
| Follow the bot's output | `deploy/linux/logs.sh` (`caddy`, `postgres`, `all`; `--since 2h`; `--file` = `logs/cs2bot.log`) |
| Stop / start everything | `docker compose stop` / `docker compose up -d` |
| Restart only the bot | `docker compose restart app` |
| Kill switch / pause | Dashboard, Telegram `/stop` `/go` `/pause` `/resume`, or `docker compose exec app cs2bot control kill-switch on` |
| Any cs2bot command | `docker compose exec app cs2bot <command>` (e.g. `control status`, `odds unmapped`, `models list`) |
| Betclic selectors fix | edit `config/betclic_selectors.yaml`, then `docker compose restart app` |
| See the server's browser | `deploy/linux/vnc.sh` ([section 12](#12-when-betclic-asks-for-an-sms-code-vnc-over-ssh)) |
| Disk usage | `df -h /`, `docker system df`, `du -sh /opt/cs2bot/data/*` |

Never run `docker compose down -v`: `-v` deletes the database volume.

Commands that open the Betclic browser profile themselves (`cs2bot executor login|health|balance`,
`cs2bot bet run`) cannot run while the bot runs, because the bot holds that profile. Use the
dashboard (Health page, "Open Betclic login") instead.

## 15. Costs

Rough monthly costs (check the current prices yourself):

| Item | Approx. per month |
|---|---|
| VPS 4 vCPU / 8-16 GB RAM / 200 GB NVMe in Poland | 60 - 200 PLN |
| Claude API (news triage + match context) | depends on volume; set a monthly limit in the Anthropic console |
| Domain (.pl) | a few PLN (renewals ~ 50-100 PLN per year) |
| Polish residential proxy (only if Betclic blocks the VPS) | 40 - 150 PLN (static ISP address) or per GB |
| Off-site backup storage (B2 / S3) | < 5 PLN |
| PandaScore (optional) | its own price list |

## 16. Risks, plainly

- **No system wins every bet.** CS2 has real variance (clutches, eco-round upsets, overtime, off
  days). The bot bets when it is at least 75% sure, but such bets still lose regularly, and losing
  streaks happen. Judge it over hundreds of bets, never over a few days.
- **Betclic's terms of service forbid bots and automated betting.** If Betclic detects it, it can
  limit or close the account and **withhold the balance**. A server address or a proxy does not make
  automation allowed. Keep only the bot's bankroll on the account. (Betting with a licensed operator
  is legal in Poland; automation is a matter between you and Betclic. This is not legal advice.)
- **Polish tax:** 12% of every stake, plus 10% on single wins above 2280 PLN.
- **The website changes.** After a Betclic redesign the bot may not find its buttons. It then stops
  that bet *before* the final click, pauses and alerts you; it does not guess.
- **The server is on the internet.** Keep your SSH key and the dashboard password private; the
  firewall, fail2ban, HTTPS and the password protect it, and updates install by themselves.
- **18+ only. Gamble responsibly.** Decide your bankroll in advance and never top it up to chase
  losses. Betclic offers deposit limits and self-exclusion. Help in Poland: Krajowe Centrum
  Przeciwdziałania Uzależnieniom (kcpu.gov.pl).

## 17. Troubleshooting

### deploy.sh: "cannot talk to Docker"

You are not yet in the `docker` group in this session: log out (`exit`) and log in again as
`cs2bot`. Check with `docker ps`.

### "Permission denied" when running a script

Run it with bash: `bash /opt/cs2bot/deploy/linux/deploy.sh`.

### The bot does not become healthy

`deploy/linux/logs.sh app` shows why. Typical: a typo in `.env` (the bot names the setting), the
database is not ready (`deploy/linux/logs.sh postgres`), or the first model training is still
running (the bot only answers after it; `deploy.sh` waits up to 20 minutes). `status.sh --doctor`
lists every check.

### The dashboard does not open / certificate errors

- `deploy/linux/logs.sh caddy`: with a domain Caddy reports why it cannot get a certificate. The
  domain's A record must point at the server and ports 80 and 443 must be reachable (some providers
  have an extra firewall in their web panel - allow 80/443 there too).
- "invalid host header": the address in the browser must be exactly `CS2BOT_PUBLIC_HOST`.
- With an IP address (`CS2BOT_TLS_MODE=internal`) a certificate warning is expected (section 4).

### Change the domain later

Set `CS2BOT_PUBLIC_HOST=bot.example.pl` and `CS2BOT_TLS_MODE=auto` in `.env`, then run `deploy.sh`.

### Betclic login problems

Telegram tells you which: *wrong password / account locked* -> fix `.env`, run `deploy.sh`, then
`/resume`; *SMS code* -> [section 12](#12-when-betclic-asks-for-an-sms-code-vnc-over-ssh); repeated
failures with the right password -> probably an IP block ([section 11](#11-betclic-and-the-servers-ip-address)).
Look at the screenshots in `data/screenshots/` or watch through VNC.

### "CIRCUIT BREAKER OPEN"

The same automatic pause came back three times in 24 hours, so the bot stopped retrying. Find the
cause (Health page, `logs.sh`), fix it, then `/resume`.

### The disk is filling up

`docker system df` and `du -sh /opt/cs2bot/data/*`. `deploy/linux/prune.sh` removes old Docker images
(the weekly timer does this too). Demo files are deleted after parsing; the database only grows.

### The server is slow / out of memory

`free -h`, `docker stats`. With 8 GB RAM put `POSTGRES_SHARED_BUFFERS=512MB` and
`POSTGRES_EFFECTIVE_CACHE_SIZE=1500MB` into `.env` and run `deploy.sh`; or move to 16 GB.

### Check the image after an update

`docker compose run --rm --no-deps app selftest` starts the virtual screen and Chromium (visible and
headless), checks the dashboard files and the demo tools. `update.sh` runs it before restarting.
