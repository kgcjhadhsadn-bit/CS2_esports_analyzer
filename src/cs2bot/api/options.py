"""Tunables of the dashboard API that are not part of :class:`cs2bot.config.APISettings`.

They have safe defaults; the integration layer or tests can pass ``create_app(..., options=ApiOptions(...))``.
The login rate limit and the extra Host names live in ``settings.api`` (``login_max_failures``,
``login_global_max_failures``, ``login_window_s``, ``allowed_hosts``); the fields of the same name
here are optional overrides (``None`` / empty = use the settings).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiOptions:
    # --- login rate limiting (failed attempts inside a sliding window) ----------------------------
    login_max_failures: int | None = None
    """Failed logins allowed per client address inside the window before HTTP 429
    (``None`` = ``settings.api.login_max_failures``)."""
    login_global_max_failures: int | None = None
    """Failed logins allowed for all clients together inside the window (slows distributed guessing;
    ``None`` = ``settings.api.login_global_max_failures``)."""
    login_window_s: float | None = None
    """Length of the sliding window in seconds (``None`` = ``settings.api.login_window_s``)."""

    # --- session cookie ---------------------------------------------------------------------------
    cookie_name: str = "cs2bot_session"

    # --- Host header allow-list -------------------------------------------------------------------
    extra_allowed_hosts: tuple[str, ...] = ()
    """Additional ``host`` or ``host:port`` values accepted in the Host header, on top of
    ``settings.api.allowed_hosts`` (e.g. a LAN name when the API is bound to ``0.0.0.0``). Loopback
    names and the configured host are always allowed."""

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
    scheduler_overdue_grace_s: float = 900.0
    """A scheduler job whose ``next_due`` lies further in the past than this (and at least two of
    its own intervals, when known) is reported as overdue - the scheduler thread is probably stuck."""
