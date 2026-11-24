#!/usr/bin/env bash
# Build and (re)start cs2bot on the VPS: PostgreSQL, the bot, Caddy.
#
#   deploy/linux/deploy.sh [--no-build] [--no-wait] [--timeout SECONDS] [--selftest]
#
# 1. checks .env (secrets present, chmod 600) and the data/ logs/ backups/ folders
# 2. docker compose build app   (pulls newer base images of the pinned tags)
# 3. --selftest: runs the image self-test (Chromium on Xvfb, dashboard, tools) before switching over
# 4. docker compose up -d --remove-orphans (only containers whose image/config changed are recreated)
# 5. waits until the bot is healthy (first start: migrations; the first model training runs in the
#    background, so the API answers right away)
# 6. shows the status and the dashboard address
# Idempotent: running it again with nothing changed leaves the running containers alone.
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

BUILD=1
WAIT=1
TIMEOUT=1200
SELFTEST=0
while (($#)); do
    case "$1" in
        --no-build) BUILD=0; shift ;;
        --no-wait) WAIT=0; shift ;;
        --timeout) TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
        --selftest) SELFTEST=1; shift ;;
        -h | --help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

require_cmd docker flock
docker info >/dev/null 2>&1 || die "cannot talk to Docker: is it running, and is $(id -un) in the docker group? (log out and in after provision.sh)"
require_env_file

# ------------------------------------------------------------------------------------ preflight
info "checking .env"
missing=()
for key in POSTGRES_PASSWORD CS2BOT_API__PASSWORD CS2BOT_API__SESSION_SECRET CS2BOT_PUBLIC_HOST; do
    [[ -n "$(env_get "$key")" ]] || missing+=("$key")
done
((${#missing[@]} == 0)) || die "missing in .env: ${missing[*]} (provision.sh generates them; see docs/VPS_SETUP.md)"

public_host="$(env_get CS2BOT_PUBLIC_HOST)"
tls_mode="$(env_get CS2BOT_TLS_MODE)"
tls_mode="${tls_mode:-auto}"
# Caddy's site address, the API's Host allow-list and the CSRF origin are all built from this value.
if [[ ! "$public_host" =~ ^[A-Za-z0-9.-]+$ ]]; then
    die "CS2BOT_PUBLIC_HOST must be a bare domain or IPv4 address like bot.example.pl (no https://, port, path or IPv6); got '$public_host'"
fi
if [[ "$public_host" =~ ^[0-9.]+$ || "$public_host" == *:* ]] && [[ "$tls_mode" != "internal" ]]; then
    warn "CS2BOT_PUBLIC_HOST is an IP address: set CS2BOT_TLS_MODE=internal (no public certificate for IPs)"
fi
if [[ "$tls_mode" != "auto" && "$tls_mode" != "internal" ]]; then
    die "CS2BOT_TLS_MODE must be 'auto' or 'internal' (got '$tls_mode')"
fi

dry_run="$(env_get CS2BOT_EXECUTOR__DRY_RUN)"
if [[ -n "$dry_run" ]] && ! is_true "$dry_run"; then
    warn "CS2BOT_EXECUTOR__DRY_RUN=$dry_run: the bot will place REAL bets with real money"
else
    ok "dry-run mode (no real bets)"
fi
for key in CS2BOT_BETCLIC__USERNAME CS2BOT_BETCLIC__PASSWORD CS2BOT_TELEGRAM__BOT_TOKEN CS2BOT_TELEGRAM__CHAT_ID; do
    [[ -n "$(env_get "$key")" ]] || warn "$key is empty in .env (see docs/VPS_SETUP.md, section 5)"
done

app_uid="$(env_get APP_UID)"
app_uid="${app_uid:-10001}"
app_gid="$(env_get APP_GID)"
app_gid="${app_gid:-10001}"
[[ "$app_uid" =~ ^[0-9]+$ && "$app_gid" =~ ^[0-9]+$ ]] || die "APP_UID / APP_GID in .env must be numbers"
if [[ "$app_uid" == "0" || "$app_gid" == "0" ]]; then
    die "APP_UID/APP_GID is 0 (root) in .env: the bot must not run as root - run $DEPLOY_DIR/init-env.sh (it replaces 0 with the checkout owner's uid) or set both to the cs2bot user's uid/gid (id cs2bot)"
fi
for dir in "$DATA_DIR" "$LOG_DIR"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir"
    fi
    owner="$(stat -c '%u' "$dir")"
    if [[ "$owner" != "$app_uid" ]]; then
        if [[ "$(id -u)" -eq 0 ]]; then
            chown -R "$app_uid:$app_gid" "$dir"
        else
            die "$dir belongs to uid $owner, the container runs as uid $app_uid: sudo chown -R $app_uid:$app_gid $dir"
        fi
    fi
done
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

avail_gb="$(df -BG --output=avail "$REPO_ROOT" | tail -n 1 | tr -dc '0-9')"
if [[ -n "$avail_gb" ]] && ((avail_gb < 10)); then
    warn "only ${avail_gb} GB free disk space (demos need tens of GB): docker system df / prune.sh"
fi

take_lock 600

# ---------------------------------------------------------------------------------------- build
if ((BUILD)); then
    info "building the image (first time: 5-15 minutes)"
    compose build --pull app
fi
if ((SELFTEST)); then
    info "image self-test"
    compose run --rm --no-deps app selftest || die "the self-test failed; the running bot was not changed"
fi

# ------------------------------------------------------------------------------------------- up
info "starting / updating the services"
compose up -d --remove-orphans

if ((WAIT)); then
    info "waiting for PostgreSQL"
    wait_healthy postgres 180 || {
        compose logs --tail=50 postgres
        die "PostgreSQL did not become healthy"
    }
    info "waiting for the bot (migrations, models; up to ${TIMEOUT}s)"
    if ! wait_healthy app "$TIMEOUT"; then
        compose logs --tail=80 app
        die "the bot did not become healthy: deploy/linux/logs.sh app"
    fi
    ok "the bot is healthy"
fi

compose ps
cat <<EOF

Dashboard:  https://${public_host}   (user: $(env_get CS2BOT_API__USERNAME), password: CS2BOT_API__PASSWORD in .env)
Status:     $DEPLOY_DIR/status.sh      Logs: $DEPLOY_DIR/logs.sh
EOF
if [[ "$tls_mode" == "internal" ]]; then
    echo "TLS:        Caddy's own certificate (IP address): the browser warns once - continue to the site."
fi
