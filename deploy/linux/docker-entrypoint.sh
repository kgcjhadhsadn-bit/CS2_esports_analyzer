#!/usr/bin/env bash
# Entrypoint of the cs2bot cloud image (PID 1 is tini, which forwards signals to this script).
#
#   up        (default) Xvfb + `cs2bot db upgrade` + `cs2bot up`, supervised: SIGTERM / SIGINT are
#             forwarded to the bot (clean shutdown, up to ~90 s), and if Xvfb dies the bot is stopped
#             and the container exits non-zero so Docker restarts it.
#   selftest  Xvfb + deploy/linux/selftest.py (Chromium headful/headless, dashboard, tools); no DB.
#   migrate   only `cs2bot db upgrade`.
#   <other>   executed as given, e.g. `docker compose run --rm app cs2bot doctor` or `bash`.
#
# Environment (all optional; docker-compose.cloud.yml sets the important ones):
#   CS2BOT_PUBLIC_HOST       domain or IP of the dashboard behind Caddy. When CS2BOT_API__ALLOWED_HOSTS /
#                            CS2BOT_API__CORS_ORIGINS are not set they are derived from it, so the app
#                            accepts the Host header and the https Origin that Caddy passes through.
#   DISPLAY                  X display of Xvfb (default :99)
#   XVFB_SCREEN              Xvfb screen geometry (default 1920x1080x24)
#   CS2BOT_APP_DIR           application directory (default /app)
#   CS2BOT_LOG_FILE          copy of the bot's output (default $CS2BOT_APP_DIR/logs/cs2bot.log; "" = off)
#   DB_UPGRADE_ATTEMPTS      tries of `cs2bot db upgrade` before giving up (default 10, 6 s apart)
set -euo pipefail

APP_DIR="${CS2BOT_APP_DIR:-/app}"
export DISPLAY="${DISPLAY:-:99}"
XVFB_SCREEN="${XVFB_SCREEN:-1920x1080x24}"
LOG_FILE="${CS2BOT_LOG_FILE-${APP_DIR}/logs/cs2bot.log}"
DB_UPGRADE_ATTEMPTS="${DB_UPGRADE_ATTEMPTS:-10}"

XVFB_PID=""
APP_PID=""
TEE_PID=""
STOP_REQUESTED=0

log() { printf '%s entrypoint: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

die() {
    log "ERROR: $*"
    exit 1
}

# --------------------------------------------------------------------------------------- settings

# JSON list with one host (pydantic-settings parses list values from JSON).
json_list() { printf '["%s"]' "$1"; }

derive_api_settings() {
    export CS2BOT_API__HOST="${CS2BOT_API__HOST:-0.0.0.0}"
    export CS2BOT_API__PORT="${CS2BOT_API__PORT:-8000}"
    local public="${CS2BOT_PUBLIC_HOST:-}"
    public="${public#https://}"
    public="${public#http://}"
    public="${public%%/*}"
    if [[ -n "$public" ]]; then
        local shown="$public"
        # A bare IPv6 address is written in brackets in Host headers and origins.
        if [[ "$public" == *:*:* && "$public" != \[* ]]; then
            shown="[$public]"
        fi
        if [[ -z "${CS2BOT_API__ALLOWED_HOSTS:-}" ]]; then
            CS2BOT_API__ALLOWED_HOSTS="$(json_list "$shown")"
            export CS2BOT_API__ALLOWED_HOSTS
        fi
        if [[ -z "${CS2BOT_API__CORS_ORIGINS:-}" ]]; then
            CS2BOT_API__CORS_ORIGINS="$(json_list "https://${shown}")"
            export CS2BOT_API__CORS_ORIGINS
        fi
    fi
    case "$CS2BOT_API__HOST" in
        127.0.0.1 | localhost | ::1) ;;
        *)
            # config.py refuses a non-loopback API without both; say so before Python does.
            [[ -n "${CS2BOT_API__PASSWORD:-}" ]] ||
                die "CS2BOT_API__PASSWORD is empty: set it in .env (deploy/linux/provision.sh generates one)"
            [[ -n "${CS2BOT_API__SESSION_SECRET:-}" ]] ||
                die "CS2BOT_API__SESSION_SECRET is empty: set it in .env (provision.sh generates one)"
            ;;
    esac
}

check_writable() {
    local dir
    for dir in "$APP_DIR/data" "$APP_DIR/logs"; do
        mkdir -p "$dir" 2>/dev/null || true
        if ! { : >"$dir/.write-test"; } 2>/dev/null; then
            die "$dir is not writable by uid $(id -u). On the host run: sudo chown -R $(id -u):$(id -g) data logs"
        fi
        rm -f "$dir/.write-test"
    done
}

# ------------------------------------------------------------------------------------------- Xvfb

start_xvfb() {
    local number="${DISPLAY#*:}"
    number="${number%%.*}"
    local socket="/tmp/.X11-unix/X${number}"
    # A container restart keeps /tmp unless it is a tmpfs: remove a stale lock of a killed Xvfb.
    if [[ -e "/tmp/.X${number}-lock" ]]; then
        local old_pid
        old_pid="$(tr -cd '0-9' <"/tmp/.X${number}-lock" || true)"
        if [[ -z "$old_pid" ]] || ! kill -0 "$old_pid" 2>/dev/null; then
            rm -f "/tmp/.X${number}-lock" "$socket"
        fi
    fi
    mkdir -p /tmp/.X11-unix
    chmod 1777 /tmp/.X11-unix 2>/dev/null || true
    # -nolisten tcp: only the Unix socket inside the container; nothing is reachable from outside.
    Xvfb "$DISPLAY" -screen 0 "$XVFB_SCREEN" -nolisten tcp -noreset -dpi 96 >/tmp/xvfb.log 2>&1 &
    XVFB_PID=$!
    for _ in $(seq 1 100); do
        if [[ -S "$socket" ]]; then
            log "Xvfb ready on $DISPLAY ($XVFB_SCREEN, pid $XVFB_PID)"
            return 0
        fi
        if ! kill -0 "$XVFB_PID" 2>/dev/null; then
            cat /tmp/xvfb.log >&2 || true
            die "Xvfb exited during start-up"
        fi
        sleep 0.1
    done
    cat /tmp/xvfb.log >&2 || true
    die "Xvfb did not create $socket within 10 s"
}

stop_xvfb() {
    if [[ -n "$XVFB_PID" ]] && kill -0 "$XVFB_PID" 2>/dev/null; then
        kill -TERM "$XVFB_PID" 2>/dev/null || true
        wait "$XVFB_PID" 2>/dev/null || true
    fi
    XVFB_PID=""
}

# --------------------------------------------------------------------------------------- database

db_upgrade() {
    local attempt
    for attempt in $(seq 1 "$DB_UPGRADE_ATTEMPTS"); do
        if cs2bot db upgrade; then
            return 0
        fi
        log "database migration failed (attempt $attempt/$DB_UPGRADE_ATTEMPTS); retrying in 6 s"
        sleep 6
    done
    die "could not migrate the database (is the postgres service healthy? docker compose logs postgres)"
}

# ------------------------------------------------------------------------------------- supervision

# Lets the tee flush the bot's last lines (bounded: a leftover child holding the pipe cannot block us).
stop_tee() {
    [[ -n "$TEE_PID" ]] || return 0
    for _ in $(seq 1 50); do
        kill -0 "$TEE_PID" 2>/dev/null || return 0
        sleep 0.1
    done
    kill -TERM "$TEE_PID" 2>/dev/null || true
}

# SIGINT is forwarded as SIGTERM: a non-interactive bash starts background jobs with SIGINT ignored,
# so the bot would never see it. `cs2bot up` shuts down cleanly on SIGTERM.
forward_signal() {
    local sig="$1"
    STOP_REQUESTED=1
    if [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null; then
        log "SIG$sig received: stopping cs2bot (pid $APP_PID) with SIGTERM"
        kill -TERM "$APP_PID" 2>/dev/null || true
    fi
}

# Waits for APP_PID, surviving signal interruptions; prints its exit status.
wait_app() {
    local status
    while :; do
        status=0
        wait "$APP_PID" || status=$?
        # wait returns >128 when a trapped signal interrupted it while the bot still runs.
        if ((status > 128)) && kill -0 "$APP_PID" 2>/dev/null; then
            continue
        fi
        printf '%s' "$status"
        return 0
    done
}

run_up() {
    derive_api_settings
    check_writable
    start_xvfb
    db_upgrade

    trap 'forward_signal TERM' TERM
    trap 'forward_signal INT' INT

    if [[ -n "$LOG_FILE" ]]; then
        mkdir -p "$(dirname "$LOG_FILE")"
        log "starting cs2bot up (output also appended to $LOG_FILE)"
        # The bot writes into a tee (container stdout + the log file). Opening the tee first keeps
        # $! = the bot itself; tee -a appends (O_APPEND), so the host's logrotate can copytruncate.
        exec 3> >(tee -a "$LOG_FILE")
        TEE_PID=$!
        cs2bot up >&3 2>&3 &
        APP_PID=$!
        exec 3>&- # only the bot holds the pipe now: tee ends when the bot's output closes
    else
        log "starting cs2bot up"
        cs2bot up &
        APP_PID=$!
    fi
    # A signal that arrived after the traps were set but before APP_PID was known was not forwarded.
    if ((STOP_REQUESTED)); then
        log "stop requested during start-up: stopping cs2bot (pid $APP_PID) with SIGTERM"
        kill -TERM "$APP_PID" 2>/dev/null || true
    fi

    local status="" done_pid
    while [[ -z "$status" ]]; do
        done_pid=""
        local rc=0
        wait -n -p done_pid "$APP_PID" "$XVFB_PID" || rc=$?
        if [[ -z "${done_pid:-}" ]]; then
            continue # a trapped signal interrupted wait; it was forwarded, keep waiting
        fi
        if [[ "${done_pid:-}" == "$APP_PID" ]]; then
            status="$rc"
        else
            XVFB_PID=""
            log "Xvfb exited unexpectedly (status $rc): stopping cs2bot so Docker restarts the container"
            kill -TERM "$APP_PID" 2>/dev/null || true
            wait_app >/dev/null
            status=70
        fi
    done
    trap - TERM INT
    stop_xvfb
    stop_tee
    log "cs2bot exited with status $status"
    exit "$status"
}

run_selftest() {
    check_writable
    start_xvfb
    local status=0
    python "$APP_DIR/deploy/linux/selftest.py" || status=$?
    stop_xvfb
    exit "$status"
}

cmd="${1:-up}"
case "$cmd" in
    up)
        run_up
        ;;
    selftest)
        export CS2BOT_API__HOST="${CS2BOT_API__HOST:-127.0.0.1}" # no password needed for a bare run
        derive_api_settings
        run_selftest
        ;;
    migrate)
        db_upgrade
        ;;
    *)
        exec "$@"
        ;;
esac
