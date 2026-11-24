#!/usr/bin/env bash
# Weekly Docker clean-up (cs2bot-prune.timer): unused images older than a week,
# dangling build cache. NEVER touches volumes - the database and the certificates live there.
#
#   deploy/linux/prune.sh [--dry-run]
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if [[ "${1:-}" == "--dry-run" ]]; then
    docker system df
    exit 0
fi
require_cmd docker
before="$(docker system df --format '{{.Type}}: {{.Size}} ({{.Reclaimable}} reclaimable)' 2>/dev/null || true)"
# Images not used by any container and older than 7 days (the running bot's image is in use).
docker image prune --all --force --filter "until=168h" >/dev/null
docker builder prune --force --filter "until=168h" >/dev/null
docker network prune --force --filter "until=168h" >/dev/null
after="$(docker system df --format '{{.Type}}: {{.Size}}' 2>/dev/null || true)"
printf 'before:\n%s\nafter:\n%s\n' "$before" "$after"
