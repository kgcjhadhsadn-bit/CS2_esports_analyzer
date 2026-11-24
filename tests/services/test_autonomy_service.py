"""Structured pause reasons and the auto-resume service (cloud autonomy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import AutonomySettings, Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import AuditLog, BankrollLedger, Bet, SystemState
from cs2bot.domain.betting import ExecutorHealth
from cs2bot.domain.control import LoginStatus, PauseCode
from cs2bot.domain.enums import BetStatus, LedgerKind
from cs2bot.engine import ledger
from cs2bot.engine.bankroll import check_loss_limits, compute_state
from cs2bot.executor import FakeExecutor
from cs2bot.services.autonomy import (
    AutonomyService,
    auto_resume_count,
    clear_login_block,
    next_local_midnight,
    next_local_monday,
    pause_overview,
)
from cs2bot.utils.time import utcnow
from tests.engine.engine_factories import add_bet

# Thursday 2026-01-15 10:00 UTC = 11:00 Warsaw
T = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)


def _settings(**autonomy: Any) -> Settings:
    s = Settings()
    s.autonomy = AutonomySettings(**{"enabled": True, "auto_login": True, **autonomy})
    return s


def _codes(factory: sessionmaker[Session]) -> list[str]:
    with factory() as s:
        return [r.code.value for r in db_state.get_pause_reasons(s)]


def _pause(factory: sessionmaker[Session], code: PauseCode, msg: str = "x", **kw: Any) -> None:
    with factory() as s:
        db_state.add_pause_reason(s, code, msg, **kw)
        s.commit()


def _audits(factory: sessionmaker[Session], action: str) -> list[dict[str, Any]]:
    with factory() as s:
        return [
            dict(a.details)
            for a in s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))
        ]


# ------------------------------------------------------------------------ structured pause state


def test_pause_reasons_are_structured_merged_and_resolved(session: Session) -> None:
    assert db_state.add_pause_reason(session, PauseCode.LOSS_LIMIT_DAILY, "limit", details={"period": "p"})
    assert not db_state.add_pause_reason(session, PauseCode.BALANCE_MISMATCH, "mismatch")
    assert not db_state.add_pause_reason(session, PauseCode.LOSS_LIMIT_DAILY, "limit again")
    reasons = db_state.get_pause_reasons(session)
    assert [r.code for r in reasons] == [PauseCode.LOSS_LIMIT_DAILY, PauseCode.BALANCE_MISMATCH]
    assert reasons[0].count == 2 and reasons[0].message == "limit; limit again"
    assert reasons[0].details == {"period": "p"}
    assert db_state.get_pause(session) == (True, "limit; limit again; mismatch")
    raw = session.get(SystemState, db_state.PAUSED)
    assert raw is not None and raw.value["reasons"][1]["code"] == "balance_mismatch"
    # resolving one code keeps the bot paused for the other
    removed = db_state.resolve_pause_reasons(session, ["loss_limit_daily"], actor="autonomy")
    assert [r.code for r in removed] == [PauseCode.LOSS_LIMIT_DAILY]
    assert db_state.is_trading_halted(session) == (True, "paused: mismatch")
    db_state.resolve_pause_reasons(session, [PauseCode.BALANCE_MISMATCH], actor="autonomy")
    assert db_state.is_trading_halted(session) == (False, None)
    resumes = list(session.scalars(select(AuditLog).where(AuditLog.action == "resume")))
    assert len(resumes) == 1 and resumes[0].details["auto"] is True


def test_manual_pause_and_owner_resume(session: Session) -> None:
    db_state.add_pause_reason(session, PauseCode.SELECTOR_ERROR, "layout changed")
    db_state.set_paused(session, True, actor="dashboard", reason="holiday")
    db_state.set_paused(session, True, actor="telegram", reason="still on holiday")  # replaces the text
    codes = [(r.code, r.message) for r in db_state.get_pause_reasons(session)]
    assert codes == [(PauseCode.SELECTOR_ERROR, "layout changed"), (PauseCode.MANUAL, "still on holiday")]
    db_state.set_state(session, db_state.AUTO_LOGIN, {"status": "bad_credentials", "blocked": True})
    db_state.set_paused(session, False, actor="owner")  # the owner's resume clears everything
    assert db_state.get_pause_reasons(session) == [] and db_state.get_pause(session)[0] is False
    assert db_state.get_state(session, db_state.AUTO_LOGIN)["blocked"] is False


def test_legacy_and_unknown_pause_codes_are_owner_only(session: Session) -> None:
    db_state.set_state(session, db_state.PAUSED, {"on": True, "reason": "old style pause"})
    (legacy,) = db_state.get_pause_reasons(session)
    assert (
        legacy.code is PauseCode.OTHER and legacy.message == "old style pause" and not legacy.auto_resumable
    )
    db_state.set_state(
        session,
        db_state.PAUSED,
        {"on": True, "reason": "x", "reasons": [{"code": "made_up", "message": "x", "since": "garbage"}]},
    )
    (unknown,) = db_state.get_pause_reasons(session)
    assert unknown.code is PauseCode.OTHER
    db_state.set_state(session, db_state.PAUSED, {"on": True, "reason": "x", "reasons": ["junk"]})
    assert [r.code for r in db_state.get_pause_reasons(session)] == [PauseCode.OTHER]  # never unpaused


def test_loss_limit_and_reconcile_record_their_codes(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings, at=datetime(2026, 1, 1, tzinfo=UTC))
    for _ in range(2):
        bet = add_bet(session, stake="25", status=BetStatus.PLACED, placed_at=T - timedelta(hours=3))
        ledger.record_stake(session, bet, at=T - timedelta(hours=3))
        bet.status, bet.settled_at, bet.pnl, bet.payout = (
            BetStatus.LOST,
            T - timedelta(hours=1),
            Decimal("-25"),
            Decimal("0"),
        )
    session.flush()
    st = compute_state(session, settings, now=T)
    assert check_loss_limits(session, st, settings, now=T)
    (daily,) = db_state.get_pause_reasons(session)
    assert daily.code is PauseCode.LOSS_LIMIT_DAILY and daily.details["period"] == "2026-01-14T23:00:00+00:00"
    result = ledger.reconcile(session, Decimal("1.00"), tolerance=Decimal("0.05"), at=T)
    assert result.flagged
    assert [r.code for r in db_state.get_pause_reasons(session)][-1] is PauseCode.BALANCE_MISMATCH


# ------------------------------------------------------------------------------- time-based reasons


def test_next_local_midnight_and_monday_are_dst_safe() -> None:
    assert next_local_midnight(T, "Europe/Warsaw") == datetime(2026, 1, 15, 23, 0, tzinfo=UTC)
    assert next_local_monday(T, "Europe/Warsaw") == datetime(2026, 1, 18, 23, 0, tzinfo=UTC)
    # the night the clocks go forward (29 March 2026): midnight is 23:00 UTC, the next one 22:00 UTC
    assert next_local_midnight(datetime(2026, 3, 29, 12, tzinfo=UTC), "Europe/Warsaw") == datetime(
        2026, 3, 29, 22, 0, tzinfo=UTC
    )


def test_daily_loss_limit_lifts_at_warsaw_midnight(session_factory: sessionmaker[Session]) -> None:
    settings = _settings()
    period = "2026-01-14T23:00:00+00:00"  # local day of T
    _pause(session_factory, PauseCode.LOSS_LIMIT_DAILY, "daily loss limit", details={"period": period}, at=T)
    service = AutonomyService(session_factory, settings)
    report = service.run_once(now=T + timedelta(hours=5))  # 16:00 Warsaw: same day
    assert report.checks[0].status == "waiting" and report.checks[0].eta == datetime(
        2026, 1, 15, 23, tzinfo=UTC
    )
    assert _codes(session_factory) == ["loss_limit_daily"]
    with session_factory() as s:
        overview = pause_overview(s, settings, now=T + timedelta(hours=5))
    assert overview["reasons"][0]["auto_resume"] and overview["reasons"][0]["eta"].startswith(
        "2026-01-15T23:00"
    )
    report = service.run_once(now=datetime(2026, 1, 15, 23, 1, tzinfo=UTC))  # 00:01 Warsaw
    assert report.resumed == ["loss_limit_daily"]
    assert _codes(session_factory) == []
    (audit,) = _audits(session_factory, "auto_resume")
    assert audit["code"] == "loss_limit_daily" and audit["still_paused"] is False


def test_weekly_limit_waits_for_monday(session_factory: sessionmaker[Session]) -> None:
    _pause(
        session_factory,
        PauseCode.LOSS_LIMIT_WEEKLY,
        "weekly",
        details={"period": "2026-01-11T23:00:00+00:00"},
        at=T,
    )
    service = AutonomyService(session_factory, _settings())
    assert service.run_once(now=datetime(2026, 1, 18, 22, 59, tzinfo=UTC)).resumed == []  # Sun 23:59
    assert service.run_once(now=datetime(2026, 1, 18, 23, 0, tzinfo=UTC)).resumed == ["loss_limit_weekly"]


def test_manual_pause_and_kill_switch_are_never_lifted(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        db_state.set_paused(s, True, actor="owner", reason="I said so")
        db_state.add_pause_reason(
            s, PauseCode.LOSS_LIMIT_DAILY, "limit", details={"period": "2026-01-01T00:00:00+00:00"}
        )
        db_state.set_kill_switch(s, True, actor="owner", reason="stop")
        s.commit()
    report = AutonomyService(session_factory, _settings()).run_once(now=T)
    assert {c.code: c.status for c in report.checks} == {"manual": "owner", "loss_limit_daily": "resumed"}
    assert _codes(session_factory) == ["manual"]
    with session_factory() as s:
        assert db_state.is_kill_switch_on(s)


def test_auto_resume_policy_can_exclude_reasons(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.LOSS_LIMIT_DAILY, "x", details={"period": "2026-01-01T00:00:00+00:00"})
    off = AutonomyService(session_factory, _settings(auto_resume=False)).run_once(now=T)
    assert off.checks[0].status == "disabled"
    narrow = AutonomyService(session_factory, _settings(auto_resume_reasons=["balance_mismatch"])).run_once(
        now=T
    )
    assert narrow.checks[0].status == "disabled" and _codes(session_factory) == ["loss_limit_daily"]


# ------------------------------------------------------------------------------------ uncertain bets


def test_uncertain_bets_lift_once_settlement_resolved_them(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        bet = add_bet(s, status=BetStatus.PLACING)
        add_bet(s, status=BetStatus.PLACING, dry_run=True)  # paper bets never count
        s.commit()
        bet_id = bet.id
    _pause(session_factory, PauseCode.UNCERTAIN_BET, "2 unconfirmed")
    service = AutonomyService(session_factory, _settings())
    report = service.run_once(now=T)
    assert report.checks[0].status == "waiting" and "1 unconfirmed" in report.checks[0].detail
    with session_factory() as s:
        row = s.get(Bet, bet_id)
        assert row is not None
        row.status = BetStatus.PLACED  # found in the Betclic bet history by settlement
        s.commit()
    assert service.run_once(now=T).resumed == ["uncertain_bet"]


# ---------------------------------------------------------------------------------------- login


def test_login_required_is_lifted_by_auto_login(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(logged_in=False)
    report = AutonomyService(session_factory, _settings(), fake).run_once(now=T)
    assert report.resumed == ["login_required"]
    assert report.login is not None and report.login.status is LoginStatus.LOGGED_IN
    assert [c[0] for c in fake.calls] == ["is_logged_in", "auto_login"]
    assert _audits(session_factory, "auto_login")[0]["status"] == "logged_in"


def test_owner_login_lifts_it_without_auto_login(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(logged_in=False)
    service = AutonomyService(session_factory, _settings(auto_login=False), fake)
    first = service.run_once(now=T)
    assert first.checks[0].status == "waiting" and "auto-login is off" in first.checks[0].detail
    fake.logged_in = True  # the owner logged in from the dashboard
    assert service.run_once(now=T).resumed == ["login_required"]
    assert ("auto_login", None) not in fake.calls


def test_2fa_stops_and_retries_rarely(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(
        logged_in=False, auto_login_results=[LoginStatus.TWO_FA_REQUIRED, LoginStatus.LOGGED_IN]
    )
    service = AutonomyService(session_factory, _settings(twofa_retry_interval_min=240), fake)
    report = service.run_once(now=T)
    assert report.login is not None and report.login.status is LoginStatus.TWO_FA_REQUIRED
    assert _codes(session_factory) == ["login_required", "2fa_required"]
    # within the 2FA interval: no new login attempt (each could send an SMS)
    later = service.run_once(now=T + timedelta(minutes=30))
    assert later.login is None and all(c.status == "waiting" for c in later.checks)
    assert later.checks[0].eta == T + timedelta(minutes=240)
    assert sum(1 for c in fake.calls if c[0] == "auto_login") == 1
    done = service.run_once(now=T + timedelta(minutes=241))
    assert sorted(done.resumed) == ["2fa_required", "login_required"] and _codes(session_factory) == []


def test_wrong_password_stops_the_retries_until_the_owner_acts(
    session_factory: sessionmaker[Session],
) -> None:
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(logged_in=False, auto_login_results=[LoginStatus.BAD_CREDENTIALS])
    service = AutonomyService(session_factory, _settings(), fake)
    service.run_once(now=T)
    blocked = service.run_once(now=T + timedelta(days=2))
    assert "auto-login stopped" in blocked.checks[0].detail
    assert sum(1 for c in fake.calls if c[0] == "auto_login") == 1
    with session_factory() as s:  # a restart does NOT clear it (a crash loop must not hammer Betclic)
        assert not clear_login_block(s, actor="startup", startup=True)
        s.commit()
    assert service.run_once(now=T + timedelta(days=3)).login is None
    with session_factory() as s:  # the owner's resume does (and lifts the pause: re-pause to retry)
        db_state.set_paused(s, False, actor="telegram")
        db_state.add_pause_reason(s, PauseCode.LOGIN_REQUIRED, "Betclic login required")
        s.commit()
    assert service.run_once(now=T + timedelta(days=3)).resumed == ["login_required"]


def test_startup_clears_only_a_block_that_never_reached_betclic(session: Session) -> None:
    db_state.set_state(session, db_state.AUTO_LOGIN, {"status": "no_credentials", "blocked": True})
    assert clear_login_block(session, actor="startup", startup=True)  # the owner just added them
    assert db_state.get_state(session, db_state.AUTO_LOGIN)["blocked"] is False
    for status in ("bad_credentials", "account_locked", "failed"):
        db_state.set_state(session, db_state.AUTO_LOGIN, {"status": status, "blocked": True})
        assert not clear_login_block(session, actor="startup", startup=True)
        assert db_state.get_state(session, db_state.AUTO_LOGIN)["blocked"] is True
    assert clear_login_block(session, actor="owner")  # not at start-up: always


def test_a_manual_login_clears_an_old_login_back_off(session_factory: sessionmaker[Session]) -> None:
    """Review round 1: after an SMS step the owner logs in by hand; when the session expires later the
    auto-login must not wait out the old 4-hour 2FA interval (or a failure back-off)."""
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(logged_in=False, auto_login_results=[LoginStatus.TWO_FA_REQUIRED])
    service = AutonomyService(session_factory, _settings(), fake)
    service.run_once(now=T)
    fake.logged_in = True  # the owner typed the SMS code over VNC
    assert sorted(service.run_once(now=T + timedelta(minutes=30)).resumed) == [
        "2fa_required",
        "login_required",
    ]
    fake.logged_in = False  # the session expires an hour later
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    later = service.run_once(now=T + timedelta(minutes=90))
    assert later.login is not None and later.resumed == ["login_required"]


def test_unrecognised_login_failures_stop_after_the_limit(session_factory: sessionmaker[Session]) -> None:
    """Review round 1: an error text the classifier does not know (a reworded "wrong password") was
    retried for ever with back-off - enough submissions to lock the account. Now at most
    ``max_login_failures`` submitted failures in a row, then the owner decides."""
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(
        logged_in=False,
        auto_login_results=[
            LoginStatus.FAILED,
            LoginStatus.FORM_NOT_FOUND,  # nothing typed: does not count
            LoginStatus.FAILED,
            LoginStatus.FAILED,
            LoginStatus.LOGGED_IN,
        ],
    )
    service = AutonomyService(
        session_factory, _settings(max_login_failures=3, login_retry_interval_min=1), fake
    )
    when = T
    for _ in range(4):
        report = service.run_once(now=when)
        assert report.login is not None and not report.login.status.logged_in
        when += timedelta(days=1)  # past any back-off
    with session_factory() as s:
        memo = db_state.get_state(s, db_state.AUTO_LOGIN)
    assert memo["blocked"] is True and memo["submit_failures"] == 3
    assert "3 submitted logins failed in a row" in memo["message"]
    stopped = service.run_once(now=when + timedelta(days=5))
    assert stopped.login is None and "auto-login stopped" in stopped.checks[0].detail
    assert sum(1 for c in fake.calls if c[0] == "auto_login") == 4
    alerts = _audits(session_factory, "auto_login")
    assert alerts[-1]["blocked"] is True
    from cs2bot.db.models import AuditLog as _Row
    from cs2bot.services.telegram import format_audit

    text = format_audit(_Row(ts=T, actor="autonomy", action="auto_login", details=alerts[-1]))
    assert text is not None and "STOPPED" in text and "/resume" in text


def test_the_sms_step_resets_the_failure_budget(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(
        logged_in=False,
        auto_login_results=[LoginStatus.FAILED, LoginStatus.FAILED, LoginStatus.TWO_FA_REQUIRED],
    )
    service = AutonomyService(session_factory, _settings(max_login_failures=3), fake)
    for day in range(3):
        service.run_once(now=T + timedelta(days=day))
    with session_factory() as s:
        memo = db_state.get_state(s, db_state.AUTO_LOGIN)
    assert memo["status"] == "2fa_required" and memo["submit_failures"] == 0 and not memo["blocked"]


def test_a_crashing_auto_login_reports_one_line_only(session_factory: sessionmaker[Session]) -> None:
    class Crashing(FakeExecutor):
        def auto_login(self, timeout_s: float | None = None) -> Any:
            raise RuntimeError('Locator.fill: Timeout\nCall log:\n  - fill("hunter2-secret")')

    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    report = AutonomyService(session_factory, _settings(), Crashing(logged_in=False)).run_once(now=T)
    assert report.login is not None and report.login.status is LoginStatus.FAILED
    assert "hunter2" not in report.login.message and "Locator.fill" in report.login.message
    with session_factory() as s:
        assert "hunter2" not in str(db_state.get_state(s, db_state.AUTO_LOGIN))
    assert all("hunter2" not in str(a) for a in _audits(session_factory, "auto_login"))


def test_failed_logins_back_off(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake = FakeExecutor(logged_in=False, auto_login_results=[LoginStatus.FAILED, LoginStatus.FAILED])
    service = AutonomyService(session_factory, _settings(login_retry_interval_min=15), fake)
    service.run_once(now=T)
    assert service.run_once(now=T + timedelta(minutes=14)).login is None
    second = service.run_once(now=T + timedelta(minutes=15))
    assert second.login is not None and second.login.status is LoginStatus.FAILED
    # the second failure doubles the wait: 30 minutes
    assert service.run_once(now=T + timedelta(minutes=44)).login is None
    assert service.run_once(now=T + timedelta(minutes=46)).resumed == ["login_required"]


# ------------------------------------------------------------------------------- balance mismatch


def test_balance_mismatch_is_booked_and_lifted(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    with session_factory() as s:
        ledger.ensure_initialized(s, settings)
        result = ledger.reconcile(s, Decimal("900.00"), tolerance=Decimal("0.05"))
        assert result.paused
        s.commit()
    fake = FakeExecutor(balance=Decimal("880.00"))  # moved again since
    report = AutonomyService(session_factory, _settings(), fake).run_once(now=utcnow())
    assert report.resumed == ["balance_mismatch"]
    with session_factory() as s:
        assert ledger.current_balance(s) == Decimal("880.00")
    (accepted,) = _audits(session_factory, "balance_adjustment_accepted")
    assert accepted["difference_now"] == "-20.00"


def test_balance_mismatch_without_executor_accepts_the_booked_entry(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    with session_factory() as s:
        ledger.ensure_initialized(s, settings)
        ledger.reconcile(s, Decimal("1100.00"), tolerance=Decimal("0.05"))
        s.commit()
    assert AutonomyService(session_factory, _settings()).run_once(now=utcnow()).resumed == [
        "balance_mismatch"
    ]
    with session_factory() as s:
        assert ledger.current_balance(s) == Decimal("1100.00")


def test_balance_mismatch_waits_while_unconfirmed_bets_explain_it(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    with session_factory() as s:
        ledger.ensure_initialized(s, settings)
        add_bet(s, stake="20", status=BetStatus.PLACING)
        s.commit()
    _pause(session_factory, PauseCode.BALANCE_MISMATCH, "mismatch")
    report = AutonomyService(session_factory, _settings(), FakeExecutor(balance=Decimal("980.00"))).run_once()
    assert report.checks[0].status == "waiting" and "unconfirmed" in report.checks[0].detail
    with session_factory() as s:
        assert [e.kind for e in s.scalars(select(BankrollLedger))] == [LedgerKind.DEPOSIT]


# ---------------------------------------------------------------------------- selector / executor


def test_selector_error_needs_a_passing_health_check(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.SELECTOR_ERROR, "layout changed", at=utcnow() - timedelta(minutes=1))
    broken = ExecutorHealth(
        ok=False, logged_in=True, message="selector 'event_link' missing", checked_at=utcnow()
    )
    fake = FakeExecutor(health=broken)
    service = AutonomyService(session_factory, _settings(), fake)
    report = service.run_once()
    assert report.checks[0].status == "waiting" and "event_link" in report.checks[0].detail
    fake.health = None  # healthy again (selectors fixed / page back)
    assert service.run_once().resumed == ["selector_error"]
    with session_factory() as s:
        assert db_state.get_state(s, db_state.EXECUTOR_HEALTH)["ok"] is True


def test_no_executor_means_waiting(session_factory: sessionmaker[Session]) -> None:
    _pause(session_factory, PauseCode.EXECUTOR_UNAVAILABLE, "gone")
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "login")
    report = AutonomyService(session_factory, _settings()).run_once()
    assert {c.status for c in report.checks} == {"waiting"}


# ------------------------------------------------------------------------------- circuit breaker


def test_circuit_breaker_opens_after_repeated_resumes(session_factory: sessionmaker[Session]) -> None:
    settings = _settings(circuit_breaker_limit=3, circuit_breaker_window_h=24)
    fake = FakeExecutor(logged_in=False)
    service = AutonomyService(session_factory, settings, fake)
    now = utcnow()
    for i in range(3):  # the session keeps expiring right after each login
        _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
        fake.logged_in = False
        assert service.run_once(now=now + timedelta(minutes=i)).resumed == ["login_required"]
    with session_factory() as s:
        assert auto_resume_count(s, "login_required", since=now - timedelta(hours=1)) == 3
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "Betclic login required")
    fake.logged_in = False
    report = service.run_once(now=now + timedelta(minutes=5))
    assert report.circuit_opened == ["login_required"]
    assert _codes(session_factory) == ["login_required", "circuit_open"]
    (opened,) = _audits(session_factory, "circuit_open")
    assert opened["code"] == "login_required" and opened["resumes"] == 3
    # stays open on later checks (no further login attempts are lifted) ...
    again = service.run_once(now=now + timedelta(minutes=10))
    assert {c.code: c.status for c in again.checks} == {
        "login_required": "circuit_open",
        "circuit_open": "owner",
    }
    with session_factory() as s:
        overview = pause_overview(s, settings)
        assert overview["circuit_open"] and overview["circuit_codes"] == ["login_required"]
        assert not any(r["auto_resume"] for r in overview["reasons"])
        db_state.set_paused(s, False, actor="owner")  # ... until the owner resumes
        s.commit()
    assert _codes(session_factory) == []


def test_two_codes_tripping_the_breaker_both_stay_held(session_factory: sessionmaker[Session]) -> None:
    """Review round 1: the circuit_open reason kept a single ``details["code"]``; a second code tripping
    the breaker overwrote it, so the first code was checked again on every run (auto-login attempts,
    health checks) and re-opened the breaker each time - a new alert every few minutes."""
    settings = _settings(circuit_breaker_limit=1)
    fake = FakeExecutor(logged_in=True)
    service = AutonomyService(session_factory, settings, fake)
    now = utcnow()
    for code in (PauseCode.LOGIN_REQUIRED, PauseCode.SELECTOR_ERROR):
        _pause(session_factory, code, "x", at=now - timedelta(minutes=30))
        assert service.run_once(now=now).resumed == [code.value]
        _pause(session_factory, code, "x again", at=now - timedelta(minutes=20))
    first = service.run_once(now=now + timedelta(minutes=1))
    assert sorted(first.circuit_opened) == ["login_required", "selector_error"]
    executor_calls = len(fake.calls)
    for i in range(2, 5):
        later = service.run_once(now=now + timedelta(minutes=i))
        assert later.resumed == [] and later.login is None
        assert all(
            c.detail == "circuit breaker open - waiting for the owner"
            for c in later.checks
            if c.code != "circuit_open"
        )
    assert len(fake.calls) == executor_calls  # held codes are not checked again (no logins, no health checks)
    assert len(_audits(session_factory, "circuit_open")) == 2  # one alert per code, not one per run
    with session_factory() as s:
        overview = pause_overview(s, settings)
    assert overview["circuit_codes"] == ["login_required", "selector_error"]
    assert not any(r["auto_resume"] for r in overview["reasons"])


def test_the_owner_resume_restarts_the_breaker_window(session_factory: sessionmaker[Session]) -> None:
    """After the owner looked at an open breaker and resumed, the next genuine pause may lift itself
    again (previously the old resumes in the window re-opened the breaker at once)."""
    settings = _settings(circuit_breaker_limit=2)
    fake = FakeExecutor(logged_in=True)
    service = AutonomyService(session_factory, settings, fake)
    now = utcnow()
    for i in range(2):
        _pause(session_factory, PauseCode.LOGIN_REQUIRED, "x")
        assert service.run_once(now=now + timedelta(seconds=i)).resumed == ["login_required"]
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "x")
    assert service.run_once(now=now + timedelta(seconds=5)).circuit_opened == ["login_required"]
    with session_factory() as s:
        db_state.set_paused(s, False, actor="telegram", reason="checked it")
        s.commit()
    _pause(session_factory, PauseCode.LOGIN_REQUIRED, "x")
    later = utcnow() + timedelta(seconds=1)
    assert service.run_once(now=later).resumed == ["login_required"]


def test_pause_writers_lock_the_pause_row(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every read-modify-write of ``system_state["paused"]`` reads the row FOR UPDATE (PostgreSQL), so
    an auto-resume can never drop a manual pause written concurrently by Telegram / the dashboard."""
    calls: list[bool] = []
    real = db_state._pause_row

    def spy(sess: Session, *, lock: bool) -> Any:
        calls.append(lock)
        return real(sess, lock=lock)

    monkeypatch.setattr(db_state, "_pause_row", spy)
    db_state.add_pause_reason(session, PauseCode.LOGIN_REQUIRED, "x")
    assert calls and calls[0] is True
    calls.clear()
    db_state.resolve_pause_reasons(session, ["login_required"], actor="autonomy")
    assert calls and calls[0] is True
    calls.clear()
    db_state.set_paused(session, False, actor="owner")
    assert calls and calls[0] is True
    calls.clear()
    db_state.get_pause_reasons(session)  # plain readers do not lock
    assert calls == [False]


@pytest.mark.postgres
def test_concurrent_pause_and_auto_resume_on_postgres(pg_session: Session) -> None:
    import threading
    import time as _time

    factory = sessionmaker(bind=pg_session.get_bind(), expire_on_commit=False)
    with factory() as s:
        db_state.add_pause_reason(s, PauseCode.LOGIN_REQUIRED, "Betclic login required")
        s.commit()
    owner = factory()
    db_state.set_paused(owner, True, actor="telegram", reason="I am watching")  # holds the row lock
    resumed: list[int] = []

    def auto_resume() -> None:
        with factory() as s:
            resumed.append(len(db_state.resolve_pause_reasons(s, ["login_required"], actor="autonomy")))
            s.commit()

    worker = threading.Thread(target=auto_resume)
    worker.start()
    _time.sleep(0.5)
    assert worker.is_alive()  # waits for the owner's transaction instead of reading a stale state
    owner.commit()
    owner.close()
    worker.join(10)
    assert resumed == [1]
    with factory() as s:
        assert [r.code for r in db_state.get_pause_reasons(s)] == [PauseCode.MANUAL]  # never dropped


def test_resumes_outside_the_window_do_not_count(session_factory: sessionmaker[Session]) -> None:
    old = utcnow() - timedelta(days=3)
    with session_factory() as s:
        for _ in range(5):
            s.add(
                AuditLog(ts=old, actor="autonomy", action="auto_resume", details={"code": "selector_error"})
            )
        s.commit()
    _pause(session_factory, PauseCode.SELECTOR_ERROR, "x", at=utcnow() - timedelta(minutes=1))
    assert AutonomyService(session_factory, _settings(), FakeExecutor()).run_once().resumed == [
        "selector_error"
    ]


def test_a_failing_check_is_reported_not_raised(session_factory: sessionmaker[Session]) -> None:
    class Exploding(FakeExecutor):
        def get_balance(self) -> Decimal | None:
            raise RuntimeError("boom")

        def health_check(self) -> ExecutorHealth:
            raise RuntimeError("boom")

    _pause(session_factory, PauseCode.SELECTOR_ERROR, "x")
    report = AutonomyService(session_factory, _settings(), Exploding()).run_once()
    assert report.checks[0].status == "waiting"  # health failure becomes "not ok"
    with session_factory() as s:
        assert db_state.get_state(s, db_state.AUTONOMY)["checks"][0]["code"] == "selector_error"


@pytest.mark.parametrize("code", ["manual", "circuit_open", "other"])
def test_owner_only_codes(session_factory: sessionmaker[Session], code: str) -> None:
    _pause(session_factory, PauseCode(code), "x")
    report = AutonomyService(session_factory, _settings(), FakeExecutor()).run_once()
    assert report.checks[0].status == "owner" and _codes(session_factory) == [code]
