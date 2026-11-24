"""Safety controls: kill switch, pause / resume, runtime config overrides, Betclic login window.

Every change goes through :mod:`cs2bot.db.state`, which writes an ``audit_log`` row. The CSRF
middleware requires ``X-Requested-With: cs2bot`` on all of these mutations.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from cs2bot.api.configedit import ConfigValidationError, config_view, normalize_overrides
from cs2bot.api.deps import DB, Auth, Ctx, require_auth
from cs2bot.api.schemas import (
    ConfigIn,
    ConfigOut,
    KillSwitchIn,
    LoginLaunchOut,
    PauseIn,
    ResumeIn,
    SwitchState,
    TradingStateOut,
)
from cs2bot.api.trading import trading_state
from cs2bot.db import state
from cs2bot.utils.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/control", tags=["control"], dependencies=[Depends(require_auth)])


@router.get("/state", response_model=TradingStateOut)
def get_trading_state(ctx: Ctx, db: DB) -> TradingStateOut:
    """Kill switch + pause flags, structured pause reasons (auto-resume ETA, circuit breaker), the
    autonomy and the Telegram status, and whether trading is halted."""
    return trading_state(db, ctx.settings)


@router.get("/kill-switch", response_model=SwitchState)
def get_kill_switch(db: DB) -> SwitchState:
    return trading_state(db).kill_switch


@router.post("/kill-switch", response_model=SwitchState)
def set_kill_switch(body: KillSwitchIn, db: DB, auth: Auth) -> SwitchState:
    state.set_kill_switch(db, body.on, actor=auth.actor, reason=body.reason.strip())
    db.commit()
    log.warning("kill switch turned %s from the dashboard (%s)", "ON" if body.on else "OFF", auth.actor)
    return trading_state(db).kill_switch


@router.post("/pause", response_model=TradingStateOut)
def pause(ctx: Ctx, db: DB, auth: Auth, body: Annotated[PauseIn | None, Body()] = None) -> TradingStateOut:
    """Manual pause (reason code ``manual``: never lifted automatically)."""
    reason = (body or PauseIn()).reason.strip() or PauseIn().reason
    state.set_paused(db, True, actor=auth.actor, reason=reason)
    db.commit()
    log.warning("trading paused from the dashboard (%s)", auth.actor)
    return trading_state(db, ctx.settings)


@router.post("/resume", response_model=TradingStateOut)
def resume(ctx: Ctx, db: DB, auth: Auth, body: Annotated[ResumeIn | None, Body()] = None) -> TradingStateOut:
    """Clear every pause reason incl. an open circuit breaker (does not turn off the kill switch)."""
    state.set_paused(db, False, actor=auth.actor, reason=(body or ResumeIn()).reason.strip())
    db.commit()
    log.warning("trading resumed from the dashboard (%s)", auth.actor)
    return trading_state(db, ctx.settings)


@router.get("/config", response_model=ConfigOut)
def get_config(ctx: Ctx, db: DB) -> ConfigOut:
    """Effective decision/bankroll settings, the defaults (``.env``) and the stored overrides."""
    return config_view(db, ctx.settings)


@router.put("/config", response_model=ConfigOut)
def put_config(body: ConfigIn, ctx: Ctx, db: DB, auth: Auth) -> ConfigOut:
    """Replace overrides per section: omitted section = keep, ``{}`` = clear, fields = replace."""
    try:
        overrides = normalize_overrides(body, ctx.settings, state.get_config_overrides(db))
        state.set_config_overrides(db, overrides, actor=auth.actor)
    except ConfigValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.errors) from None
    except (ValidationError, ValueError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=422, detail=[{"loc": ["body"], "msg": str(exc)[:300], "type": "value_error"}]
        ) from None
    db.commit()
    return config_view(db, ctx.settings)


def _launch_status(ctx: Ctx, *, started: bool = False) -> LoginLaunchOut:
    runner = ctx.login_runner
    return LoginLaunchOut(
        configured=runner.configured,
        running=runner.running,
        started=started,
        last_started_at=runner.last_started_at,
        last_finished_at=runner.last_finished_at,
        last_result=runner.last_result,
        last_error=runner.last_error,
    )


@router.get("/betclic-login", response_model=LoginLaunchOut)
def betclic_login_status(ctx: Ctx) -> LoginLaunchOut:
    return _launch_status(ctx)


@router.post("/betclic-login", response_model=LoginLaunchOut, status_code=202)
def betclic_login(ctx: Ctx, db: DB, auth: Auth) -> LoginLaunchOut:
    """Open the executor's Betclic login window (visible browser) in a background thread."""
    runner = ctx.login_runner
    if not runner.configured:
        raise HTTPException(status_code=501, detail="Betclic login launcher is not configured")
    if not runner.start():
        raise HTTPException(status_code=409, detail="the Betclic login window is already open")
    try:
        state.audit(db, actor=auth.actor, action="betclic_login_requested", details={})
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.warning("could not write the betclic_login_requested audit entry")
    return _launch_status(ctx, started=True)
