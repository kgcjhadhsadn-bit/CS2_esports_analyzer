"""System state stored in the DB: kill switch, pause flag, runtime config overrides.

Every component that can move money MUST check :func:`is_trading_halted` right before acting.
All changes are written to ``audit_log``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from cs2bot.config import BankrollSettings, DecisionSettings, Settings
from cs2bot.db.models import AuditLog, SystemState
from cs2bot.utils.time import utcnow

KILL_SWITCH = "kill_switch"
PAUSED = "paused"
CONFIG_OVERRIDES = "config_overrides"
LAST_RECONCILIATION = "last_reconciliation"
EXECUTOR_HEALTH = "executor_health"


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


def set_paused(session: Session, on: bool, *, actor: str, reason: str = "") -> None:
    set_state(session, PAUSED, {"on": on, "reason": reason}, actor=actor)
    audit(session, actor=actor, action="pause" if on else "resume", details={"reason": reason})


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
