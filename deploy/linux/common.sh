#!/usr/bin/env bash
# Shared helpers of the deploy/linux scripts (sourced, never run directly).
#
# Every script works from the repository checkout that contains it (normally /opt/cs2bot) and talks
# to Docker Compose through `compose`, which always selects docker-compose.cloud.yml and .env.

# shellcheck disable=SC2034  # variables used by the sourcing scripts

if [[ -n "${CS2BOT_COMMON_SOURCED:-}" ]]; then
    return 0
fi
CS2BOT_COMMON_SOURCED=1

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/../.." && pwd)"
COMPOSE_FILE_PATH="$REPO_ROOT/docker-compose.cloud.yml"
ENV_FILE="$REPO_ROOT/.env"
BACKUP_DIR="${CS2BOT_BACKUP_DIR:-$REPO_ROOT/backups}"
LOG_DIR="$REPO_ROOT/logs"
DATA_DIR="$REPO_ROOT/data"

if [[ -t 1 ]]; then
    C_RED=$'\033[31m' C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_BOLD=$'\033[1m' C_RESET=$'\033[0m'
else
    C_RED="" C_GREEN="" C_YELLOW="" C_BOLD="" C_RESET=""
fi

info() { printf '%s==>%s %s\n' "$C_BOLD" "$C_RESET" "$*"; }
ok() { printf '%s[ok]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err() { printf '%s[error]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
die() {
    err "$*"
    exit 1
}

require_cmd() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is not installed (run deploy/linux/provision.sh first)"
    done
}

# Value of KEY in .env, following the rules of Docker Compose / python-dotenv closely enough for the
# keys these scripts read: the last assignment wins, "export " is allowed, surrounding single or double
# quotes are removed and an unquoted " #" starts a comment. Prints nothing when the key is missing.
env_get() {
    local key="$1" file="${2:-$ENV_FILE}" line value=""
    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line#export }"
        [[ "$line" == "$key="* ]] || continue
        value="${line#*=}"
        if [[ "$value" == \"*\"* ]]; then
            value="${value#\"}"
            value="${value%%\"*}"
        elif [[ "$value" == \'*\'* ]]; then
            value="${value#\'}"
            value="${value%%\'*}"
        else
            value="${value%% #*}"
            value="${value%"${value##*[![:space:]]}"}"
        fi
    done <"$file"
    printf '%s' "$value"
}

# Sets KEY=VALUE in .env: replaces the first uncommented assignment of KEY (dropping later duplicates)
# or appends it. VALUE is written as given (callers quote it when needed).
env_set() {
    local key="$1" value="$2" file="${3:-$ENV_FILE}" tmp
    tmp="$(mktemp "${file}.XXXXXX")"
    awk -v key="$key" -v value="$value" '
        BEGIN { done = 0 }
        {
            line = $0
            sub(/^[ \t]*export[ \t]+/, "", line)
            if (index(line, key "=") == 1) {
                if (!done) { print key "=" value; done = 1 }
                next
            }
            print
        }
        END { if (!done) print key "=" value }
    ' "$file" >"$tmp"
    chmod --reference="$file" "$tmp" 2>/dev/null || chmod 600 "$tmp"
    chown --reference="$file" "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$file"
}

# Truthy like pydantic: 1 / true / yes / on (any case).
is_true() {
    case "${1,,}" in
        1 | true | yes | on | y | t) return 0 ;;
        *) return 1 ;;
    esac
}

require_env_file() {
    [[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found: run deploy/linux/provision.sh (or copy .env.example and fill it in)"
    local mode
    mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo 600)"
    if [[ "$mode" != "600" && "$mode" != "400" ]]; then
        warn ".env is readable by other users (mode $mode): fixing with chmod 600"
        chmod 600 "$ENV_FILE" || warn "could not chmod .env - do it yourself: chmod 600 $ENV_FILE"
    fi
}

compose() {
    docker compose --project-directory "$REPO_ROOT" -f "$COMPOSE_FILE_PATH" --env-file "$ENV_FILE" "$@"
}

# Container id of a compose service ("" when it does not exist).
service_container() {
    compose ps --all -q "$1" 2>/dev/null | head -n 1
}

service_health() {
    local id
    id="$(service_container "$1")"
    [[ -n "$id" ]] || {
        printf 'missing'
        return 0
    }
    docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" \
        2>/dev/null || printf 'missing'
}

service_running() {
    local id
    id="$(service_container "$1")"
    [[ -n "$id" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" == "true" ]]
}

# Waits until SERVICE is healthy (or running, when it has no healthcheck); TIMEOUT in seconds.
wait_healthy() {
    local service="$1" timeout="${2:-600}" waited=0 status
    while ((waited < timeout)); do
        status="$(service_health "$service")"
        case "$status" in
            healthy | running) return 0 ;;
            unhealthy)
                err "$service is unhealthy"
                return 1
                ;;
            exited | dead)
                err "$service stopped ($status)"
                return 1
                ;;
        esac
        sleep 5
        waited=$((waited + 5))
        if ((waited % 60 == 0)); then
            info "still waiting for $service ($status, ${waited}s of ${timeout}s)"
        fi
    done
    err "$service did not become healthy within ${timeout}s (last status: $status)"
    return 1
}

# Asks a yes/no question (default no). Non-interactive shells get "no" unless ASSUME_YES=1.
confirm() {
    local prompt="$1" answer
    if [[ "${ASSUME_YES:-0}" == "1" ]]; then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        return 1
    fi
    read -r -p "$prompt [y/N] " answer
    [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]]
}

# Runs the rest of the script under one exclusive lock shared by deploy / update / backup / restore,
# so a nightly backup never runs in the middle of a restore. The lock is taken on a read-only handle of
# the compose file (works for root and the cs2bot user alike) and inherited by child scripts, which
# see CS2BOT_LOCK_HELD and do not lock again. WAIT_S = seconds to wait for another holder.
take_lock() {
    local wait_s="${1:-0}"
    if [[ -n "${CS2BOT_LOCK_HELD:-}" ]]; then
        return 0
    fi
    exec 9<"$COMPOSE_FILE_PATH" || die "cannot open $COMPOSE_FILE_PATH"
    if ! flock -w "$wait_s" 9; then
        die "another deploy / update / backup / restore is running; try again later"
    fi
    export CS2BOT_LOCK_HELD=1
}
