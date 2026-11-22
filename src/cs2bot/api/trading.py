"""Read model of the safety switches stored in ``system_state`` (kill switch, pause)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cs2bot.api.formatting import utc
from cs2bot.api.schemas import SwitchState, TradingStateOut
from cs2bot.db import state
from cs2bot.db.models import SystemState


def _switch(session: Session, key: str) -> SwitchState:
    row = session.get(SystemState, key)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    reason = value.get("reason")
    return SwitchState(
        on=bool(value.get("on", False)),
        reason=str(reason) if reason not in (None, "") else None,
        updated_at=utc(row.updated_at) if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
    )


def trading_state(session: Session) -> TradingStateOut:
    halted, reason = state.is_trading_halted(session)
    return TradingStateOut(
        halted=halted,
        reason=reason,
        kill_switch=_switch(session, state.KILL_SWITCH),
        paused=_switch(session, state.PAUSED),
    )
