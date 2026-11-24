"""Cloud autonomy in the CLI (`control status`, `telegram test`), doctor checks and decision wiring."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from typer.testing import CliRunner

import cs2bot.db.models as M
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
from cs2bot.executor import FakeExecutor
from cs2bot.services import doctor
from cs2bot.services.betting import run_betting_cycle, trusted_markets
from tests.services.conftest import World
from tests.services.test_services_e2e import live, prepare_market

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
    # an @channel works for notifications, but only a numeric chat id may send commands
    channel = TelegramSettings(enabled=True, bot_token=SecretStr(TOKEN), chat_id="@my_channel")
    warned = doctor.check_telegram(_settings(telegram=channel))
    assert warned.level == "warn" and "numeric chat id" in warned.message


def test_backtest_writes_the_confidence_guard(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cs2bot.models.confidence import ConfidenceGuard
    from cs2bot.services.backtesting import save_guard_from_report

    settings = Settings()
    settings.models.confidence_guard_file = tmp_path / "models" / "guard.json"
    (tmp_path / "models").mkdir()
    assert save_guard_from_report(settings, SimpleNamespace(predictions=[]))[0] is None
    assert not settings.models.confidence_guard_file.exists()  # nothing resolved: nothing written
    records = [SimpleNamespace(market_type="match_winner", p=0.8, y=i % 5 != 0) for i in range(50)]
    save_guard_from_report(settings, SimpleNamespace(predictions=records))
    guard = ConfidenceGuard.load(settings.models.confidence_guard_file)
    assert "match_winner" in guard.trust and not guard.is_trusted("match_winner")  # far too little history


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


def _guard_trusting(*markets: str) -> Any:
    from cs2bot.models.confidence import ConfidenceGuard, MarketTrust

    guard = ConfidenceGuard.fit([("match_winner", 0.8, 1), ("total_maps", 0.8, 0)])
    for market in ("match_winner", "total_maps", "map_winner"):
        guard.trust[market] = MarketTrust(
            market_type=market,
            trusted=market in markets,
            n=500,
            n_high=100,
            ece=0.01,
            high_mean_predicted=0.82,
            high_observed_rate=0.83,
            high_ci_high=0.9,
        )
    return guard


def test_trusted_markets_from_the_confidence_guard_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Review round 1: deep_data's per-market trust verdict (ConfidenceGuard, written by `cs2bot
    backtest`) is the source of ``decision.require_trusted_calibration``."""
    settings = Settings()
    settings.models.confidence_guard_file = tmp_path / "guard.json"
    assert trusted_markets(None, settings) is None  # no guard yet: unknown
    _guard_trusting("match_winner").save(settings.models.confidence_guard_file)
    assert trusted_markets(None, settings) == {"match_winner"}

    class Bundle:
        market_trust: ClassVar[dict[str, Any]] = {"total_maps": True}

    assert trusted_markets(Bundle(), settings) == {"match_winner"}  # the guard file wins
    settings.models.confidence_guard_file.write_text("{not json", encoding="utf-8")
    assert trusted_markets(None, settings) is None and "confidence guard" in caplog.text


def test_require_trusted_calibration_is_fail_safe_in_the_betting_cycle(world: World, tmp_path: Path) -> None:
    """With the requirement on, a market type is bet only when the guard trusts it - and with no guard
    at all nothing is bet (an unknown verdict is not "trusted")."""
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    world.settings.decision.require_trusted_calibration = True
    world.settings.models.confidence_guard_file = tmp_path / "guard.json"
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert ex.orders == []
    with world.factory() as s:
        rows = list(s.scalars(select(M.BetDecisionRow)))
    assert rows and not any(r.accepted for r in rows)
    assert any(any("untrusted_calibration" in reason for reason in r.reasons) for r in rows)
    # the guard trusts the match winner only: that market type may be bet, the others still not
    _guard_trusting("match_winner").save(world.settings.models.confidence_guard_file)
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=1))
    assert ex.orders, "a trusted, attractive match-winner price must be bet"
    with world.factory() as s:
        accepted = [r.spec_key for r in s.scalars(select(M.BetDecisionRow).where(M.BetDecisionRow.accepted))]
    assert accepted and all(key.startswith("match_winner") for key in accepted), accepted
