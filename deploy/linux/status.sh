#!/usr/bin/env bash
# Overview of the cs2bot VPS: containers and health, trading state, disk, backups, timers.
#
#   deploy/linux/status.sh [--doctor]
#
# --doctor also runs `cs2bot doctor` inside the bot container (database, migrations, Chromium,
# display, Telegram, credentials, disk). Exit code 1 when a service is not healthy.
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

DOCTOR=0
case "${1:-}" in
    --doctor) DOCTOR=1 ;;
    -h | --help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) die "unknown option: $1 (see --help)" ;;
esac

require_cmd docker
require_env_file
problems=0

printf '%s== Services ==%s\n' "$C_BOLD" "$C_RESET"
compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}' || compose ps
for service in postgres app caddy; do
    health="$(service_health "$service")"
    case "$health" in
        healthy | running) ok "$service: $health" ;;
        starting) warn "$service: starting (first start: migrations + model training)" ;;
        *)
            err "$service: $health"
            problems=$((problems + 1))
            ;;
    esac
done

if service_running app; then
    printf '\n%s== Trading ==%s\n' "$C_BOLD" "$C_RESET"
    compose exec -T app cs2bot control status 2>&1 || warn "could not read the trading state"
    if ((DOCTOR)); then
        printf '\n%s== Doctor ==%s\n' "$C_BOLD" "$C_RESET"
        compose exec -T app cs2bot doctor 2>&1 || problems=$((problems + 1))
    fi
fi

printf '\n%s== Settings ==%s\n' "$C_BOLD" "$C_RESET"
dry_run="$(env_get CS2BOT_EXECUTOR__DRY_RUN)"
if [[ -n "$dry_run" ]] && ! is_true "$dry_run"; then
    echo "betting mode:  LIVE (real money)"
else
    echo "betting mode:  dry run (no real bets)"
fi
echo "dashboard:     https://$(env_get CS2BOT_PUBLIC_HOST) (TLS: $(env_get CS2BOT_TLS_MODE))"
proxy="$(env_get CS2BOT_BETCLIC__PROXY_URL)"
echo "betclic proxy: $([[ -n "$proxy" ]] && echo configured || echo 'none (direct)')"
hltv_proxy="$(env_get CS2BOT_HLTV__PROXY_URL)"
hltv_mode="$(env_get CS2BOT_HLTV__PROXY_MODE)"
echo "hltv proxy:    $([[ -n "$hltv_proxy" ]] && echo "configured (${hltv_mode:-after_block})" || echo 'none (direct)')"

printf '\n%s== Disk ==%s\n' "$C_BOLD" "$C_RESET"
df -h "$REPO_ROOT" | tail -n 1 | awk '{print "disk:          " $4 " free of " $2 " (" $5 " used)"}'
du -sh "$DATA_DIR" 2>/dev/null | awk '{print "data/:         " $1}' || true
docker system df 2>/dev/null || true

printf '\n%s== Backups ==%s\n' "$C_BOLD" "$C_RESET"
latest="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'cs2bot-[0-9]*.dump' -printf '%T@ %p\n' 2>/dev/null |
    sort -rn | head -n 1 | cut -d' ' -f2-)"
if [[ -n "$latest" ]]; then
    age_h=$((($(date +%s) - $(stat -c %Y "$latest")) / 3600))
    echo "latest:        $(basename "$latest") ($(du -h "$latest" | cut -f1), ${age_h} h ago)"
    if ((age_h > 36)); then
        warn "the newest backup is older than 36 h: systemctl status cs2bot-backup.service"
    fi
else
    warn "no backup yet (deploy/linux/backup.sh)"
fi
echo "count:         $(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'cs2bot-*.dump' 2>/dev/null | wc -l)"
if [[ -n "$(env_get RCLONE_REMOTE)" ]]; then
    echo "off-site:      $(env_get RCLONE_REMOTE)"
fi

if command -v systemctl >/dev/null 2>&1; then
    printf '\n%s== Timers ==%s\n' "$C_BOLD" "$C_RESET"
    systemctl list-timers --all --no-pager 'cs2bot-*' 2>/dev/null || true
fi

if ((problems)); then
    printf '\n'
    err "$problems problem(s): deploy/linux/logs.sh <service>"
    exit 1
fi
