"""Tunables of the dashboard API that are not (yet) part of :class:`cs2bot.config.APISettings`.

They have safe defaults; the integration layer or tests can pass ``create_app(..., options=ApiOptions(...))``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiOptions:
    # --- login rate limiting (failed attempts inside a sliding window) ----------------------------
    login_max_failures: int = 5
    """Failed logins allowed per client address inside ``login_window_s`` before HTTP 429."""
    login_global_max_failures: int = 20
    """Failed logins allowed for all clients together inside the window (slows distributed guessing)."""
    login_window_s: float = 300.0

    # --- session cookie ---------------------------------------------------------------------------
    cookie_name: str = "cs2bot_session"

    # --- Host header allow-list -------------------------------------------------------------------
    extra_allowed_hosts: tuple[str, ...] = ()
    """Additional ``host`` or ``host:port`` values accepted in the Host header (e.g. a LAN name when
    the API is bound to ``0.0.0.0``). Loopback names and the configured host are always allowed."""

    # --- pagination -------------------------------------------------------------------------------
    default_page_size: int = 50
    max_page_size: int = 500

    # --- link review queue ------------------------------------------------------------------------
    review_window_hours: float = 24.0
    """Candidate matches must start within +- this many hours of the bookmaker event."""
    review_max_candidates: int = 5

    # --- health -----------------------------------------------------------------------------------
    stuck_job_after_s: float = 2 * 3600.0
    """A job still RUNNING after this long is reported as stuck."""
    executor_health_stale_s: float = 3600.0
    """Executor health older than this is reported as stale."""
    failing_job_threshold: int = 3
    """Consecutive failed runs after which a job raises a "failing" alert."""
