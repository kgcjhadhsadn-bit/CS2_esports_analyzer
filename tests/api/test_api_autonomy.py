"""Structured pause reasons, auto-resume ETA, circuit breaker and Telegram status in the API."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.db import state
from cs2bot.domain.control import PauseCode
from tests.api.api_seed import BASE_URL, CSRF, Seed
from tests.api.conftest import AppFactory


def test_state_lists_structured_reasons_with_eta(
    app_factory: AppFactory, seeded: Seed, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        state.add_pause_reason(
            s,
            PauseCode.LOSS_LIMIT_DAILY,
            "daily loss limit reached",
            details={"period": "2026-01-01T00:00:00+00:00"},
        )
        state.add_pause_reason(s, PauseCode.TWO_FA_REQUIRED, "SMS code")
        state.set_state(s, state.AUTO_LOGIN, {"status": "2fa_required", "at": "2026-01-01T10:00:00+00:00"})
        s.commit()
    app = app_factory(autonomy={"enabled": True, "auto_login": True})
    with TestClient(app, base_url=BASE_URL) as client:
        body = client.get("/api/control/state").json()
        assert body["halted"] is True and body["paused"]["on"] is True
        reasons = {r["code"]: r for r in body["pause_reasons"]}
        assert set(reasons) == {"loss_limit_daily", "2fa_required"}
        assert reasons["loss_limit_daily"]["auto_resume"] is True and reasons["loss_limit_daily"]["eta"]
        assert "midnight" in reasons["loss_limit_daily"]["label"]
        assert body["circuit_open"] is False
        assert body["autonomy"] == {
            "enabled": True,
            "auto_login": True,
            "last_check_at": None,
            "last_login_status": "2fa_required",
            "last_login_at": "2026-01-01T10:00:00Z",
            "last_login_message": None,
            "login_blocked": False,
        }
        assert body["telegram"]["enabled"] is False and body["telegram"]["configured"] is False
        health = client.get("/api/health").json()
        codes = {a["code"] for a in health["alerts"]}
        assert {"trading_paused", "betclic_2fa_required"} <= codes
        assert health["trading"]["pause_reasons"]


def test_circuit_breaker_and_owner_resume(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        state.add_pause_reason(s, PauseCode.LOGIN_REQUIRED, "login")
        state.add_pause_reason(s, PauseCode.CIRCUIT_OPEN, "breaker", details={"code": "login_required"})
        s.commit()
    body = client.get("/api/control/state").json()
    assert body["circuit_open"] is True and body["circuit_codes"] == ["login_required"]
    assert not any(r["auto_resume"] for r in body["pause_reasons"])  # autonomy off by default
    assert "circuit_open" in {a["code"] for a in client.get("/api/health").json()["alerts"]}
    resumed = client.post("/api/control/resume", headers=CSRF).json()
    assert resumed["halted"] is False and resumed["pause_reasons"] == [] and resumed["circuit_open"] is False


def test_manual_pause_is_marked_owner_only(client: TestClient) -> None:
    body = client.post("/api/control/pause", json={"reason": "lunch"}, headers=CSRF).json()
    (reason,) = body["pause_reasons"]
    assert reason["code"] == "manual" and reason["auto_resume"] is False and reason["actor"] == "dashboard"


def test_telegram_status(
    app_factory: AppFactory, seeded: Seed, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        state.set_state(
            s,
            state.TELEGRAM,
            {
                "scanned_at": "2026-01-01T10:00:00+00:00",
                "client": {
                    "sent": 7,
                    "last_ok_at": "2026-01-01T09:59:00+00:00",
                    "last_error": "HTTP 403: blocked",
                },
            },
        )
        s.commit()
    app = app_factory(telegram={"enabled": True, "bot_token": "1:x", "chat_id": "42"})
    with TestClient(app, base_url=BASE_URL) as client:
        tg = client.get("/api/control/state").json()["telegram"]
        assert tg["configured"] is True and tg["sent"] == 7 and tg["last_error"] == "HTTP 403: blocked"
        assert "1:x" not in str(client.get("/api/control/state").json())
        assert "telegram_failing" in {a["code"] for a in client.get("/api/health").json()["alerts"]}
