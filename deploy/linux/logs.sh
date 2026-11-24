#!/usr/bin/env bash
# Show the logs of the cs2bot services.
#
#   deploy/linux/logs.sh [SERVICE] [--since 2h] [--tail N] [--no-follow]
#   deploy/linux/logs.sh --file          the bot's log file (logs/cs2bot.log, kept 14 days by logrotate)
#   deploy/linux/logs.sh --backup        logs/backup.log
#
# SERVICE: app (default), postgres, caddy or all. Follows new lines until Ctrl+C unless --no-follow.
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

SERVICE="app"
FOLLOW=1
TAIL=200
SINCE=""
FILE=""
while (($#)); do
    case "$1" in
        --file) FILE="$LOG_DIR/cs2bot.log"; shift ;;
        --backup) FILE="$LOG_DIR/backup.log"; shift ;;
        --since) SINCE="${2:?--since needs a value, e.g. 2h}"; shift 2 ;;
        --tail) TAIL="${2:?--tail needs a number}"; shift 2 ;;
        --no-follow) FOLLOW=0; shift ;;
        -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        app | postgres | caddy) SERVICE="$1"; shift ;;
        all) SERVICE=""; shift ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

if [[ -n "$FILE" ]]; then
    [[ -f "$FILE" ]] || die "$FILE does not exist yet"
    if ((FOLLOW)); then
        exec tail -n "$TAIL" -F "$FILE"
    fi
    exec tail -n "$TAIL" "$FILE"
fi

require_cmd docker
require_env_file
args=(logs --tail "$TAIL" --timestamps)
((FOLLOW)) && args+=(--follow)
[[ -n "$SINCE" ]] && args+=(--since "$SINCE")
[[ -n "$SERVICE" ]] && args+=("$SERVICE")
compose "${args[@]}"
