"""Application context and FastAPI dependencies (DB session, auth, audit actor, pagination)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.api.auth import LoginRateLimiter, SessionAuth
from cs2bot.api.options import ApiOptions
from cs2bot.config import Settings
from cs2bot.db.models import BookmakerEvent
from cs2bot.domain.betting import ExecutorHealth
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import ensure_utc, utcnow

log = get_logger(__name__)

ExecutorHealthProvider = Callable[[], ExecutorHealth | None]
# Opens the visible Betclic login window; may block. A bool result (logged in or not) is reported.
LoginLauncher = Callable[[], object]
# Re-maps an event's stored markets after a manual link change (``cs2bot.odds.collector.remap_event``).
EventRemapper = Callable[[Session, BookmakerEvent], object]
# ``cs2bot.flows.scheduler.Scheduler.status``: one dict per job (name, lane, enabled, last_run,
# next_due, failures). Must be fast (it only snapshots in-memory state).
SchedulerStatusProvider = Callable[[], Sequence[Mapping[str, object]]]

# ``audit_log.actor`` / ``system_state.updated_by`` are VARCHAR(64) (PostgreSQL rejects longer values).
_ACTOR_MAX_LEN = 64


class LoginLauncherRunner:
    """Runs the (blocking) Betclic login launcher in a background thread, one at a time."""

    def __init__(self, launcher: LoginLauncher | None) -> None:
        self.launcher = launcher
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_error: str | None = None
        self.last_result: bool | None = None

    @property
    def configured(self) -> bool:
        return self.launcher is not None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> bool:
        """Start the launcher; ``False`` if it is already running (or not configured)."""
        launcher = self.launcher
        if launcher is None:
            return False
        with self._lock:
            if self.running:
                return False
            self.last_started_at = utcnow()
            self.last_error = None
            self.last_result = None
            self._thread = threading.Thread(
                target=self._run, args=(launcher,), name="cs2bot-betclic-login", daemon=True
            )
            self._thread.start()
        return True

    def _run(self, launcher: LoginLauncher) -> None:
        try:
            result = launcher()
            self.last_result = result if isinstance(result, bool) else None
        except Exception as exc:  # the launcher belongs to another package; never crash the API
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("Betclic login launcher failed: %s", type(exc).__name__)
        finally:
            self.last_finished_at = utcnow()

    def join(self, timeout: float | None = None) -> None:
        """Wait for a running launcher (tests / shutdown)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


@dataclass
class ApiContext:
    """Everything the routes need, stored on ``app.state.cs2bot``."""

    settings: Settings
    session_factory: sessionmaker[Session]
    options: ApiOptions
    auth: SessionAuth
    login_limiter: LoginRateLimiter
    login_runner: LoginLauncherRunner
    executor_health_provider: ExecutorHealthProvider | None = None
    event_remapper: EventRemapper | None = None
    scheduler_status_provider: SchedulerStatusProvider | None = None
    clock: Callable[[], datetime] = field(default=utcnow)

    def now(self) -> datetime:
        return ensure_utc(self.clock())


@dataclass(frozen=True)
class AuthState:
    enabled: bool
    username: str | None = None
    sid: str | None = None

    @property
    def actor(self) -> str:
        """Audit-log actor for changes made through the dashboard (at most 64 characters)."""
        actor = f"dashboard:{self.username}" if self.username else "dashboard"
        return actor[:_ACTOR_MAX_LEN]


def get_ctx(request: Request) -> ApiContext:
    ctx = getattr(request.app.state, "cs2bot", None)
    if not isinstance(ctx, ApiContext):  # pragma: no cover - only if the app was built by hand
        raise RuntimeError("cs2bot API context missing: build the app with create_app()")
    return ctx


Ctx = Annotated[ApiContext, Depends(get_ctx)]


def get_db(ctx: Ctx) -> Iterator[Session]:
    """One session per request; routes that change data commit explicitly."""
    session = ctx.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DB = Annotated[Session, Depends(get_db)]


def current_auth(request: Request, ctx: Ctx) -> AuthState:
    """Auth state without enforcing it (used by /api/auth/*)."""
    if not ctx.auth.enabled:
        return AuthState(enabled=False)
    info = ctx.auth.verify(request.cookies.get(ctx.options.cookie_name))
    if info is None:
        return AuthState(enabled=True)
    return AuthState(enabled=True, username=info.username, sid=info.sid)


def require_auth(auth: Annotated[AuthState, Depends(current_auth)]) -> AuthState:
    """Reject the request with 401 when auth is enabled and there is no valid session."""
    if auth.enabled and auth.username is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return auth


Auth = Annotated[AuthState, Depends(require_auth)]


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int


def page_params(
    ctx: Ctx,
    limit: Annotated[int | None, Query(ge=1, description="Page size")] = None,
    offset: Annotated[int, Query(ge=0, le=2**31 - 1, description="Rows to skip")] = 0,
) -> PageParams:
    size = ctx.options.default_page_size if limit is None else min(limit, ctx.options.max_page_size)
    return PageParams(limit=size, offset=offset)


Paging = Annotated[PageParams, Depends(page_params)]
