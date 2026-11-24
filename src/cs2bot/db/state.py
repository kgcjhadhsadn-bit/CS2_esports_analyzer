"""System state stored in the DB: kill switch, pause flag, runtime config overrides.

Every component that can move money MUST check :func:`is_trading_halted` right before acting.
All changes are written to ``audit_log``.

Structured pauses (cloud autonomy): ``system_state["paused"]`` is::

    {"on": bool, "reason": "<all messages joined with '; '>",
     "reasons": [{"code": "loss_limit_daily", "message": "...", "since": "<ISO UTC>", "actor": "...",
                  "count": 1, "details": {...}}, ...]}

The bot stays paused while at least one reason is active. :func:`add_pause_reason` adds (or refreshes)
one reason, :func:`resolve_pause_reasons` removes the given codes (the auto-resume service) and
unpauses when none is left, ``set_paused(False)`` (an owner's resume) clears every reason. A pause
stored before reasons existed reads as one ``other`` reason (owner-only). ``reason`` is kept for
readers of the plain text (``get_pause``, the API).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, DecisionSettings, Settings
from cs2bot.db.models import AuditLog, SystemState
from cs2bot.domain.control import PauseCode, PauseReason, parse_pause_code
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import ensure_utc, utcnow

log = get_logger(__name__)

KILL_SWITCH = "kill_switch"
PAUSED = "paused"
CONFIG_OVERRIDES = "config_overrides"
LAST_RECONCILIATION = "last_reconciliation"
EXECUTOR_HEALTH = "executor_health"
AUTO_LOGIN = "auto_login"  # last automatic Betclic login attempt (cs2bot.services.autonomy)
AUTONOMY = "autonomy"  # last auto-resume check (cs2bot.services.autonomy)
TELEGRAM = "telegram"  # Telegram notifier cursor / status (cs2bot.services.telegram)

#: Longest pause text kept per reason and for the joined ``reason`` text.
MAX_PAUSE_REASON_CHARS = 2000


def get_state(session: Session, key: str) -> dict[str, Any]:
    row = session.get(SystemState, key)
    return dict(row.value) if row is not None else {}


def set_state(session: Session, key: str, value: dict[str, Any], *, actor: str = "system") -> None:
    row = session.get(SystemState, key)
    now = utcnow()
    if row is None:
        session.add(SystemState(key=key, value=value, updated_at=now, updated_by=actor))
    else:
        row.value = value
        row.updated_at = now
        row.updated_by = actor
    session.flush()


def audit(session: Session, *, actor: str, action: str, details: dict[str, Any] | None = None) -> None:
    session.add(AuditLog(ts=utcnow(), actor=actor, action=action, details=details or {}))
    session.flush()


def is_kill_switch_on(session: Session) -> bool:
    return bool(get_state(session, KILL_SWITCH).get("on", False))


def set_kill_switch(session: Session, on: bool, *, actor: str, reason: str = "") -> None:
    set_state(session, KILL_SWITCH, {"on": on, "reason": reason}, actor=actor)
    audit(
        session, actor=actor, action="kill_switch_on" if on else "kill_switch_off", details={"reason": reason}
    )


def get_pause(session: Session) -> tuple[bool, str | None]:
    st = get_state(session, PAUSED)
    return bool(st.get("on", False)), st.get("reason")


def _bounded(text: str, limit: int = MAX_PAUSE_REASON_CHARS) -> str:
    """Keep the start (the first reason) and the end (the newest one) of an over-long text."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2 - 7) :]
    return f"{head} [...] {tail}"[:limit]


def _combine(existing: str, new: str) -> str:
    """``existing; new`` (bounded: the first reason and the newest one survive); no repeats."""
    if not existing:
        return new
    if new in existing:
        return existing
    combined = f"{existing}; {new}"
    if len(combined) > MAX_PAUSE_REASON_CHARS:
        combined = f"{existing[: MAX_PAUSE_REASON_CHARS // 2]} [...]; {new}"[:MAX_PAUSE_REASON_CHARS]
    return combined


def _pause_row(session: Session, *, lock: bool) -> SystemState | None:
    """The ``paused`` row; ``lock=True`` reads it ``SELECT ... FOR UPDATE`` (fresh from the database).

    Every read-modify-write of the pause reasons locks the row first: the auto-resume service (fast lane),
    the Telegram commands (telegram lane), the dashboard (API thread) and the betting / settlement code
    change it from different threads and transactions. Without the lock, PostgreSQL's READ COMMITTED lets
    two writers both read ``[a]`` and write back their own version - e.g. an auto-resume removing ``a``
    could silently drop a ``manual`` pause the owner added a millisecond earlier and unpause the bot.
    SQLite (tests, development) ignores ``FOR UPDATE``; production runs PostgreSQL. Writers that also
    change another ``system_state`` row (``set_paused`` -> ``auto_login``) lock ``paused`` first, so the
    lock order is the same everywhere (no deadlock).
    """
    if lock:
        return session.get(SystemState, PAUSED, with_for_update=True, populate_existing=True)
    return session.get(SystemState, PAUSED)


def get_pause_reasons(session: Session, *, lock: bool = False) -> list[PauseReason]:
    """The active pause reasons, oldest first (empty when the bot is not paused).

    ``lock=True`` locks the row for the rest of the transaction (used by every writer below).
    """
    row = _pause_row(session, lock=lock)
    if row is None:
        return []
    st = dict(row.value or {})
    if not st.get("on", False):
        return []
    raw = st.get("reasons")
    if not isinstance(raw, list) or not raw:
        # stored before reasons existed (or by hand): one owner-only reason
        return [
            PauseReason(
                code=PauseCode.OTHER,
                message=str(st.get("reason") or "paused"),
                since=ensure_utc(row.updated_at) if row.updated_at else utcnow(),
                actor=row.updated_by or "system",
            )
        ]
    out: list[PauseReason] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            since_raw = item.get("since")
            since = ensure_utc(datetime.fromisoformat(since_raw)) if isinstance(since_raw, str) else None
        except ValueError:
            since = None
        details = item.get("details")
        try:
            count = max(1, int(item.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        out.append(
            PauseReason(
                code=parse_pause_code(item.get("code")),
                message=str(item.get("message") or ""),
                since=since or (ensure_utc(row.updated_at) if row.updated_at else utcnow()),
                actor=str(item.get("actor") or "system"),
                count=count,
                details=dict(details) if isinstance(details, dict) else {},
            )
        )
    if not out:  # "on" with an unreadable list: never silently unpause
        out.append(
            PauseReason(code=PauseCode.OTHER, message=str(st.get("reason") or "paused"), since=utcnow())
        )
    return out


def _write_reasons(session: Session, reasons: list[PauseReason], *, actor: str) -> None:
    on = bool(reasons)
    text = _bounded("; ".join(r.message for r in reasons if r.message)) if on else ""
    set_state(
        session,
        PAUSED,
        {"on": on, "reason": text, "reasons": [r.model_dump(mode="json") for r in reasons]},
        actor=actor,
    )


def add_pause_reason(
    session: Session,
    code: PauseCode | str,
    message: str,
    *,
    actor: str = "system",
    details: dict[str, Any] | None = None,
    at: datetime | None = None,
    replace: bool = False,
) -> bool:
    """Pause trading for ``code``; returns True when this call paused a running bot.

    An active reason with the same code is refreshed instead of duplicated: its message gets the new
    text appended (bounded, a text already contained is not repeated), ``count`` goes up, ``details``
    are updated and ``since`` is kept. ``replace=True`` (a person pausing again) replaces the message
    instead. Every call is audited (``pause``) unless the message was already there. Flushes, never
    commits.
    """
    pcode = parse_pause_code(code)
    when = ensure_utc(at) if at is not None else utcnow()
    msg = _bounded(message.strip() or pcode.value)
    reasons = get_pause_reasons(session, lock=True)
    was_paused = bool(reasons)
    current = next((r for r in reasons if r.code is pcode), None)
    if current is not None:
        combined = msg if replace else _combine(current.message, msg)
        changed = replace or combined != current.message
        current.message = combined
        current.count += 1
        if details:
            current.details = {**current.details, **details}
    else:
        reasons.append(PauseReason(code=pcode, message=msg, since=when, actor=actor, details=details or {}))
        changed = True
    _write_reasons(session, reasons, actor=actor)
    if changed:
        audit(
            session,
            actor=actor,
            action="pause",
            details={"reason": msg, "code": pcode.value, **({"details": details} if details else {})},
        )
    return not was_paused


def resolve_pause_reasons(
    session: Session, codes: Iterable[PauseCode | str], *, actor: str, note: str = ""
) -> list[PauseReason]:
    """Remove the active reasons with these codes; unpause (audited ``resume``) when none is left.

    Returns the removed reasons. Used by the auto-resume service; an owner's resume is
    ``set_paused(False)``. Flushes, never commits.
    """
    wanted = {parse_pause_code(c) for c in codes}
    reasons = get_pause_reasons(session, lock=True)
    removed = [r for r in reasons if r.code in wanted]
    if not removed:
        return []
    remaining = [r for r in reasons if r.code not in wanted]
    _write_reasons(session, remaining, actor=actor)
    if not remaining:
        audit(
            session,
            actor=actor,
            action="resume",
            details={"reason": note, "auto": True, "codes": [r.code.value for r in removed]},
        )
    return removed


def set_paused(
    session: Session, on: bool, *, actor: str, reason: str = "", code: PauseCode | str = PauseCode.MANUAL
) -> None:
    """``on=True``: add a pause reason (default ``manual``, kept with any other active reason).
    ``on=False``: the owner's resume - every reason is cleared (circuit breaker included) and a stopped
    auto-login (wrong password / locked account) may try again."""
    if on:
        add_pause_reason(session, code, reason, actor=actor, replace=True)
        return
    previous = get_pause_reasons(session, lock=True)
    _write_reasons(session, [], actor=actor)
    login = get_state(session, AUTO_LOGIN)
    if login.get("blocked") or login.get("submit_failures"):
        # the owner has looked at it: automatic logins may try again (with a fresh failure budget)
        set_state(
            session,
            AUTO_LOGIN,
            {**login, "blocked": False, "failures": 0, "submit_failures": 0},
            actor=actor,
        )
    audit(
        session,
        actor=actor,
        action="resume",
        details={"reason": reason, "cleared": [r.code.value for r in previous]},
    )


def is_trading_halted(session: Session) -> tuple[bool, str | None]:
    """True if the kill switch is on or the bot is paused (e.g. a loss limit was hit)."""
    if is_kill_switch_on(session):
        return True, "kill switch is on"
    paused, reason = get_pause(session)
    if paused:
        return True, f"paused: {reason or 'no reason given'}"
    return False, None


def get_config_overrides(session: Session) -> dict[str, Any]:
    """Overrides edited from the dashboard: {"decision": {...}, "bankroll": {...}}."""
    return get_state(session, CONFIG_OVERRIDES)


def set_config_overrides(session: Session, overrides: dict[str, Any], *, actor: str) -> None:
    allowed = {"decision", "bankroll"}
    unknown = set(overrides) - allowed
    if unknown:
        raise ValueError(f"unsupported override sections: {sorted(unknown)}")
    # validate by constructing the merged models (raises on bad values)
    DecisionSettings.model_validate(overrides.get("decision", {}))
    BankrollSettings.model_validate(overrides.get("bankroll", {}))
    set_state(session, CONFIG_OVERRIDES, overrides, actor=actor)
    audit(session, actor=actor, action="config_overrides", details=overrides)


def effective_decision_settings(session: Session, settings: Settings) -> DecisionSettings:
    ov = get_config_overrides(session).get("decision", {})
    return DecisionSettings.model_validate({**settings.decision.model_dump(), **ov})


def effective_bankroll_settings(session: Session, settings: Settings) -> BankrollSettings:
    ov = get_config_overrides(session).get("bankroll", {})
    return BankrollSettings.model_validate({**settings.bankroll.model_dump(), **ov})
