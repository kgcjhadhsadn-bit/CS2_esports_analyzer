"""Bankroll page data."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from cs2bot.api.bankroll import compute_bankroll
from cs2bot.api.deps import DB, Ctx, require_auth
from cs2bot.api.schemas import BankrollOut

router = APIRouter(prefix="/api/bankroll", tags=["bankroll"], dependencies=[Depends(require_auth)])


@router.get("", response_model=BankrollOut)
def get_bankroll(
    ctx: Ctx,
    db: DB,
    dry_run: Annotated[
        bool, Query(description="false: real-money bets and the ledger; true: paper (dry-run) bets")
    ] = False,
    max_points: Annotated[int, Query(ge=10, le=20000, description="Max points per curve")] = 2000,
) -> BankrollOut:
    return compute_bankroll(db, ctx.settings, dry_run=dry_run, now=ctx.now(), max_points=max_points)
