#!/usr/bin/env bash
# Lint, type-check and test - the same checks as CI (.github/workflows/ci.yml).
#
#   scripts/test.sh                  ruff check + ruff format --check + mypy + pytest
#   scripts/test.sh --quick          pytest only
#   scripts/test.sh --fix            ruff check --fix + ruff format first, then everything
#   scripts/test.sh --pg             also run the PostgreSQL tests against a throw-away database
#                                    "cs2bot_test" in the docker-compose PostgreSQL
#   scripts/test.sh -- <pytest args> pass the rest to pytest (e.g. -- tests/engine -k kelly)
#
# Environment: PYTEST_WORKERS (default auto) sets pytest-xdist's -n.
# Browser tests (@pytest.mark.browser) need Playwright's Chromium at /opt/pw-browsers/chromium (a
# symlink to the chrome binary works; the executor tests also honour PLAYWRIGHT_CHROMIUM_EXECUTABLE);
# without it they are skipped.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
cd "$ROOT"

QUICK=0
FIX=0
PG=0
PYTEST_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --quick) QUICK=1 ;;
        --fix) FIX=1 ;;
        --pg) PG=1 ;;
        --)
            shift
            PYTEST_ARGS=("$@")
            break
            ;;
        -h | --help)
            awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$SELF"
            exit 0
            ;;
        *)
            echo "unknown option: $1 (see --help)" >&2
            exit 2
            ;;
    esac
    shift
done

export UV_FROZEN=1
export PYTHONUTF8=1
WORKERS="${PYTEST_WORKERS:-auto}"

if [[ "$FIX" == 1 ]]; then
    echo "==> ruff check --fix / ruff format"
    uv run ruff check --fix src tests
    uv run ruff format src tests
fi

if [[ "$QUICK" == 0 ]]; then
    echo "==> ruff check"
    uv run ruff check src tests
    echo "==> ruff format --check"
    uv run ruff format --check src tests
    echo "==> mypy"
    uv run mypy
fi

# Read a KEY=value from .env without sourcing it (values may contain shell characters).
env_value() {
    local key="$1" default="$2" value=""
    if [[ -f .env ]]; then
        value="$(grep -E "^[[:space:]]*${key}=" .env | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)"
        value="${value%\"}"
        value="${value#\"}"
    fi
    echo "${value:-$default}"
}

if [[ "$PG" == 1 ]]; then
    echo "==> PostgreSQL test database"
    docker compose -f "$ROOT/docker-compose.yml" up -d --wait
    PG_USER="$(env_value POSTGRES_USER cs2bot)"
    PG_PASSWORD="$(env_value POSTGRES_PASSWORD change-me)"
    PG_PORT="$(env_value POSTGRES_PORT 5432)"
    docker exec cs2bot-postgres dropdb --username "$PG_USER" --if-exists --force cs2bot_test
    docker exec cs2bot-postgres createdb --username "$PG_USER" cs2bot_test
    export CS2BOT_TEST_DATABASE_URL="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@127.0.0.1:${PG_PORT}/cs2bot_test"
    echo "==> pytest (not postgres)"
    uv run pytest -q -n "$WORKERS" -m "not postgres" ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
    # The postgres fixture drops and re-creates the schema of one database: run those tests serially.
    echo "==> pytest (postgres)"
    uv run pytest -q -p no:xdist -m postgres ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
else
    echo "==> pytest"
    uv run pytest -q -n "$WORKERS" ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
fi
echo "All checks passed."
