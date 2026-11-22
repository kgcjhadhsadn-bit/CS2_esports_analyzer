"""API routers (all under ``/api``)."""

from __future__ import annotations

from fastapi import APIRouter

from cs2bot.api.routes import (
    auth,
    bankroll,
    bets,
    control,
    decisions,
    health,
    jobs,
    matches,
    performance,
    review,
    teams,
)

ROUTERS: tuple[APIRouter, ...] = (
    auth.router,
    health.router,
    matches.router,
    bets.router,
    bankroll.router,
    performance.router,
    decisions.router,
    control.router,
    review.router,
    jobs.router,
    teams.router,
)

__all__ = ["ROUTERS"]
