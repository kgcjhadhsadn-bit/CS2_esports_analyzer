"""Health / liveness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from cs2bot.api.deps import Ctx, require_auth
from cs2bot.api.health import compute_health
from cs2bot.api.schemas import HealthOut

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/ping")
def ping() -> dict[str, str]:
    """Unauthenticated liveness probe (no data) for the supervisor / ``cs2bot doctor``."""
    return {"status": "ok"}


@router.get("/health", response_model=HealthOut, dependencies=[Depends(require_auth)])
def health(ctx: Ctx) -> HealthOut:
    """DB, job runs, data freshness, executor health, trading state and alerts (never 5xx on DB errors)."""
    return compute_health(ctx)
