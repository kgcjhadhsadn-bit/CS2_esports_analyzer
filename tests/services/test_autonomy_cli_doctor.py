"""Cloud autonomy in the CLI (`control status`, `telegram test`), doctor checks and decision wiring."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx
from pydantic import SecretStr
from typer.testing import CliRunner

from cs2bot.cli import app
from cs2bot.config import (
    AutonomySettings,
    BetclicSettings,
    ExecutorSettings,
    Settings,
    TelegramSettings,
    reset_settings_cache,
)
from cs2bot.db import state as db_state
from cs2bot.db.base import Base
from cs2bot.db.session import make_engine, make_session_factory, reset_engine_cache
from cs2bot.domain.control import PauseCode
from cs2bot.services import doctor
from cs2bot.services.betting import trusted_markets

runner = CliRunner()
TOKEN = "123:secret"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = f"sqlite:///{tmp_path / 'bot.db'}"
    monkeypatch.setenv("CS2BOT_DATABASE__URL", url)
    monkeypatch.setenv("CS2BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CS2BOT_LOG_LEVEL", "WARNING")
    reset_settings_cache()
    reset_engine_cache()
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    yield url
    reset_settings_cache()
    reset_engine_cache()


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def test_control_status_lists_structured_reasons(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CS2BOT_AUTONOMY__ENABLED", "true")
    reset_settings_cache()
    engine = make_engine(env)
    with make_session_factory(engine)() as s:
        db_state.add_pause_reason(
            s,
            PauseCode.LOSS_LIMIT_DAILY,
            "daily loss limit reached",
            details={"period": "2026-01-01T00:00:00+00:00"},
        )
        db_state.add_pause_reason(s, PauseCode.CIRCUIT_OPEN, "breaker", details={"code": "login_required"})
        s.commit()
    engine.dispose()
    code, out = invoke("control", "status")
    assert code == 0
    assert "loss_limit_daily (lifts automatically, expected" in out
    assert "circuit_open (needs the owner)" in out and "circuit breaker OPEN" in out
    assert "autonomy on" in out and "telegram off" in out
    assert invoke("control", "resume")[0] == 0
    assert "paused: no" in invoke("control", "status")[1]


def test_telegram_test_command(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = invoke("telegram", "test")
    assert code == 1 and "disabled" in out
    monkeypatch.setenv("CS2BOT_TELEGRAM__ENABLED", "true")
    reset_settings_cache()
    code, out = invoke("telegram", "test")
    assert code == 1 and "BOT_TOKEN" in out
    monkeypatch.setenv("CS2BOT_TELEGRAM__BOT_TOKEN", TOKEN)
    monkeypatch.setenv("CS2BOT_TELEGRAM__CHAT_ID", "42")
    reset_settings_cache()
    with respx.mock:
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        code, out = invoke("telegram", "test", "--message", "ping")
    assert code == 0 and "sent to chat 42" in out and route.called
    with respx.mock:
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        )
        code, out = invoke("telegram", "test")
    assert code == 1 and "Unauthorized" in out and TOKEN not in out


# ------------------------------------------------------------------------------------------ doctor


def _settings(**sections: Any) -> Settings:
    return Settings(**sections)


def test_doctor_telegram_check() -> None:
    assert doctor.check_telegram(_settings()).level == "warn"
    missing = doctor.check_telegram(_settings(telegram=TelegramSettings(enabled=True, chat_id="42")))
    assert missing.level == "fail" and "BOT_TOKEN" in missing.message
    bad_chat = TelegramSettings(enabled=True, bot_token=SecretStr(TOKEN), chat_id="me")
    assert doctor.check_telegram(_settings(telegram=bad_chat)).level == "fail"
    good = TelegramSettings(enabled=True, bot_token=SecretStr(TOKEN), chat_id="-100123")
    assert doctor.check_telegram(_settings(telegram=good)).level == "ok"
    probed = doctor.check_telegram(_settings(telegram=good), probe=lambda: {"username": "cs2_bot"})
    assert probed.level == "ok" and "@cs2_bot" in probed.message and TOKEN not in probed.message
    assert doctor.default_telegram_probe(_settings()) is None
    assert doctor.default_telegram_probe(_settings(telegram=good)) is not None


def test_doctor_autonomy_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    assert [c.level for c in doctor.check_autonomy(_settings())] == ["warn"]
    no_login = doctor.check_autonomy(_settings(autonomy=AutonomySettings(enabled=True)))
    assert [c.level for c in no_login] == ["ok", "warn"]
    on = AutonomySettings(enabled=True, auto_login=True)
    assert doctor.check_autonomy(_settings(autonomy=on))[1].level == "fail"  # credentials_source none
    env_missing = BetclicSettings(credentials_source="env", username=SecretStr("u"))
    assert doctor.check_autonomy(_settings(autonomy=on, betclic=env_missing))[1].level == "fail"
    env_ok = BetclicSettings(
        credentials_source="env", username=SecretStr("u"), password=SecretStr("pw-secret")
    )
    checks = doctor.check_autonomy(_settings(autonomy=on, betclic=env_ok))
    assert checks[1].level == "ok" and "pw-secret" not in checks[1].message

    import sys
    import types

    fake = types.ModuleType("keyring")
    fake.get_credential = lambda service, user: types.SimpleNamespace(username="u", password="p")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    keyring = BetclicSettings(credentials_source="keyring")
    assert doctor.check_autonomy(_settings(autonomy=on, betclic=keyring))[1].level == "ok"


def test_doctor_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert doctor.check_display(_settings(executor=ExecutorSettings(headless=True))).level == "ok"
    assert doctor.check_display(_settings()).level == "warn"  # falls back to headless
    strict = ExecutorSettings(headless_without_display=False)
    assert doctor.check_display(_settings(executor=strict)).level == "fail"
    monkeypatch.setenv("DISPLAY", ":99")
    assert doctor.check_display(_settings()).level == "ok"


# ------------------------------------------------------------------------------- decision wiring


def test_trusted_markets_from_a_bundle() -> None:
    class Bundle:
        market_trust: ClassVar[dict[str, Any]] = {
            "match_winner": {"trusted": True},
            "map_winner": {"trusted": False},
            "total_maps": True,
        }

    assert trusted_markets(Bundle()) == {"match_winner", "total_maps"}
    assert trusted_markets(object()) is None and trusted_markets(None) is None
