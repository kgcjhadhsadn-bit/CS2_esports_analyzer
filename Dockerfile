# cs2bot cloud image (PLAN.md 0b): the bot (`cs2bot up`: scheduler + executor + API/dashboard),
# Playwright Chromium on an Xvfb virtual display, demo archive tools and the built dashboard.
#
#   build:  docker compose -f docker-compose.cloud.yml build app      (deploy/linux/deploy.sh does this)
#   test:   docker compose -f docker-compose.cloud.yml run --rm --no-deps app selftest
#
# Every base image is pinned; update them deliberately (and together with uv.lock for Playwright).
# Debian 12 (bookworm) is a platform Playwright supports for `install --with-deps`.

ARG PYTHON_IMAGE=python:3.12.16-slim-bookworm
ARG NODE_IMAGE=node:22.23.5-bookworm-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

# ------------------------------------------------------------------------------- uv binary
FROM ${UV_IMAGE} AS uv

# ------------------------------------------------------------------------- dashboard build
FROM ${NODE_IMAGE} AS dashboard
WORKDIR /build/dashboard
ENV CI=true NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
# tsc --noEmit + vite build -> dist/
RUN npm run build && test -f dist/index.html

# ------------------------------------------------------------------------------ app image
FROM ${PYTHON_IMAGE} AS app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    TZ=UTC \
    UV_FROZEN=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/app/.venv/bin:$PATH \
    DISPLAY=:99

# tini: PID 1 (signal forwarding, zombie reaping); xvfb: the virtual display for the visible Betclic
# browser; x11vnc: only started on demand by deploy/linux/vnc.sh (SSH tunnel, never public);
# unar + libarchive-tools (bsdtar): RAR / zip demo archives (each handles archives the other cannot);
# curl: healthcheck; fonts: Polish diacritics and emoji render like on a desktop.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-color-emoji \
        libarchive-tools \
        procps \
        tini \
        tzdata \
        unar \
        x11vnc \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies first (cached until uv.lock changes), then the project itself (editable install of src/).
COPY pyproject.toml uv.lock README.md alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    && cs2bot version

# Chromium of exactly the Playwright version in uv.lock, plus its system libraries (needs root/apt).
# `cs2bot-chromium` wraps it with --disable-gpu: headful Chromium on Xvfb otherwise fails page
# screenshots intermittently ("Unable to capture screenshot"); WebGL still works (SwiftShader).
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright \
    && chrome="$(python -c 'from playwright.sync_api import sync_playwright as s; p = s().start(); print(p.chromium.executable_path); p.stop()')" \
    && test -x "$chrome" \
    && printf '#!/bin/sh\n# Playwright Chromium with --disable-gpu (see Dockerfile).\nexec "%s" --disable-gpu "$@"\n' "$chrome" \
        > /usr/local/bin/cs2bot-chromium \
    && chmod 0755 /usr/local/bin/cs2bot-chromium

# Repository files the app reads at runtime (config/ is also bind-mounted read-only by compose, so a
# selector fix is a `git pull` + restart), the built dashboard and the entrypoint.
COPY config ./config
COPY --from=dashboard /build/dashboard/dist ./dashboard/dist
COPY deploy/linux/docker-entrypoint.sh /usr/local/bin/cs2bot-entrypoint
COPY deploy/linux/selftest.py ./deploy/linux/selftest.py
RUN chmod 0755 /usr/local/bin/cs2bot-entrypoint \
    && sed -i 's/\r$//' /usr/local/bin/cs2bot-entrypoint

# Non-root user. APP_UID / APP_GID match the host user that owns the bind-mounted data/ and logs/
# (deploy/linux/provision.sh writes them into .env; compose passes them as build args).
ARG APP_UID=10001
ARG APP_GID=10001
RUN if ! getent group "${APP_GID}" >/dev/null; then groupadd --gid "${APP_GID}" cs2bot; fi \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /home/cs2bot --create-home \
        --shell /bin/bash cs2bot \
    && mkdir -p /app/data /app/logs \
    && chown "${APP_UID}:${APP_GID}" /app/data /app/logs

USER cs2bot

ENV CS2BOT_BETCLIC__CHROMIUM_EXECUTABLE=/usr/local/bin/cs2bot-chromium

# Persistent state: data/ = browser profiles (Betclic session + placement journal), models, caches,
# screenshots, demos; logs/ = a copy of the bot's output. Both are bind mounts in docker-compose.cloud.yml.
VOLUME ["/app/data", "/app/logs"]

EXPOSE 8000

# /api/ping is the API's unauthenticated liveness probe (/api/health needs the dashboard password).
# The API starts within seconds of the migrations (the first model training runs in the background);
# the start period covers a slow database start (db upgrade retries) with room to spare.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5m --retries=5 \
    CMD curl -fsS --max-time 8 http://127.0.0.1:8000/api/ping || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/cs2bot-entrypoint"]
CMD ["up"]
