"""Session login for the dashboard (only active when ``settings.api.password`` is set)."""

from __future__ import annotations

import math
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from cs2bot.api.deps import DB, AuthState, Ctx, current_auth
from cs2bot.api.schemas import AuthMe, LoginIn
from cs2bot.db import state
from cs2bot.utils.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

_MAX_PASSWORD_LEN = 1024


def _client(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _audit(db: DB, actor: str, action: str, client: str) -> None:
    """Best-effort audit entry: a DB hiccup must not block logging in or out."""
    try:
        state.audit(db, actor=actor, action=action, details={"client": client})
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.warning("could not write the %s audit entry", action)


@router.get("/me", response_model=AuthMe)
def me(auth: Annotated[AuthState, Depends(current_auth)]) -> AuthMe:
    """Whether auth is enabled and whether this browser has a valid session."""
    if not auth.enabled:
        return AuthMe(auth_enabled=False, authenticated=True, username=None)
    return AuthMe(auth_enabled=True, authenticated=auth.username is not None, username=auth.username)


@router.post("/login", response_model=AuthMe)
def login(body: LoginIn, request: Request, response: Response, ctx: Ctx, db: DB) -> AuthMe:
    if not ctx.auth.enabled:
        return AuthMe(auth_enabled=False, authenticated=True, username=None)
    client = _client(request)
    wait = ctx.login_limiter.retry_after(client)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="too many failed login attempts - try again later",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    password = body.password.get_secret_value()
    if len(password) > _MAX_PASSWORD_LEN or not ctx.auth.check_credentials(body.username, password):
        ctx.login_limiter.record_failure(client)
        log.warning("failed dashboard login from %s", client)
        raise HTTPException(status_code=401, detail="invalid username or password")
    ctx.login_limiter.reset(client)
    response.set_cookie(
        ctx.options.cookie_name,
        ctx.auth.issue(),
        max_age=ctx.auth.max_age_s,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    _audit(db, AuthState(enabled=True, username=ctx.auth.username).actor, "auth_login", client)
    return AuthMe(auth_enabled=True, authenticated=True, username=ctx.auth.username)


@router.post("/logout", response_model=AuthMe)
def logout(
    request: Request,
    response: Response,
    ctx: Ctx,
    db: DB,
    auth: Annotated[AuthState, Depends(current_auth)],
) -> AuthMe:
    response.delete_cookie(ctx.options.cookie_name, path="/", httponly=True, samesite="strict")
    if not ctx.auth.enabled:
        return AuthMe(auth_enabled=False, authenticated=True, username=None)
    if auth.sid is not None:
        ctx.auth.revoke(auth.sid)
        _audit(db, auth.actor, "auth_logout", _client(request))
    return AuthMe(auth_enabled=True, authenticated=False, username=None)
