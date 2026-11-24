#!/usr/bin/env bash
# See (and use) the bot's browser on the server - e.g. to type a Betclic SMS code once.
#
#   deploy/linux/vnc.sh [--timeout SECONDS] [--view-only]
#   deploy/linux/vnc.sh --stop
#
# Starts x11vnc inside the bot container on the Xvfb display (:99) with a fresh one-time password.
# It listens only on the VPS's own 127.0.0.1:${VNC_HOST_PORT:-5900} (never on the internet) and ends
# when you disconnect, or after --timeout seconds (default 600) without a connection.
#
# On YOUR computer, open an SSH tunnel and point a VNC viewer (TigerVNC, RealVNC Viewer) at it:
#   ssh -N -L 5900:127.0.0.1:5900 cs2bot@<vps>       then connect the viewer to   localhost:5900
# Tip: press the dashboard's "Open Betclic login" button first, so the login page is on screen.
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

TIMEOUT=600
VIEW_ONLY=0
STOP=0
while (($#)); do
    case "$1" in
        --timeout) TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
        --view-only) VIEW_ONLY=1; shift ;;
        --stop) STOP=1; shift ;;
        -h | --help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || die "--timeout must be a number of seconds"

require_cmd docker openssl
require_env_file
service_running app || die "the bot container is not running (deploy/linux/deploy.sh)"

# pkill/pgrep come from procps in the image.
if ((STOP)); then
    if compose exec -T app pkill -x x11vnc; then
        ok "x11vnc stopped"
    else
        echo "x11vnc was not running"
    fi
    exit 0
fi
if compose exec -T app pgrep -x x11vnc >/dev/null 2>&1; then
    die "x11vnc is already running in the container (stop it with: $0 --stop)"
fi

password="$(openssl rand -base64 12 | tr -dc 'A-Za-z0-9' | head -c 8)"
# VNC passwords are limited to 8 characters; the file lives in the container's tmpfs /tmp only.
printf '%s\n' "$password" | compose exec -T app sh -c \
    'umask 077 && read -r pw && x11vnc -storepasswd "$pw" /tmp/vnc.pass >/dev/null 2>&1'

args=(x11vnc -display :99 -rfbport 5900 -rfbauth /tmp/vnc.pass -once -timeout "$TIMEOUT"
    -shared -noxdamage -quiet -o /tmp/x11vnc.log)
if ((VIEW_ONLY)); then
    args+=(-viewonly)
fi
compose exec -d -T app "${args[@]}"

port="$(env_get VNC_HOST_PORT)"
port="${port:-5900}"
host="$(env_get CS2BOT_PUBLIC_HOST)"
sleep 1
compose exec -T app pgrep -x x11vnc >/dev/null 2>&1 || die "x11vnc did not start: docker compose exec app cat /tmp/x11vnc.log"

cat <<EOF

${C_BOLD}VNC is ready${C_RESET} (one connection, closes after ${TIMEOUT}s without one).
  1. On your computer:  ssh -N -L 5900:127.0.0.1:${port} $(id -un)@${host:-<vps>}
  2. VNC viewer:        localhost:5900
  3. Password:          ${password}
Never open port ${port} in a firewall; the tunnel is the only safe way in.
EOF
