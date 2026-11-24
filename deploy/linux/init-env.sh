#!/usr/bin/env bash
# Create or complete the VPS .env (provision.sh runs it; safe to run again at any time).
#
#   deploy/linux/init-env.sh [--domain bot.example.pl] [--public-host IP] [--uid N --gid N] [--owner USER]
#
# A missing .env is created from .env.example with the VPS values (autonomy, auto-login, Telegram on,
# DRY RUN on, visible browser) and freshly generated secrets: POSTGRES_PASSWORD,
# CS2BOT_API__PASSWORD, CS2BOT_API__SESSION_SECRET. An existing .env keeps every value you set; only
# missing / empty keys are filled in (--domain always replaces CS2BOT_PUBLIC_HOST). Mode 600.
# Without --domain / --public-host, CS2BOT_PUBLIC_HOST defaults to this machine's outgoing IPv4.
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

DOMAIN=""
PUBLIC_HOST=""
NEW_UID="$(id -u)"
NEW_GID="$(id -g)"
OWNER=""
while (($#)); do
    case "$1" in
        --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
        --public-host) PUBLIC_HOST="${2:?--public-host needs a value}"; shift 2 ;;
        --uid) NEW_UID="${2:?--uid needs a value}"; shift 2 ;;
        --gid) NEW_GID="${2:?--gid needs a value}"; shift 2 ;;
        --owner) OWNER="${2:?--owner needs a value}"; shift 2 ;;
        -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done
require_cmd openssl awk

DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN%%/*}"
if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
    die "--domain: a host name like bot.example.pl (got '$DOMAIN')"
fi

gen_hex() { openssl rand -hex "$1"; }
# Letters and digits only: nothing that needs quoting in .env, a URL or a shell.
gen_password() {
    local out=""
    while ((${#out} < $1)); do
        out+="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9')"
    done
    printf '%s' "${out:0:$1}"
}
is_ip() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$1" == *:*:* ]]; }

detected_host() {
    command -v ip >/dev/null 2>&1 || return 0
    ip -4 route get 1.1.1.1 2>/dev/null |
        awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}'
}

fresh=0
if [[ ! -f "$ENV_FILE" ]]; then
    [[ -f "$REPO_ROOT/.env.example" ]] || die "$REPO_ROOT/.env.example is missing"
    (umask 077 && cp "$REPO_ROOT/.env.example" "$ENV_FILE")
    printf '\n# --- VPS settings (deploy/linux/init-env.sh, docs/VPS_SETUP.md) -------------------------\n' \
        >>"$ENV_FILE"
    fresh=1
fi
chmod 600 "$ENV_FILE"

# set_default KEY VALUE: always on a fresh .env; on an existing one only when KEY is missing / empty.
set_default() {
    if ((fresh)) || [[ -z "$(env_get "$1")" ]]; then
        env_set "$1" "$2"
    fi
}
set_if_empty() {
    if [[ -z "$(env_get "$1")" ]]; then
        env_set "$1" "$2"
    fi
}

set_default COMPOSE_FILE docker-compose.cloud.yml
set_default APP_UID "$NEW_UID"
set_default APP_GID "$NEW_GID"
set_default CS2BOT_ENVIRONMENT prod
set_default CS2BOT_LOG_JSON true
set_default POSTGRES_USER cs2bot
set_default POSTGRES_DB cs2bot
set_if_empty POSTGRES_PASSWORD "$(gen_hex 24)"
# Informational on the VPS: docker-compose.cloud.yml builds the app's database URL from POSTGRES_*.
env_set CS2BOT_DATABASE__URL \
    "postgresql+psycopg://$(env_get POSTGRES_USER):$(env_get POSTGRES_PASSWORD)@postgres:5432/$(env_get POSTGRES_DB)"
# The API listens on the container network behind Caddy: config.py then requires both secrets.
set_default CS2BOT_API__HOST 0.0.0.0
set_if_empty CS2BOT_API__USERNAME admin
set_if_empty CS2BOT_API__PASSWORD "$(gen_password 24)"
set_if_empty CS2BOT_API__SESSION_SECRET "$(gen_hex 32)"

if [[ -n "$DOMAIN" ]]; then
    env_set CS2BOT_PUBLIC_HOST "$DOMAIN"
    env_set CS2BOT_TLS_MODE auto
elif [[ -z "$(env_get CS2BOT_PUBLIC_HOST)" ]]; then
    host="${PUBLIC_HOST:-$(detected_host)}"
    if [[ -n "$host" ]]; then
        env_set CS2BOT_PUBLIC_HOST "$host"
        if is_ip "$host"; then
            env_set CS2BOT_TLS_MODE internal
        else
            env_set CS2BOT_TLS_MODE auto
        fi
        if [[ "$host" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
            warn "$host is a private address (NAT): put the public IP or a domain into CS2BOT_PUBLIC_HOST"
        fi
    else
        warn "could not detect this machine's IP: set CS2BOT_PUBLIC_HOST in .env"
    fi
fi
set_if_empty CS2BOT_TLS_MODE auto

# Unattended operation (PLAN.md 0b). Real money only after the dry-run smoke test (VPS_SETUP.md).
set_default CS2BOT_EXECUTOR__DRY_RUN true
set_default CS2BOT_EXECUTOR__HEADLESS false
set_default CS2BOT_AUTONOMY__ENABLED true
set_default CS2BOT_AUTONOMY__AUTO_LOGIN true
set_default CS2BOT_BETCLIC__CREDENTIALS_SOURCE env
set_default CS2BOT_TELEGRAM__ENABLED true
set_default BACKUP_KEEP_DAYS 14

chmod 600 "$ENV_FILE"
if [[ -n "$OWNER" ]]; then
    chown "$OWNER:$(id -gn "$OWNER")" "$ENV_FILE"
fi
if ((fresh)); then
    ok ".env created with generated secrets (chmod 600): $ENV_FILE"
else
    ok ".env completed; your values were kept: $ENV_FILE"
fi
