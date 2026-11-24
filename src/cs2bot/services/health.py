"""Executor health: run ``health_check()``, persist it for the dashboard, keep a fast in-memory copy.

The dashboard's ``/api/health`` calls ``executor_health_provider`` on every request, so the provider
must never touch the browser: :class:`HealthCache` returns the last stored result. The result is also
written to ``system_state["executor_health"]`` (``ExecutorHealth.model_dump(mode="json")``), which is
what the API reads when no provider is wired (``cs2bot executor health`` from the CLI).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.db import state as db_state
from cs2bot.domain.betting import ExecutorHealth
from cs2bot.utils.time import utcnow

log = logging.getLogger(__name__)


class HealthCache:
    """Thread-safe holder of the latest :class:`ExecutorHealth` (``get`` is the API provider)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._health: ExecutorHealth | None = None

    def get(self) -> ExecutorHealth | None:
        with self._lock:
            return self._health

    def set(self, health: ExecutorHealth) -> None:
        with self._lock:
            self._health = health


def store_health(session: Session, health: ExecutorHealth) -> None:
    db_state.set_state(session, db_state.EXECUTOR_HEALTH, health.model_dump(mode="json"), actor="executor")


def refresh_executor_health(
    session_factory: sessionmaker[Session], executor: Any, cache: HealthCache | None = None
) -> ExecutorHealth:
    """Run the executor's health check (never raises), store it in the DB and in ``cache``."""
    try:
        health = executor.health_check()
        if not isinstance(health, ExecutorHealth):
            raise TypeError(f"health_check returned {type(health).__name__}")
    except Exception as exc:
        log.warning("executor health check failed: %s", exc)
        health = ExecutorHealth(
            ok=False, message=f"health check failed: {type(exc).__name__}: {exc}"[:500], checked_at=utcnow()
        )
    if cache is not None:
        cache.set(health)
    try:
        with session_factory() as session:
            store_health(session, health)
            session.commit()
    except Exception:
        log.exception("could not store the executor health")
    return health
