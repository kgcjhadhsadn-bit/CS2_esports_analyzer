"""Read model of the safety switches stored in ``system_state`` (kill switch, pause, autonomy, Telegram)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from cs2bot.api.formatting import utc
from cs2bot.api.schemas import (
    AutonomyStatusOut,
    PauseReasonOut,
    SwitchState,
    TelegramStatusOut,
    TradingStateOut,
)
from cs2bot.config import Settings
from cs2bot.db import state
from cs2bot.db.models import SystemState
from cs2bot.utils.time import parse_datetime


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


def _when(value: Any) -> datetime | None:
    try:
        return parse_datetime(value) if isinstance(value, str | datetime) else None
    except (ValueError, OverflowError):
        return None


def _telegram(session: Session, settings: Settings) -> TelegramStatusOut:
    from cs2bot.services.telegram import TELEGRAM_POLL_STATE

    tg = settings.telegram
    notify = state.get_state(session, state.TELEGRAM)
    raw_client = notify.get("client")
    client: dict[str, Any] = raw_client if isinstance(raw_client, dict) else {}
    poll = state.get_state(session, TELEGRAM_POLL_STATE)
    sent = client.get("sent") if isinstance(client.get("sent"), int) else None
    handled = poll.get("handled") if isinstance(poll.get("handled"), int) else None
    return TelegramStatusOut(
        enabled=tg.enabled,
        configured=bool(tg.enabled and tg.bot_token is not None and tg.chat_id),
        last_scan_at=_when(notify.get("scanned_at")),
        last_ok_at=_when(client.get("last_ok_at")),
        last_error=str(client["last_error"])[:300] if client.get("last_error") else None,
        sent=sent,
        last_poll_at=_when(poll.get("polled_at")),
        commands_handled=handled,
    )


def trading_state(session: Session, settings: Settings | None = None) -> TradingStateOut:
    """Kill switch + pause flags; with ``settings`` also the structured pause reasons (ETA, circuit
    breaker), the autonomy and the Telegram status."""
    halted, reason = state.is_trading_halted(session)
    out = TradingStateOut(
        halted=halted,
        reason=reason,
        kill_switch=_switch(session, state.KILL_SWITCH),
        paused=_switch(session, state.PAUSED),
    )
    if settings is None:
        return out
    from cs2bot.services.autonomy import pause_overview

    overview = pause_overview(session, settings)
    out.pause_reasons = [
        PauseReasonOut(
            code=r["code"],
            label=r["label"],
            message=r["message"],
            since=_when(r["since"]),
            actor=r["actor"],
            count=r["count"],
            auto_resume=r["auto_resume"],
            eta=_when(r["eta"]),
            last_check=r["last_check"],
        )
        for r in overview["reasons"]
    ]
    out.circuit_open = bool(overview["circuit_open"])
    out.circuit_codes = list(overview["circuit_codes"])
    login = overview["last_login"] or {}
    out.autonomy = AutonomyStatusOut(
        enabled=settings.autonomy.enabled,
        auto_login=bool(overview["auto_login_enabled"]),
        last_check_at=_when(overview["last_check_at"]),
        last_login_status=login.get("status"),
        last_login_at=_when(login.get("at")),
        last_login_message=login.get("message"),
        login_blocked=bool(login.get("blocked")),
    )
    out.telegram = _telegram(session, settings)
    return out
