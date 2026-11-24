#!/usr/bin/env bash
# Database backup: compressed pg_dump (custom format) of the cs2bot database into backups/.
#
#   deploy/linux/backup.sh [--label WORD] [--keep-days N] [--keep-at-least N] [--no-prune]
#                          [--no-offsite] [--with-data] [--quiet]
#
# - backups/cs2bot-YYYYMMDD-HHMMSS[-label].dump, verified with pg_restore --list before it gets its
#   final name; restore with deploy/linux/restore.sh.
# - Retention: files older than --keep-days (BACKUP_KEEP_DAYS in .env, default 14) are deleted, but
#   the newest --keep-at-least (default 3) always stay.
# - --with-data (or BACKUP_INCLUDE_DATA=true): also data/models and data/browser (the Betclic session
#   and placement journal; browser caches excluded) as backups/cs2bot-data-....tar.gz. These contain
#   your Betclic login cookies: keep backups/ private (it is chmod 700).
# - Off-site copy: when RCLONE_REMOTE is set in .env (e.g. "b2:cs2bot-backups", configured with
#   `rclone config` as the cs2bot user), each new file is copied there and remote files older than
#   RCLONE_KEEP_DAYS (default 30) are deleted.
# Run nightly by the cs2bot-backup.timer systemd timer (provision.sh). Exit code 0 = backup written.
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

LABEL=""
KEEP_DAYS=""
KEEP_AT_LEAST=3
PRUNE=1
OFFSITE=1
WITH_DATA=""
QUIET=0

while (($#)); do
    case "$1" in
        --label) LABEL="${2:?--label needs a value}"; shift 2 ;;
        --keep-days) KEEP_DAYS="${2:?--keep-days needs a value}"; shift 2 ;;
        --keep-at-least) KEEP_AT_LEAST="${2:?--keep-at-least needs a value}"; shift 2 ;;
        --no-prune) PRUNE=0; shift ;;
        --no-offsite) OFFSITE=0; shift ;;
        --with-data) WITH_DATA=1; shift ;;
        --quiet) QUIET=1; shift ;;
        -h | --help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

[[ -z "$LABEL" || "$LABEL" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || die "--label: letters, digits, - and _ only"
require_cmd docker flock
require_env_file

KEEP_DAYS="${KEEP_DAYS:-$(env_get BACKUP_KEEP_DAYS)}"
KEEP_DAYS="${KEEP_DAYS:-14}"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ && "$KEEP_AT_LEAST" =~ ^[0-9]+$ ]] || die "--keep-days / --keep-at-least must be numbers"
if [[ -z "$WITH_DATA" ]]; then
    WITH_DATA=0
    if is_true "$(env_get BACKUP_INCLUDE_DATA)"; then
        WITH_DATA=1
    fi
fi

PG_USER="$(env_get POSTGRES_USER)"
PG_USER="${PG_USER:-cs2bot}"
PG_DB="$(env_get POSTGRES_DB)"
PG_DB="${PG_DB:-cs2bot}"

mkdir -p "$BACKUP_DIR" "$LOG_DIR" 2>/dev/null || true
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/backup.log"

say() {
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    { printf '%s\n' "$line" >>"$LOG_FILE"; } 2>/dev/null || true
    ((QUIET)) || printf '%s\n' "$line"
}
fail() {
    say "FAILED: $*"
    die "$*"
}

take_lock 900

service_running postgres || fail "the postgres container is not running (deploy/linux/deploy.sh starts it)"

stamp="$(date +%Y%m%d-%H%M%S)"
name="cs2bot-${stamp}${LABEL:+-$LABEL}.dump"
final="$BACKUP_DIR/$name"
partial="$BACKUP_DIR/.${name}.partial"
trap 'rm -f "$partial"' EXIT

say "backup of database '$PG_DB' -> $final"
# Custom format is compressed and lets pg_restore pick objects; -Z 6 balances size and CPU.
if ! compose exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" --format=custom --compress=6 >"$partial"; then
    fail "pg_dump failed"
fi
[[ -s "$partial" ]] || fail "pg_dump wrote an empty file"
# Verify: the archive's table of contents must be readable.
if ! compose exec -T postgres pg_restore --list <"$partial" >/dev/null; then
    fail "the dump could not be read back (pg_restore --list)"
fi
chmod 600 "$partial"
mv -f "$partial" "$final"
trap - EXIT
size="$(du -h "$final" | cut -f1)"
say "OK $final ($size)"
new_files=("$final")

if ((WITH_DATA)); then
    data_archive="$BACKUP_DIR/cs2bot-data-${stamp}${LABEL:+-$LABEL}.tar.gz"
    parts=()
    for sub in models browser; do
        [[ -d "$DATA_DIR/$sub" ]] && parts+=("data/$sub")
    done
    if ((${#parts[@]})); then
        # Browser caches are large and worthless in a backup; a profile copied while the browser runs
        # may miss the last seconds of writes (cookies are rewritten often, so this is harmless).
        if tar -C "$REPO_ROOT" -czf "$data_archive.partial" \
            --exclude='*/Cache' --exclude='*/Code Cache' --exclude='*/GPUCache' \
            --exclude='*/Service Worker/CacheStorage' --exclude='*/ShaderCache' \
            --exclude='*/GrShaderCache' --exclude='*/component_crx_cache' \
            --warning=no-file-changed --warning=no-file-removed "${parts[@]}"; then
            chmod 600 "$data_archive.partial"
            mv -f "$data_archive.partial" "$data_archive"
            say "OK $data_archive ($(du -h "$data_archive" | cut -f1))"
            new_files+=("$data_archive")
        else
            rm -f "$data_archive.partial"
            say "WARNING: the data archive failed (database backup is fine)"
        fi
    fi
fi

# ----------------------------------------------------------------------------------------- retention
if ((PRUNE)); then
    for pattern in 'cs2bot-[0-9]*.dump' 'cs2bot-data-*.tar.gz'; do
        mapfile -t files < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' |
            sort -rn | cut -d' ' -f2-)
        index=0
        for file in "${files[@]}"; do
            index=$((index + 1))
            ((index > KEEP_AT_LEAST)) || continue
            if [[ -n "$(find "$file" -maxdepth 0 -mtime +"$KEEP_DAYS" 2>/dev/null)" ]]; then
                rm -f -- "$file"
                say "deleted old backup $(basename "$file")"
            fi
        done
    done
fi

# ------------------------------------------------------------------------------------------ off-site
REMOTE="$(env_get RCLONE_REMOTE)"
if ((OFFSITE)) && [[ -n "$REMOTE" ]]; then
    if ! command -v rclone >/dev/null 2>&1; then
        say "WARNING: RCLONE_REMOTE is set but rclone is not installed (apt install rclone)"
    else
        remote_keep="$(env_get RCLONE_KEEP_DAYS)"
        remote_keep="${remote_keep:-30}"
        for file in "${new_files[@]}"; do
            if rclone copy --quiet "$file" "$REMOTE"; then
                say "copied $(basename "$file") to $REMOTE"
            else
                say "WARNING: off-site copy of $(basename "$file") to $REMOTE failed"
            fi
        done
        if [[ "$remote_keep" =~ ^[0-9]+$ ]] && ((remote_keep > 0)); then
            rclone delete --quiet --min-age "${remote_keep}d" --include 'cs2bot-*' "$REMOTE" ||
                say "WARNING: could not delete old off-site backups in $REMOTE"
        fi
    fi
fi
exit 0
