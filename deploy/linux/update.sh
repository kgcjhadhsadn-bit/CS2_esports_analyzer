#!/usr/bin/env bash
# Update cs2bot to the newest version of the repository and redeploy.
#
#   deploy/linux/update.sh [--no-backup] [--branch NAME] [--yes]
#
# 1. backup of the database (label "before-update") unless --no-backup
# 2. git pull --ff-only (refuses when files of the checkout were edited, except .env / config/)
# 3. deploy.sh --selftest: rebuild, image self-test, restart only what changed, wait until healthy
# Local edits of config/*.yaml (Betclic selectors) are kept with `git stash` + `git stash pop`.
set -euo pipefail
# shellcheck source=deploy/linux/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

BACKUP=1
BRANCH=""
while (($#)); do
    case "$1" in
        --no-backup) BACKUP=0; shift ;;
        --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
        -y | --yes) ASSUME_YES=1; shift ;;
        -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done
export ASSUME_YES="${ASSUME_YES:-0}"

require_cmd git docker flock
require_env_file
take_lock 900

cd "$REPO_ROOT"
before="$(git rev-parse --short HEAD)"

if ((BACKUP)); then
    if service_running postgres; then
        info "backup before the update"
        bash "$DEPLOY_DIR/backup.sh" --label before-update --no-prune || die "backup failed; nothing was changed"
    else
        warn "postgres is not running: no backup before the update"
    fi
fi

# Local changes: config/ edits are carried over, anything else stops the update.
dirty="$(git status --porcelain --untracked-files=no)"
other="$(grep -v -E '^.. config/' <<<"$dirty" || true)"
if [[ -n "$other" ]]; then
    err "files of the checkout were changed locally:"
    printf '%s\n' "$other" >&2
    die "undo them (git checkout -- <file>) or commit them, then run update.sh again"
fi
stashed=0
if [[ -n "$dirty" ]]; then
    info "keeping your config/ changes (git stash)"
    git stash push --quiet --message "cs2bot update.sh $(date +%F_%T)" -- config/
    stashed=1
fi

info "git pull"
if [[ -n "$BRANCH" ]]; then
    git fetch --quiet origin "$BRANCH"
    git checkout --quiet "$BRANCH"
fi
if ! git pull --ff-only --quiet; then
    if ((stashed)); then
        git stash pop --quiet || true
    fi
    die "git pull failed (no access to the repository, or the branch diverged)"
fi
if ((stashed)); then
    if ! git stash pop --quiet; then
        warn "your config/ changes conflict with the update: they are kept in 'git stash list'; resolve with git stash show -p"
    fi
fi
after="$(git rev-parse --short HEAD)"
if [[ "$before" == "$after" ]]; then
    info "already up to date ($after); redeploying anyway (base image security updates)"
else
    info "updated $before -> $after"
    git --no-pager log --oneline "$before..$after" | head -n 20
fi

bash "$DEPLOY_DIR/deploy.sh" --selftest
