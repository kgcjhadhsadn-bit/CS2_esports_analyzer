#!/usr/bin/env bash
# Development start on Linux / macOS: PostgreSQL in Docker + the bot natively with uv.
#
#   scripts/dev.sh              start PostgreSQL, apply migrations, run "cs2bot up" (Ctrl+C stops it)
#   scripts/dev.sh --db-only    only start PostgreSQL and apply migrations
#   scripts/dev.sh --seed       also fill an EMPTY database with synthetic demo data first
#   scripts/dev.sh --stop       stop PostgreSQL (data is kept)
#
# The owner's Windows PC uses deploy/windows/*.ps1 instead. Settings come from .env (created from
# .env.example on the first run - fill in your own keys; never commit .env).
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
cd "$ROOT"

DB_ONLY=0
SEED=0
STOP=0
for arg in "$@"; do
    case "$arg" in
        --db-only) DB_ONLY=1 ;;
        --seed) SEED=1 ;;
        --stop) STOP=1 ;;
        -h | --help)
            awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$SELF"
            exit 0
            ;;
        *)
            echo "unknown option: $arg (see --help)" >&2
            exit 2
            ;;
    esac
done

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: '$1' is not installed. $2" >&2
        exit 1
    fi
}
need docker "Install Docker (https://docs.docker.com/get-docker/)."
need uv "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"

if ! docker info >/dev/null 2>&1; then
    echo "error: the Docker daemon is not running (start Docker Desktop or 'sudo systemctl start docker')." >&2
    exit 1
fi

if [[ "$STOP" == 1 ]]; then
    docker compose -f "$ROOT/docker-compose.yml" stop
    exit 0
fi

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env from .env.example - edit it (API keys, bankroll) before going further."
fi

# Same uv behaviour as the Windows scripts: never re-resolve the lock file.
export UV_FROZEN=1
export PYTHONUTF8=1

echo "==> Starting PostgreSQL (docker compose up -d --wait)"
docker compose -f "$ROOT/docker-compose.yml" up -d --wait

echo "==> Python packages (uv sync)"
uv sync --frozen

echo "==> Database migrations (cs2bot db upgrade)"
uv run cs2bot db upgrade

if [[ "$SEED" == 1 ]]; then
    echo "==> Synthetic demo data (cs2bot db seed-synthetic)"
    uv run cs2bot db seed-synthetic
fi

if [[ "$DB_ONLY" == 1 ]]; then
    echo "PostgreSQL is running on 127.0.0.1:${POSTGRES_PORT:-5432}."
    exit 0
fi

echo "==> Starting the bot (uv run cs2bot up) - dashboard on http://127.0.0.1:8000, Ctrl+C stops it"
exec uv run cs2bot up
