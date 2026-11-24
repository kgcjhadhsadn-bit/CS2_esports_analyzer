"""FastAPI application factory for the local dashboard (http://127.0.0.1:8000).

The integration layer (``cs2bot up``) calls :func:`run` (blocking) or :func:`make_server` (run it
in a thread next to the scheduler). Protections, outermost first:

1. Host-header allow-list (DNS rebinding) + security headers   - :class:`LocalSecurityMiddleware`
2. CORS limited to ``settings.api.cors_origins`` (no wildcard)  - Starlette ``CORSMiddleware``
3. ``X-Requested-With: cs2bot`` on POST/PUT/PATCH/DELETE (CSRF) - :class:`CSRFMiddleware`
4. optional session-cookie auth (``settings.api.password``)    - ``require_auth`` dependency
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.cors import CORSMiddleware

from cs2bot import __version__
from cs2bot.api.auth import LoginRateLimiter, SessionAuth
from cs2bot.api.deps import (
    ApiContext,
    EventRemapper,
    ExecutorHealthProvider,
    LoginLauncher,
    LoginLauncherRunner,
    SchedulerStatusProvider,
)
from cs2bot.api.options import ApiOptions
from cs2bot.api.routes import ROUTERS
from cs2bot.api.security import (
    CSRF_HEADER,
    CSRFMiddleware,
    LocalSecurityMiddleware,
    build_allowed_hosts,
    build_allowed_origins,
    safe_cors_origins,
)
from cs2bot.api.static import install_dashboard
from cs2bot.config import Settings, get_settings
from cs2bot.db.session import make_engine, make_session_factory
from cs2bot.utils.logging import get_logger

log = get_logger(__name__)

API_PREFIX = "/api"


async def _db_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """Connection / pool problems: 503 so the dashboard can show "database down" and retry."""
    log.warning("database unavailable on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


async def _db_error(request: Request, exc: Exception) -> JSONResponse:
    # only the exception type is logged: SQLAlchemy messages contain SQL and bound parameters
    log.error("database error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse({"detail": "database error"}, status_code=500)


async def _validation_failed(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422 shape without the ``input`` / ``ctx`` echo (a bad login body would echo the password)."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [
        {
            "loc": [str(part) if not isinstance(part, int) else part for part in err.get("loc", ())],
            "msg": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        for err in errors
    ]
    return JSONResponse({"detail": detail}, status_code=422)


def _pick[T](override: T | None, configured: T) -> T:
    """An :class:`ApiOptions` override when given, else the value from ``settings.api``."""
    return configured if override is None else override


_UVICORN_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})


def _uvicorn_log_level(name: str) -> str:
    """uvicorn only knows its own level names (``settings.log_level`` may be e.g. ``WARN``)."""
    level = name.strip().lower()
    if level in _UVICORN_LOG_LEVELS:
        return level
    return {"warn": "warning", "fatal": "critical", "notset": "debug"}.get(level, "info")


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    *,
    login_launcher: LoginLauncher | None = None,
    executor_health_provider: ExecutorHealthProvider | None = None,
    event_remapper: EventRemapper | None = None,
    scheduler_status_provider: SchedulerStatusProvider | None = None,
    options: ApiOptions | None = None,
) -> FastAPI:
    """Build the dashboard API.

    * ``settings`` - defaults to :func:`cs2bot.config.get_settings`.
    * ``session_factory`` - defaults to a new engine for ``settings.database.url``.
    * ``login_launcher`` - opens the executor's visible Betclic login window (blocking is fine: it
      runs in a background thread), e.g. ``executor.open_login_window``; a ``bool`` result is shown
      as ``last_result``. ``POST /api/control/betclic-login`` answers 501 without it.
    * ``executor_health_provider`` - returns the executor's current health (should be fast, e.g.
      a cached value); falls back to ``system_state["executor_health"]`` when it returns ``None``.
    * ``event_remapper(session, event_row)`` - re-maps an event's markets right after a manual link
      review changed its match / orientation (wire ``cs2bot.odds.collector.remap_event`` with the
      market mapper). Without it the mappings stay cleared until the collector's next pass.
    * ``scheduler_status_provider`` - ``Scheduler.status`` of the built-in scheduler
      (:mod:`cs2bot.flows.scheduler`); ``/api/health`` then shows each job's next due time and
      failure count and stops flagging jobs the scheduler no longer runs.
    * ``options`` - API tunables (page sizes, health thresholds, overrides of the login rate limit
      and extra allowed hosts for tests), see :class:`ApiOptions`.

    The login rate limit (``login_max_failures``, ``login_global_max_failures``, ``login_window_s``)
    and extra Host-header names (``allowed_hosts``) are read from ``settings.api``.
    """
    settings = settings or get_settings()
    options = options or ApiOptions()
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database.url))
    api = settings.api
    auth = SessionAuth(
        username=api.username,
        password=api.password,
        secret=api.session_secret,
        max_age_s=api.session_max_age_s,
    )
    if auth.enabled and auth.ephemeral_secret:
        log.warning("CS2BOT_API__SESSION_SECRET is not set: using a random secret, sessions end on restart")
    if not auth.enabled and not api.is_loopback:  # pragma: no cover - config validation forbids this
        raise ValueError("refusing to serve the API on a non-loopback address without a password")

    ctx = ApiContext(
        settings=settings,
        session_factory=session_factory,
        options=options,
        auth=auth,
        login_limiter=LoginRateLimiter(
            max_failures=_pick(options.login_max_failures, api.login_max_failures),
            global_max_failures=_pick(options.login_global_max_failures, api.login_global_max_failures),
            window_s=_pick(options.login_window_s, api.login_window_s),
        ),
        login_runner=LoginLauncherRunner(login_launcher),
        executor_health_provider=executor_health_provider,
        event_remapper=event_remapper,
        scheduler_status_provider=scheduler_status_provider,
    )

    app = FastAPI(
        title="cs2bot dashboard API",
        version=__version__,
        description="Local-only API of the CS2 analyzer / Betclic bot. See docs/API.md.",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
        # no tracing / metrics / log export from a local betting bot
        telemetry={
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        },
    )
    app.state.cs2bot = ctx

    allowed_hosts = build_allowed_hosts(api, (*api.allowed_hosts, *options.extra_allowed_hosts))
    # add_middleware() wraps: the last one added is the outermost
    app.add_middleware(CSRFMiddleware, allowed_origins=build_allowed_origins(api, allowed_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=safe_cors_origins(api),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[CSRF_HEADER, "Content-Type"],
        allow_private_network=False,
        max_age=600,
    )
    app.add_middleware(LocalSecurityMiddleware, allowed_hosts=allowed_hosts, api_prefix=API_PREFIX)
    if api.forwarded_allow_ips:
        # outermost: behind Caddy (VPS) the real client address and https scheme reach the login rate
        # limit, the audit log and the cookie's Secure flag. Only the configured proxy is trusted; any
        # other peer's X-Forwarded-* headers are ignored. The Host header is never rewritten.
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(api.forwarded_allow_ips))

    for unavailable in (OperationalError, InterfaceError, PoolTimeoutError):
        app.add_exception_handler(unavailable, _db_unavailable)
    app.add_exception_handler(SQLAlchemyError, _db_error)
    app.add_exception_handler(RequestValidationError, _validation_failed)

    for router in ROUTERS:
        app.include_router(router)
    install_dashboard(app, settings)
    return app


def make_server(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    *,
    login_launcher: LoginLauncher | None = None,
    executor_health_provider: ExecutorHealthProvider | None = None,
    event_remapper: EventRemapper | None = None,
    scheduler_status_provider: SchedulerStatusProvider | None = None,
    options: ApiOptions | None = None,
    log_level: str | None = None,
) -> uvicorn.Server:
    """A configured uvicorn server bound to ``settings.api.host:port`` (call ``.run()``).

    ``server.run()`` may be called from a non-main thread (signal handlers are then skipped);
    set ``server.should_exit = True`` to stop it.
    """
    settings = settings or get_settings()
    app = create_app(
        settings,
        session_factory,
        login_launcher=login_launcher,
        executor_health_provider=executor_health_provider,
        event_remapper=event_remapper,
        scheduler_status_provider=scheduler_status_provider,
        options=options,
    )
    config = uvicorn.Config(
        app,
        host=settings.api.host,
        port=settings.api.port,
        log_level=_uvicorn_log_level(log_level or settings.log_level),
        # forwarding headers are handled by create_app (only from settings.api.forwarded_allow_ips);
        # uvicorn's own handling would trust 127.0.0.1 by default
        proxy_headers=False,
        server_header=False,
        access_log=True,
    )
    return uvicorn.Server(config)


def run(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    *,
    login_launcher: LoginLauncher | None = None,
    executor_health_provider: ExecutorHealthProvider | None = None,
    event_remapper: EventRemapper | None = None,
    scheduler_status_provider: SchedulerStatusProvider | None = None,
    options: ApiOptions | None = None,
) -> None:
    """Serve the API + dashboard until interrupted (blocking)."""
    settings = settings or get_settings()
    server = make_server(
        settings,
        session_factory,
        login_launcher=login_launcher,
        executor_health_provider=executor_health_provider,
        event_remapper=event_remapper,
        scheduler_status_provider=scheduler_status_provider,
        options=options,
    )
    log.info("dashboard on http://%s:%d", settings.api.host, settings.api.port)
    server.run()
