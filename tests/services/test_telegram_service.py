"""Telegram client (respx), notifier, daily report, owner commands and the scheduler / run_up wiring."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings, TelegramSettings
from cs2bot.db import state as db_state
from cs2bot.db.models import BetDecisionRow, JobRun, Match, Team
from cs2bot.domain.control import PauseCode
from cs2bot.domain.enums import BetStatus, JobStatus
from cs2bot.engine import ledger
from cs2bot.flows.jobs import NOTIFY, TELEGRAM, Runtime, build_jobs
from cs2bot.services.registry import ModelRegistry
from cs2bot.services.telegram import (
    HELP_TEXT,
    TelegramClient,
    TelegramCommands,
    TelegramError,
    TelegramNotifier,
    daily_report_text,
    format_audit,
    redact,
)
from cs2bot.utils.time import utcnow
from tests.engine.engine_factories import add_bet

TOKEN = "123456:ABC-secret-token"
CHAT = "424242"
API = "https://api.telegram.org"
SEND_URL = f"{API}/bot{TOKEN}/sendMessage"
UPDATES_URL = f"{API}/bot{TOKEN}/getUpdates"


def tg_settings(**kw: Any) -> TelegramSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "bot_token": SecretStr(TOKEN),
        "chat_id": CHAT,
        "min_send_interval_s": 0.0,
        "max_retries": 2,
    }
    return TelegramSettings(**{**base, **kw})


def full_settings(**kw: Any) -> Settings:
    s = Settings()
    s.telegram = tg_settings(**kw)
    return s


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def ok(result: Any = True) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def sent_texts(route: respx.Route) -> list[str]:
    return [json.loads(call.request.content)["text"] for call in route.calls]


# ================================================================================================ client


@respx.mock
def test_send_posts_plain_text_to_the_chat() -> None:
    route = respx.post(SEND_URL).mock(return_value=ok({"message_id": 1}))
    client = TelegramClient(tg_settings())
    assert client.send("hello") is True
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == CHAT and body["text"] == "hello" and "parse_mode" not in body
    assert client.sent == 1 and client.last_ok_at is not None and client.status()["configured"]


@respx.mock
def test_send_is_rate_limited() -> None:
    respx.post(SEND_URL).mock(return_value=ok())
    sleeps = Sleeps()
    clock = iter([0.0, 0.2, 1.3, 5.0, 5.0])  # after #1, check #2, after #2, check #3, after #3
    client = TelegramClient(tg_settings(min_send_interval_s=1.1), sleep=sleeps, monotonic=lambda: next(clock))
    assert client.send("a") and client.send("b") and client.send("c")
    assert sleeps.calls == [pytest.approx(0.9)]  # only the second message came too early


@respx.mock
def test_send_retries_429_and_5xx_then_succeeds() -> None:
    route = respx.post(SEND_URL).mock(
        side_effect=[
            httpx.Response(
                429, json={"ok": False, "description": "Too Many", "parameters": {"retry_after": 7}}
            ),
            httpx.Response(502, text="bad gateway"),
            ok(),
        ]
    )
    sleeps = Sleeps()
    client = TelegramClient(tg_settings(max_retries=2), sleep=sleeps)
    assert client.send("x") is True and route.call_count == 3
    assert sleeps.calls == [7.0, 2.0]


@respx.mock
def test_send_never_raises_and_redacts_the_token(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError(f"cannot reach {SEND_URL}"))
    client = TelegramClient(tg_settings(max_retries=1), sleep=Sleeps())
    with caplog.at_level(logging.DEBUG):
        assert client.send("x") is False
    assert client.failed == 1 and client.last_error is not None
    assert TOKEN not in client.last_error and "<token>" in client.last_error
    assert TOKEN not in caplog.text


@respx.mock
def test_client_errors_are_not_retried() -> None:
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(403, json={"ok": False, "description": "bot was blocked by the user"})
    )
    sleeps = Sleeps()
    client = TelegramClient(tg_settings(), sleep=sleeps)
    assert client.send("x") is False and route.call_count == 1 and sleeps.calls == []
    assert "blocked" in (client.last_error or "")


def test_unconfigured_client_sends_nothing() -> None:
    assert TelegramClient(TelegramSettings(enabled=True)).send("x") is False
    assert TelegramClient.from_settings(Settings()) is None  # disabled by default
    assert TelegramClient.from_settings(full_settings()) is not None
    with pytest.raises(TelegramError, match="no bot token"):
        TelegramClient(TelegramSettings()).call("getMe", {})


@respx.mock
def test_long_messages_are_truncated() -> None:
    route = respx.post(SEND_URL).mock(return_value=ok())
    TelegramClient(tg_settings()).send("x" * 10_000)
    assert len(sent_texts(route)[0]) <= 4096


@respx.mock
def test_get_updates_and_get_me() -> None:
    route = respx.post(UPDATES_URL).mock(return_value=ok([{"update_id": 5}, "junk"]))
    respx.post(f"{API}/bot{TOKEN}/getMe").mock(return_value=ok({"username": "cs2_bot"}))
    client = TelegramClient(tg_settings(poll_timeout_s=3))
    assert client.get_updates(17) == [{"update_id": 5}]
    body = json.loads(route.calls[0].request.content)
    assert body == {"timeout": 3, "allowed_updates": ["message"], "offset": 17}
    assert client.get_me()["username"] == "cs2_bot"


def test_redact() -> None:
    assert redact(f"https://x/bot{TOKEN}/m", TOKEN) == "https://x/bot<token>/m"
    assert redact("nothing", None) == "nothing"


# ============================================================================================= notifier


def _match(session: Session) -> Match:
    t1, t2 = Team(name="Natus Vincere"), Team(name="Vitality")
    session.add_all([t1, t2])
    session.flush()
    match = Match(team1_id=t1.id, team2_id=t2.id, scheduled_at=utcnow() + timedelta(hours=3))
    session.add(match)
    session.flush()
    return match


def _decision(session: Session, match: Match) -> BetDecisionRow:
    row = BetDecisionRow(
        match_id=match.id,
        spec_key="match_winner",
        selection_key="team1",
        odds=Decimal("1.85"),
        decided_at=utcnow(),
        accepted=True,
        reasons=[],
        p_win=0.812,
        sigma=0.02,
        ev=0.034,
    )
    session.add(row)
    session.flush()
    return row


@respx.mock
def test_notifier_starts_from_now_then_announces_new_events(session_factory: sessionmaker[Session]) -> None:
    route = respx.post(SEND_URL).mock(return_value=ok())
    settings = full_settings()
    with session_factory() as s:
        db_state.set_paused(s, True, actor="owner", reason="old pause - history")
        s.commit()
    notifier = TelegramNotifier(session_factory, settings, TelegramClient(settings.telegram))
    assert notifier.scan_once() == 0  # first run: nothing from before is replayed
    with session_factory() as s:
        match = _match(s)
        decision = _decision(s, match)
        bet = add_bet(s, match_id=match.id, status=BetStatus.PLACED, odds="1.85", stake="12.50")
        bet.decision_id, bet.market_raw, bet.selection_raw = decision.id, "Zwycięzca meczu", "Natus Vincere"
        db_state.add_pause_reason(s, PauseCode.LOSS_LIMIT_DAILY, "daily loss limit reached")
        db_state.set_kill_switch(s, True, actor="dashboard", reason="panic")
        s.commit()
    assert notifier.scan_once() == 3
    texts = sent_texts(route)
    assert texts[0].startswith("PAUSED by system [loss_limit_daily]")
    assert texts[1] == "KILL SWITCH ON by dashboard: panic. Send /go to turn it off."
    assert "Bet placed: Natus Vincere vs Vitality" in texts[2]
    assert "Zwycięzca meczu - Natus Vincere" in texts[2] and "odds 1.85, stake 12.50 PLN" in texts[2]
    assert "p 0.812, EV +0.034" in texts[2]
    assert notifier.scan_once() == 0  # nothing twice
    with session_factory() as s:  # settled
        from cs2bot.db.models import Bet

        row = s.get(Bet, bet.id)
        assert row is not None
        row.status, row.settled_at, row.payout, row.pnl = (
            BetStatus.WON,
            utcnow(),
            Decimal("20.35"),
            Decimal("7.85"),
        )
        s.commit()
    assert notifier.scan_once() == 1
    assert sent_texts(route)[-1].startswith("Bet WON: Natus Vincere vs Vitality")
    with session_factory() as s:
        st = db_state.get_state(s, db_state.TELEGRAM)
        assert st["client"]["sent"] == 4 and st["cursor"]["placed_ids"] == [bet.id]


@respx.mock
def test_dry_run_bets_can_be_muted(session_factory: sessionmaker[Session]) -> None:
    route = respx.post(SEND_URL).mock(return_value=ok())
    settings = full_settings(notify_dry_run_bets=False)
    notifier = TelegramNotifier(session_factory, settings, TelegramClient(settings.telegram))
    notifier.scan_once()
    with session_factory() as s:
        add_bet(s, status=BetStatus.DRY_RUN, dry_run=True)
        s.commit()
    assert notifier.scan_once() == 0 and route.call_count == 0
    loud = TelegramNotifier(session_factory, full_settings(), TelegramClient(settings.telegram))
    assert loud.scan_once() == 1 and sent_texts(route)[0].startswith("DRY RUN bet (not placed)")


@respx.mock
def test_a_failed_send_is_retried_on_the_next_scan(session_factory: sessionmaker[Session]) -> None:
    route = respx.post(SEND_URL).mock(side_effect=[httpx.Response(500, text="down"), ok(), ok()])
    settings = full_settings(max_retries=0)
    notifier = TelegramNotifier(session_factory, settings, TelegramClient(settings.telegram, sleep=Sleeps()))
    notifier.scan_once()
    with session_factory() as s:
        db_state.set_paused(s, True, actor="telegram", reason="break")
        db_state.set_paused(s, False, actor="telegram", reason="back")
        s.commit()
    assert notifier.scan_once() == 0  # Telegram down: the cursor stays before the pause
    assert notifier.scan_once() == 2
    assert [t.split()[0] for t in sent_texts(route)[1:]] == ["PAUSED", "RESUMED"]


@respx.mock
def test_job_failures_are_rate_limited_per_job(session_factory: sessionmaker[Session]) -> None:
    route = respx.post(SEND_URL).mock(return_value=ok())
    settings = full_settings(error_alert_cooldown_min=60)
    notifier = TelegramNotifier(session_factory, settings, TelegramClient(settings.telegram))
    t0 = utcnow()
    notifier.scan_once(now=t0)
    with session_factory() as s:
        for minutes in (1, 2, 70):
            s.add(
                JobRun(
                    job_name="ingest_results_hltv",
                    status=JobStatus.FAILED,
                    started_at=t0 + timedelta(minutes=minutes),
                    finished_at=t0 + timedelta(minutes=minutes, seconds=30),
                    error="HLTVBlockedError: challenge",
                )
            )
        s.commit()
    assert notifier.scan_once(now=t0 + timedelta(minutes=71)) == 2
    assert sent_texts(route) == ["Job ingest_results_hltv failed: HLTVBlockedError: challenge"] * 2
    assert notifier.scan_once(now=t0 + timedelta(minutes=72)) == 0  # the scan overlap reports nothing twice


@respx.mock
def test_a_long_job_failing_after_the_scan_is_still_reported(session_factory: sessionmaker[Session]) -> None:
    """Review round 1: failures were looked up by ``started_at`` >= last scan, so a job that was still
    running at the previous scan (e.g. a 20-minute retrain) and failed afterwards was never reported."""
    route = respx.post(SEND_URL).mock(return_value=ok())
    settings = full_settings()
    notifier = TelegramNotifier(session_factory, settings, TelegramClient(settings.telegram))
    t0 = utcnow()
    with session_factory() as s:
        run = JobRun(
            job_name="models_retrain", status=JobStatus.RUNNING, started_at=t0 - timedelta(minutes=10)
        )
        s.add(run)
        s.commit()
        run_id = run.id
    notifier.scan_once(now=t0)  # the retrain is still running
    with session_factory() as s:
        row = s.get(JobRun, run_id)
        assert row is not None
        row.status, row.finished_at, row.error = JobStatus.FAILED, t0 + timedelta(minutes=5), "MemoryError"
        s.commit()
    assert notifier.scan_once(now=t0 + timedelta(minutes=6)) == 1
    assert sent_texts(route) == ["Job models_retrain failed: MemoryError"]


@respx.mock
def test_a_message_telegram_refuses_is_skipped_not_retried_forever(
    session_factory: sessionmaker[Session],
) -> None:
    """Review round 1: a 400 for one message's content used to stop the notifier on that event forever
    (every later event was never sent). A 400 about the chat (a setting) is still retried."""
    refused = httpx.Response(400, json={"ok": False, "description": "Bad Request: message is too long"})
    route = respx.post(SEND_URL).mock(side_effect=[refused, ok(), ok()])
    settings = full_settings(max_retries=0)
    notifier = TelegramNotifier(session_factory, settings, TelegramClient(settings.telegram, sleep=Sleeps()))
    notifier.scan_once()
    with session_factory() as s:
        db_state.set_paused(s, True, actor="telegram", reason="break")
        db_state.set_paused(s, False, actor="telegram", reason="back")
        db_state.set_kill_switch(s, True, actor="telegram", reason="stop")
        s.commit()
    assert notifier.scan_once() == 2  # the refused one is skipped, the next two go out
    assert [t.split()[0] for t in sent_texts(route)[1:]] == ["RESUMED", "KILL"]
    assert notifier.scan_once() == 0


@respx.mock
def test_a_chat_error_is_retried_on_the_next_scan(session_factory: sessionmaker[Session]) -> None:
    chat_missing = httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
    route = respx.post(SEND_URL).mock(side_effect=[chat_missing, ok()])
    settings = full_settings(max_retries=0)
    client = TelegramClient(settings.telegram, sleep=Sleeps())
    notifier = TelegramNotifier(session_factory, settings, client)
    notifier.scan_once()
    with session_factory() as s:
        db_state.set_kill_switch(s, True, actor="telegram", reason="stop")
        s.commit()
    assert notifier.scan_once() == 0 and not client.last_failure_permanent
    assert notifier.scan_once() == 1 and sent_texts(route)[-1].startswith("KILL SWITCH ON")


def test_an_unexpected_transport_error_is_wrapped_and_redacted() -> None:
    class Exploding(httpx.Client):
        def post(self, url: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
            raise ValueError(f"cannot parse {url}")

    client = TelegramClient(tg_settings(), http=Exploding(), sleep=Sleeps())
    with pytest.raises(TelegramError) as info:
        client.call("getMe", {})
    assert TOKEN not in str(info.value) and "<token>" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert client.send("x") is False and TOKEN not in (client.last_error or "")


def test_messages_are_truncated_in_utf16_units() -> None:
    from cs2bot.services.telegram import MAX_MESSAGE_CHARS, truncate_message

    emoji = "\U0001f3c6"  # outside the BMP: two UTF-16 code units
    text = emoji * 3000  # 3000 code points, 6000 UTF-16 units
    out = truncate_message(text)
    assert len(out.encode("utf-16-le")) // 2 <= MAX_MESSAGE_CHARS and out.endswith("(truncated)")
    assert truncate_message("short") == "short"


@respx.mock
def test_a_bounded_send_gives_up_quickly() -> None:
    """The shutdown message: one try, short timeout (Telegram down must not eat the shutdown budget)."""
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
    sleeps = Sleeps()
    client = TelegramClient(tg_settings(max_retries=5), sleep=sleeps)
    assert client.send("bye", attempts=1, timeout_s=2.0) is False
    assert route.call_count == 1 and sleeps.calls == []


def test_format_audit_covers_the_control_events() -> None:
    from cs2bot.db.models import AuditLog

    def row(action: str, **details: Any) -> AuditLog:
        return AuditLog(ts=utcnow(), actor="autonomy", action=action, details=details)

    assert "lifts automatically" not in (format_audit(row("pause", code="manual", reason="x")) or "x")
    assert (format_audit(row("resume", auto=True)) or "").startswith("RESUMED automatically")
    auto = format_audit(
        row("auto_resume", code="login_required", label="Betclic login", resolved_by="auto-login")
    )
    assert auto == "Auto-resume: Betclic login - auto-login"
    assert "CIRCUIT BREAKER OPEN for 'login_required'" in (
        format_audit(row("circuit_open", code="login_required", resumes=3)) or ""
    )
    assert "SMS / 2FA" in (format_audit(row("auto_login", status="2fa_required")) or "")
    assert "STOPPED (bad_credentials)" in (format_audit(row("auto_login", status="bad_credentials")) or "")
    assert format_audit(row("config_overrides")) is None


def test_daily_report_text(session_factory: sessionmaker[Session]) -> None:
    settings = Settings()
    now = utcnow()
    with session_factory() as s:
        ledger.ensure_initialized(s, settings)
        add_bet(s, status=BetStatus.PLACED, placed_at=now)
        add_bet(s, status=BetStatus.DRY_RUN, dry_run=True)
        db_state.add_pause_reason(s, PauseCode.UNCERTAIN_BET, "x")
        s.commit()
        text = daily_report_text(s, settings, now)
    assert text.startswith("Daily report") and "DRY RUN" in text
    assert "Bets placed today 1 (+1 dry-run)" in text and "Paused: uncertain_bet" in text


# ============================================================================================= commands


def _update(text: str, *, chat: str = CHAT, age_s: float = 0, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": int(chat)}, "text": text, "date": int(time.time() - age_s)},
    }


def _commands(factory: sessionmaker[Session], **kw: Any) -> TelegramCommands:
    settings = full_settings(**kw)
    return TelegramCommands(factory, settings, TelegramClient(settings.telegram, sleep=Sleeps()))


def test_pause_resume_stop_go(session_factory: sessionmaker[Session]) -> None:
    cmd = _commands(session_factory)
    assert cmd.handle_update(_update("/pause watching the major")) is not None
    with session_factory() as s:
        (reason,) = db_state.get_pause_reasons(s)
        assert reason.code is PauseCode.MANUAL and reason.message == "watching the major"
        assert reason.actor == "telegram"
    assert "KILL SWITCH ON" in (cmd.handle_update(_update("/stop")) or "")
    reply = cmd.handle_update(_update("/resume"))
    assert reply is not None and "KILL SWITCH is still ON" in reply  # /resume never touches it
    with session_factory() as s:
        assert db_state.is_kill_switch_on(s) and not db_state.get_pause(s)[0]
    assert cmd.handle_update(_update("/go@cs2_bot")) == "Kill switch OFF."
    with session_factory() as s:
        assert db_state.is_trading_halted(s) == (False, None)


def test_status_balance_bets_help(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        ledger.ensure_initialized(s, Settings())
        db_state.add_pause_reason(
            s,
            PauseCode.LOSS_LIMIT_DAILY,
            "daily loss limit reached",
            details={"period": "2026-01-01T00:00:00+00:00"},
        )
        add_bet(s, status=BetStatus.PLACED)
        s.commit()
    cmd = _commands(session_factory)
    status = cmd.handle_update(_update("/status")) or ""
    assert "Mode: DRY RUN" in status and "loss_limit_daily" in status and "Kill switch: off" in status
    assert "Ledger balance: 1000.00 PLN" in (cmd.handle_update(_update("/balance")) or "")
    assert "placed" in (cmd.handle_update(_update("/bets")) or "")
    assert cmd.handle_update(_update("/help")) == HELP_TEXT
    assert "Unknown command" in (cmd.handle_update(_update("/hack")) or "")
    assert cmd.handle_update(_update("just chatting")) is None


def test_other_chats_and_stale_commands_are_ignored(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    cmd = _commands(session_factory)
    with caplog.at_level(logging.WARNING):
        assert cmd.handle_update(_update("/stop", chat="999")) is None
        assert cmd.handle_update(_update("/go", age_s=3600)) is None
    assert "ignored a message from chat 999" in caplog.text and "stale command" in caplog.text
    assert cmd.handle_update({"update_id": 3}) is None
    with session_factory() as s:
        assert not db_state.is_kill_switch_on(s)


@pytest.mark.parametrize(
    "chat",
    [
        {"id": 999, "type": "private", "username": "owner"},  # username matches an "@owner" chat id
        {"id": "424242", "type": "private"},  # a string id is not what Telegram sends
        {"id": True},
        {"username": "owner"},
        "424242",
    ],
)
def test_only_the_numeric_chat_id_may_command_the_bot(
    session_factory: sessionmaker[Session], chat: Any
) -> None:
    """Review round 1: a chat whose *username* matched an ``@name`` chat id was obeyed; usernames can
    be changed and re-registered by someone else, so only the numeric chat id counts now."""
    for wanted in ("@owner", CHAT):
        cmd = _commands(session_factory, chat_id=wanted)
        update = {"update_id": 5, "message": {"chat": chat, "text": "/go", "date": int(time.time())}}
        assert cmd.handle_update(update) is None
    with session_factory() as s:
        assert db_state.get_state(s, db_state.KILL_SWITCH) == {}


@respx.mock
def test_poll_once_answers_and_persists_the_offset(session_factory: sessionmaker[Session]) -> None:
    updates = respx.post(UPDATES_URL).mock(
        side_effect=[ok([_update("/pause", update_id=10), _update("/help", update_id=11)]), ok([])]
    )
    send = respx.post(SEND_URL).mock(return_value=ok())
    cmd = _commands(session_factory)
    assert cmd.poll_once() == 2
    assert [t.split()[0] for t in sent_texts(send)] == ["Paused", "cs2bot"]
    assert cmd.poll_once() == 0
    assert json.loads(updates.calls[1].request.content)["offset"] == 12
    with session_factory() as s:
        assert db_state.get_state(s, "telegram_poll")["offset"] == 12


@respx.mock
def test_poll_backs_off_on_network_errors(session_factory: sessionmaker[Session]) -> None:
    respx.post(UPDATES_URL).mock(side_effect=httpx.ConnectError("down"))
    cmd = _commands(session_factory, max_retries=0)
    sleeps = Sleeps()
    calls = iter([False, False, True])
    assert cmd.poll(should_stop=lambda: next(calls), session_s=60, sleep=sleeps) == 0
    assert sleeps.calls == [5.0, 10.0]


# ================================================================================================= wiring


def test_jobs_are_enabled_with_a_client(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    settings = full_settings()
    settings.autonomy.enabled = True
    settings.models.artifacts_dir = tmp_path / "models"
    client = TelegramClient(settings.telegram)
    rt = Runtime(
        settings=settings, session_factory=session_factory, registry=ModelRegistry(settings), telegram=client
    )
    jobs = {j.name: j for j in build_jobs(rt)}
    assert jobs["telegram_notify"].enabled and jobs["telegram_notify"].lane == NOTIFY
    assert jobs["telegram_daily_report"].enabled and jobs["telegram_commands"].lane == TELEGRAM
    assert jobs["autonomy_check"].enabled and jobs["autonomy_check"].lane == "fast"


def test_run_up_sends_startup_and_shutdown_messages(tmp_path: Path) -> None:
    from alembic import command

    from cs2bot.db.base import Base
    from cs2bot.db.session import make_engine, make_session_factory
    from cs2bot.executor import FakeExecutor
    from cs2bot.flows.supervisor import EXIT_OK, UpHandles, run_up
    from cs2bot.odds import StaticOddsSource
    from cs2bot.services.dbops import alembic_config

    settings = full_settings(commands_enabled=False, daily_report=False)
    settings.database.url = f"sqlite:///{tmp_path / 'bot.db'}"
    settings.data_dir = tmp_path / "data"
    settings.models.artifacts_dir = tmp_path / "models"
    settings.scheduler.tick_s = 0.05
    settings.llm.enabled = False
    settings.autonomy.enabled = True
    engine = make_engine(settings.database.url)
    Base.metadata.create_all(engine)
    command.stamp(alembic_config(settings.database.url), "head")
    factory = make_session_factory(engine)
    with factory() as s:
        db_state.set_state(s, db_state.AUTO_LOGIN, {"status": "bad_credentials", "blocked": True})
        s.commit()

    class Recorder:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.options: list[dict[str, Any]] = []
            self.closed = False
            self.last_error: str | None = None

        def send(self, text: str, **options: Any) -> bool:
            self.texts.append(text)
            self.options.append(options)
            return True

        def status(self) -> dict[str, Any]:
            return {}

        def close(self) -> None:
            self.closed = True

    recorder = Recorder()

    def started(h: UpHandles) -> None:
        assert h.runtime.telegram is recorder
        h.stop.set()

    code = run_up(
        settings,
        session_factory=factory,
        executor_factory=lambda s, f: FakeExecutor(),
        telegram_factory=lambda s: recorder,
        runtime_overrides={"odds_source_factory": lambda: StaticOddsSource([]), "hltv_enabled": False},
        start_api=False,
        train_if_missing=False,
        on_started=started,
        echo=lambda _line: None,
    )
    assert code == EXIT_OK
    assert recorder.texts[0].startswith("cs2bot started. Betting mode: DRY RUN")
    assert "Autonomy on" in recorder.texts[0]
    assert recorder.texts[-1] == "cs2bot is stopping (normal stop)." and recorder.closed
    assert recorder.options[-1] == {"attempts": 1, "timeout_s": 5.0}  # never eats the shutdown budget
    with factory() as s:
        # review round 1: a wrong password stays stopped across restarts (a crash-looping container must
        # not submit it again and again); only the owner's resume clears it
        assert db_state.get_state(s, db_state.AUTO_LOGIN)["blocked"] is True
    engine.dispose()


def test_now_is_aware() -> None:
    assert utcnow().tzinfo is UTC and datetime.now(UTC) - utcnow() < timedelta(seconds=5)
