"""Auto-resume service (cloud autonomy): lift every automatic pause once its cause is resolved.

PLAN.md 0b: the bot runs on a VPS with nobody watching. Every automatic pause records a
machine-readable :class:`~cs2bot.domain.control.PauseCode` (:mod:`cs2bot.db.state`); the scheduler
runs :meth:`AutonomyService.run_once` every ``autonomy.check_interval_min`` minutes, which checks each
active reason:

==========================  ===========================================================================
reason                      resolved when
==========================  ===========================================================================
``loss_limit_daily``        the Warsaw day of the pause is over (next local midnight)
``loss_limit_weekly``       the Warsaw week of the pause is over (next Monday 00:00 local)
``uncertain_bet``           no real bet is left ``PLACING`` (settlement resolved them from Betclic's
                            bet history)
``login_required`` /        the session is logged in again: the owner logged in, or the auto-login
``2fa_required``            (``autonomy.auto_login``) succeeded. A login that stops at an SMS / 2FA step
                            records ``2fa_required`` and is retried only every
                            ``twofa_retry_interval_min``; wrong password / locked account / no
                            credentials / ``max_login_failures`` submitted failures in a row stop the
                            retries until the owner resumes (a restart only clears ``no_credentials``)
``balance_mismatch``        the difference is booked: the Betclic balance is read again and reconciled
                            (any remaining difference becomes a RECONCILIATION entry, "auto-resume
                            everything"); with no readable balance the entry booked when the mismatch
                            was found is accepted. Waits while unconfirmed placements could explain a
                            shortfall
``selector_error`` /        a passing executor health check made after the pause
``executor_unavailable``
==========================  ===========================================================================

``manual``, ``circuit_open`` and ``other`` are never lifted, and the kill switch is not a pause reason
at all: only the owner clears those (dashboard, Telegram ``/resume`` / ``/go``, CLI).

**Circuit breaker:** when a reason has already auto-resumed ``circuit_breaker_limit`` times within
``circuit_breaker_window_h`` hours, it is not lifted again: a ``circuit_open`` reason is added (the bot
stays paused until the owner resumes) and an alert goes out. This stops loops such as a login that
works, expires at once and pauses again.

Every auto-resume is written to ``audit_log`` (``auto_resume``; ``circuit_open``, ``auto_login``,
``balance_adjustment_accepted`` likewise), which is also how the Telegram notifier learns about them.
The last check is stored in ``system_state["autonomy"]`` (per-reason status and ETA for the dashboard).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import AuditLog, Bet
from cs2bot.domain.betting import ExecutorHealth
from cs2bot.domain.control import (
    AUTO_RESUMABLE,
    PAUSE_CODE_LABELS,
    LoginAttempt,
    LoginStatus,
    PauseCode,
    PauseReason,
    circuit_codes,
)
from cs2bot.domain.enums import BetStatus
from cs2bot.engine import ledger
from cs2bot.services.health import HealthCache, refresh_executor_health
from cs2bot.utils.time import ensure_utc, local_day_start, local_week_start, to_local, utcnow

log = logging.getLogger(__name__)

ACTOR = "autonomy"
LOGIN_CODES = frozenset({PauseCode.LOGIN_REQUIRED, PauseCode.TWO_FA_REQUIRED})
HEALTH_CODES = frozenset({PauseCode.SELECTOR_ERROR, PauseCode.EXECUTOR_UNAVAILABLE})
#: Longest wait between two failed auto-login attempts (the interval doubles per failure).
MAX_LOGIN_BACKOFF = timedelta(hours=24)


# ---------------------------------------------------------------------------------------- time helpers


def next_local_midnight(now: datetime, tz: str | ZoneInfo) -> datetime:
    """UTC instant of the next local midnight after ``now`` (DST-safe)."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = to_local(now, zone)
    tomorrow = (local.replace(hour=12) + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone).astimezone(UTC)


def next_local_monday(now: datetime, tz: str | ZoneInfo) -> datetime:
    """UTC instant of the next local Monday 00:00 after ``now``."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = to_local(now, zone)
    days = 7 - local.weekday()
    monday = (local.replace(hour=12) + timedelta(days=days)).date()
    return datetime(monday.year, monday.month, monday.day, tzinfo=zone).astimezone(UTC)


def _period_start(reason: PauseReason) -> datetime | None:
    raw = reason.details.get("period")
    if isinstance(raw, str):
        try:
            return ensure_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------------------------- reports


@dataclass
class ReasonCheck:
    """Outcome of checking one active pause reason."""

    code: str
    status: str  # "resumed" | "waiting" | "circuit_open" | "owner" (never automatic) | "disabled"
    detail: str = ""
    eta: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "detail": self.detail,
            "eta": self.eta.isoformat() if self.eta else None,
        }


@dataclass
class AutonomyReport:
    checked_at: datetime
    checks: list[ReasonCheck] = field(default_factory=list)
    login: LoginAttempt | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def resumed(self) -> list[str]:
        return [c.code for c in self.checks if c.status == "resumed"]

    @property
    def circuit_opened(self) -> list[str]:
        return [c.code for c in self.checks if c.status == "circuit_open"]

    @property
    def items(self) -> int:
        return len(self.resumed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "checks": [c.as_dict() for c in self.checks],
            "resumed": self.resumed,
            "circuit_opened": self.circuit_opened,
            "login": self.login.model_dump(mode="json") if self.login else None,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------------------------- service


def auto_resume_count(session: Session, code: str, *, since: datetime) -> int:
    """How often ``code`` was auto-resumed since ``since`` (``audit_log`` ``auto_resume`` rows)."""
    rows = session.scalars(
        select(AuditLog.details).where(AuditLog.action == "auto_resume", AuditLog.ts >= since)
    ).all()
    return sum(1 for details in rows if isinstance(details, dict) and details.get("code") == code)


def clear_login_block(session: Session, *, actor: str, startup: bool = False) -> bool:
    """Allow automatic logins again after they were stopped; returns True when a block was lifted.

    The owner's resume (``db.state.set_paused(False)``) always clears the block. At start-up
    (``startup=True``) only a block that never reached Betclic is cleared (``no_credentials``: the owner
    has probably just added them to ``.env``); a wrong password, a locked account or too many failed
    attempts stay blocked across restarts, so a crash-looping container can never submit a wrong
    password again and again (the Telegram alert asks for ``/resume`` after fixing it).
    """
    st = db_state.get_state(session, db_state.AUTO_LOGIN)
    if not st.get("blocked"):
        return False
    if startup and st.get("status") != LoginStatus.NO_CREDENTIALS.value:
        log.warning(
            "auto-login stays stopped after '%s': fix the account / credentials, then resume (Telegram "
            "/resume, dashboard or `cs2bot control resume`)",
            st.get("status"),
        )
        return False
    db_state.set_state(
        session,
        db_state.AUTO_LOGIN,
        {**st, "blocked": False, "failures": 0, "submit_failures": 0},
        actor=actor,
    )
    return True


def last_owner_resume(session: Session, *, since: datetime) -> datetime | None:
    """When the owner last resumed (dashboard / Telegram / CLI ``resume`` audit, not an automatic one)."""
    rows = session.execute(
        select(AuditLog.ts, AuditLog.details)
        .where(AuditLog.action == "resume", AuditLog.ts >= since)
        .order_by(AuditLog.id.desc())
    ).all()
    for ts, details in rows:
        if not (isinstance(details, dict) and details.get("auto")):
            return ensure_utc(ts)
    return None


class AutonomyService:
    """Checks the active pause reasons and lifts the resolved ones (see the module docstring).

    ``executor`` is the shared (thread-bound) Betclic executor or ``None``. All executor calls are
    serialised by its worker thread; the service itself runs in the scheduler's ``fast`` lane, so it
    never races a betting cycle.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        executor: Any | None = None,
        *,
        health_cache: HealthCache | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.executor = executor
        self.health_cache = health_cache
        self.clock = clock

    @property
    def cfg(self) -> Any:
        return self.settings.autonomy

    # ------------------------------------------------------------------ main entry

    def run_once(self, now: datetime | None = None) -> AutonomyReport:
        """Check every active reason once; never raises (errors are reported)."""
        when = ensure_utc(now) if now is not None else self.clock()
        report = AutonomyReport(checked_at=when)
        try:
            with self.session_factory() as session:
                reasons = db_state.get_pause_reasons(session)
        except Exception as exc:
            log.exception("autonomy: could not read the pause state")
            report.errors.append(f"state: {type(exc).__name__}: {exc}"[:300])
            return report
        allowed = {str(c) for c in self.cfg.auto_resume_reasons}
        open_codes = circuit_codes(reasons)
        login_check: ReasonCheck | None = None
        for reason in reasons:
            code = reason.code
            if code not in AUTO_RESUMABLE:
                report.checks.append(ReasonCheck(code.value, "owner", "only the owner can resume this"))
                continue
            if not self.cfg.auto_resume or code.value not in allowed:
                report.checks.append(
                    ReasonCheck(code.value, "disabled", "auto-resume is off for this reason")
                )
                continue
            if code.value in open_codes:
                report.checks.append(
                    ReasonCheck(code.value, "circuit_open", "circuit breaker open - waiting for the owner")
                )
                continue
            try:
                if code in LOGIN_CODES:
                    if login_check is None:  # one login check covers login_required and 2fa_required
                        login_check = self._check_login(reason, when, report)
                    check = ReasonCheck(code.value, login_check.status, login_check.detail, login_check.eta)
                else:
                    check = self._check(reason, when)
            except Exception as exc:
                log.exception("autonomy: checking %s failed", code.value)
                report.errors.append(f"{code.value}: {type(exc).__name__}: {exc}"[:300])
                check = ReasonCheck(code.value, "waiting", f"check failed: {type(exc).__name__}")
            if check.status == "resumed":
                check = self._resume(reason, check, when)
            report.checks.append(check)
        self._store(report)
        return report

    # ------------------------------------------------------------------ per-reason checks

    def _check(self, reason: PauseReason, now: datetime) -> ReasonCheck:
        code = reason.code
        tz = self.settings.timezone
        if code is PauseCode.LOSS_LIMIT_DAILY:
            period = _period_start(reason) or local_day_start(reason.since, tz)
            if local_day_start(now, tz) > period:
                return ReasonCheck(code.value, "resumed", "a new Warsaw day has started")
            return ReasonCheck(code.value, "waiting", "daily loss limit", next_local_midnight(now, tz))
        if code is PauseCode.LOSS_LIMIT_WEEKLY:
            period = _period_start(reason) or local_week_start(reason.since, tz)
            if local_week_start(now, tz) > period:
                return ReasonCheck(code.value, "resumed", "a new Warsaw week has started")
            return ReasonCheck(code.value, "waiting", "weekly loss limit", next_local_monday(now, tz))
        if code is PauseCode.UNCERTAIN_BET:
            return self._check_uncertain(now)
        if code is PauseCode.BALANCE_MISMATCH:
            return self._check_balance(reason, now)
        if code in HEALTH_CODES:
            return self._check_health(reason)
        return ReasonCheck(code.value, "owner", "no automatic check for this reason")

    def _check_uncertain(self, now: datetime) -> ReasonCheck:
        with self.session_factory() as session:
            open_unconfirmed = (
                session.scalar(
                    select(func.count(Bet.id)).where(Bet.status == BetStatus.PLACING, Bet.dry_run.is_(False))
                )
                or 0
            )
        if open_unconfirmed:
            return ReasonCheck(
                PauseCode.UNCERTAIN_BET.value,
                "waiting",
                f"{open_unconfirmed} unconfirmed placement(s): settlement reconciles them against the "
                "Betclic bet history",
            )
        return ReasonCheck(
            PauseCode.UNCERTAIN_BET.value, "resumed", "every placement is confirmed or resolved"
        )

    def _check_balance(self, reason: PauseReason, now: datetime) -> ReasonCheck:
        code = PauseCode.BALANCE_MISMATCH.value
        observed: Decimal | None = None
        if self.cfg.balance_recheck and self.executor is not None:
            try:
                observed = self.executor.get_balance()
            except Exception as exc:
                log.warning("autonomy: could not read the Betclic balance (%s)", type(exc).__name__)
        with self.session_factory() as session:
            if observed is not None:
                result = ledger.reconcile(
                    session,
                    observed,
                    tolerance=self.settings.executor.reconcile_tolerance,
                    actor=ACTOR,
                    at=now,
                    pause_on_flag=False,
                )
                if result.deferred:
                    session.commit()
                    return ReasonCheck(
                        code,
                        "waiting",
                        "unconfirmed placements may explain the difference - waiting for settlement",
                    )
                details = {
                    "observed_balance": str(result.observed_balance),
                    "difference_now": str(result.difference),
                    "entry_id": result.entry_id,
                    "original": reason.details,
                }
                how = f"Betclic balance {result.observed_balance} PLN reconciled"
            else:
                details = {"original": reason.details, "balance_read": False}
                how = "the adjustment booked when the mismatch was found is accepted"
            db_state.audit(session, actor=ACTOR, action="balance_adjustment_accepted", details=details)
            session.commit()
        return ReasonCheck(code, "resumed", how)

    def _check_health(self, reason: PauseReason) -> ReasonCheck:
        code = reason.code.value
        if self.executor is None:
            return ReasonCheck(code, "waiting", "no executor in this process")
        health: ExecutorHealth = refresh_executor_health(
            self.session_factory, self.executor, self.health_cache
        )
        if health.ok and ensure_utc(health.checked_at) >= reason.since:
            return ReasonCheck(code, "resumed", "executor health check passed")
        return ReasonCheck(code, "waiting", f"executor health check: {health.message or 'not ok'}"[:300])

    def _check_login(self, reason: PauseReason, now: datetime, report: AutonomyReport) -> ReasonCheck:
        code = reason.code.value
        if self.executor is None:
            return ReasonCheck(code, "waiting", "no executor in this process")
        try:
            logged_in = bool(self.executor.is_logged_in())
        except Exception as exc:
            log.warning("autonomy: login state check failed (%s)", type(exc).__name__)
            logged_in = False
        if logged_in:
            try:
                self._forget_login_backoff(now)
            except Exception:  # bookkeeping only: never keeps a resolved pause in place
                log.warning("autonomy: could not reset the auto-login back-off", exc_info=True)
            return ReasonCheck(code, "resumed", "the Betclic session is logged in")
        if not (self.cfg.auto_login and hasattr(self.executor, "auto_login")):
            return ReasonCheck(
                code, "waiting", "auto-login is off: log in from the dashboard or `cs2bot executor login`"
            )
        with self.session_factory() as session:
            memo = db_state.get_state(session, db_state.AUTO_LOGIN)
        if memo.get("blocked"):
            return ReasonCheck(
                code,
                "waiting",
                f"auto-login stopped after '{memo.get('status')}': fix the credentials / account, then "
                "resume (Telegram /resume, dashboard or `cs2bot control resume`)",
            )
        due = self._next_login_at(memo)
        if due is not None and due > now:
            return ReasonCheck(code, "waiting", f"next auto-login attempt after {memo.get('status')}", due)
        try:
            attempt: LoginAttempt = self.executor.auto_login()
        except Exception as exc:
            # first line only: a browser error's call log could echo what was typed into the form
            first = (str(exc).strip().splitlines() or [""])[0]
            attempt = LoginAttempt(
                status=LoginStatus.FAILED,
                message=f"{type(exc).__name__}: {first}"[:300],
                at=utcnow(),
                submitted=True,  # unknown: count it, so repeated crashes cannot hammer the login form
            )
        report.login = attempt
        status = attempt.status
        failures = 0 if status.logged_in else int(memo.get("failures") or 0) + 1
        # submitted attempts that neither logged in nor reached the SMS step (the SMS step proves the
        # password was right): bounded by autonomy.max_login_failures, then stop until the owner acts
        if status.logged_in or status is LoginStatus.TWO_FA_REQUIRED:
            submit_failures = 0
        else:
            submit_failures = int(memo.get("submit_failures") or 0) + (1 if attempt.submitted else 0)
        too_many = submit_failures >= self.cfg.max_login_failures
        blocked = status.stops_retrying or too_many
        message = attempt.message[:300]
        if too_many and not status.stops_retrying:
            message = (
                f"{message} - {submit_failures} submitted logins failed in a row: auto-login stopped "
                "(an unrecognised wrong-password message?)"
            )[:400]
        new_memo = {
            "status": status.value,
            "message": message,
            "at": now.isoformat(),
            "failures": failures,
            "submit_failures": submit_failures,
            "blocked": blocked,
        }
        with self.session_factory() as session:
            # lock order: ``paused`` first, then ``auto_login`` (the owner's resume does the same)
            db_state.get_pause_reasons(session, lock=True)
            db_state.set_state(session, db_state.AUTO_LOGIN, new_memo, actor=ACTOR)
            db_state.audit(
                session,
                actor=ACTOR,
                action="auto_login",
                details={"status": status.value, "message": message, "blocked": blocked},
            )
            if attempt.status is LoginStatus.TWO_FA_REQUIRED:
                db_state.add_pause_reason(
                    session,
                    PauseCode.TWO_FA_REQUIRED,
                    "Betclic asks for an SMS / 2FA code: log in once by hand (dashboard login button or "
                    "VNC); the bot retries the login every "
                    f"{self.cfg.twofa_retry_interval_min:.0f} min",
                    actor=ACTOR,
                )
            session.commit()
        if attempt.status.logged_in:
            return ReasonCheck(code, "resumed", f"auto-login: {attempt.status.value}")
        return ReasonCheck(
            code,
            "waiting",
            f"auto-login: {attempt.status.value} ({message})"[:300],
            None if blocked else self._next_login_at(new_memo),
        )

    def _forget_login_backoff(self, now: datetime) -> None:
        """The session is logged in (the owner logged in by hand, e.g. after an SMS step): the next
        logout may try an auto-login at once instead of waiting out an old back-off / 2FA interval.
        A stopped auto-login (wrong password ...) stays stopped - only the owner's resume clears it."""
        with self.session_factory() as session:
            memo = db_state.get_state(session, db_state.AUTO_LOGIN)
            if (
                not memo
                or memo.get("blocked")
                or memo.get("status")
                in (
                    LoginStatus.LOGGED_IN.value,
                    LoginStatus.ALREADY_LOGGED_IN.value,
                )
            ):
                return
            db_state.get_pause_reasons(session, lock=True)  # lock order: paused, then auto_login
            db_state.set_state(
                session,
                db_state.AUTO_LOGIN,
                {
                    **memo,
                    "status": LoginStatus.ALREADY_LOGGED_IN.value,
                    "message": "the session is logged in",
                    "at": now.isoformat(),
                    "failures": 0,
                    "submit_failures": 0,
                },
                actor=ACTOR,
            )
            session.commit()

    def _next_login_at(self, memo: dict[str, Any]) -> datetime | None:
        raw = memo.get("at")
        if not isinstance(raw, str) or memo.get("status") in (
            LoginStatus.LOGGED_IN.value,
            LoginStatus.ALREADY_LOGGED_IN.value,
        ):
            return None
        try:
            last = ensure_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None
        if memo.get("status") == LoginStatus.TWO_FA_REQUIRED.value:
            return last + timedelta(minutes=self.cfg.twofa_retry_interval_min)
        failures = max(1, int(memo.get("failures") or 1))
        wait = timedelta(minutes=self.cfg.login_retry_interval_min) * (2 ** min(failures - 1, 10))
        return last + min(wait, MAX_LOGIN_BACKOFF)

    # ------------------------------------------------------------------ resume + circuit breaker

    def _resume(self, reason: PauseReason, check: ReasonCheck, now: datetime) -> ReasonCheck:
        code = reason.code.value
        window = timedelta(hours=self.cfg.circuit_breaker_window_h)
        with self.session_factory() as session:
            # lock the pause state for the whole decision (see db.state._pause_row)
            current = db_state.get_pause_reasons(session, lock=True)
            if db_state.is_kill_switch_on(session):
                log.info("autonomy: %s resolved (%s); the kill switch stays on", code, check.detail)
            # the window restarts when the owner resumes: they looked at it and chose to continue
            since = now - window
            owner_at = last_owner_resume(session, since=since)
            if owner_at is not None and owner_at > since:
                since = owner_at
            resumed_before = auto_resume_count(session, code, since=since)
            if resumed_before >= self.cfg.circuit_breaker_limit:
                message = (
                    f"circuit breaker: '{code}' already lifted itself {resumed_before} times in the last "
                    f"{self.cfg.circuit_breaker_window_h:g} h - staying paused until the owner resumes"
                )
                # every code the breaker holds is kept (a single "code" would be overwritten when a
                # second code trips, and the first would then be checked - and alerted - again and again)
                held = sorted(circuit_codes(current) | {code})
                db_state.add_pause_reason(
                    session,
                    PauseCode.CIRCUIT_OPEN,
                    message,
                    actor=ACTOR,
                    details={"code": code, "codes": held, "resumes": resumed_before},
                )
                db_state.audit(
                    session,
                    actor=ACTOR,
                    action="circuit_open",
                    details={"code": code, "resumes": resumed_before, "message": message},
                )
                session.commit()
                log.error("autonomy: %s", message)
                return ReasonCheck(code, "circuit_open", message)
            removed = db_state.resolve_pause_reasons(session, [code], actor=ACTOR, note=check.detail)
            if not removed:  # someone else lifted it meanwhile (owner resume)
                session.commit()
                return ReasonCheck(code, "resumed", f"{check.detail} (already lifted)")
            still_paused = bool(db_state.get_pause_reasons(session))
            db_state.audit(
                session,
                actor=ACTOR,
                action="auto_resume",
                details={
                    "code": code,
                    "label": PAUSE_CODE_LABELS.get(code, code),
                    "message": reason.message[:500],
                    "resolved_by": check.detail[:300],
                    "still_paused": still_paused,
                    "since": reason.since.isoformat(),
                },
            )
            session.commit()
        log.warning("autonomy: lifted the '%s' pause (%s)", code, check.detail)
        return check

    def _store(self, report: AutonomyReport) -> None:
        try:
            with self.session_factory() as session:
                db_state.set_state(session, db_state.AUTONOMY, report.as_dict(), actor=ACTOR)
                session.commit()
        except Exception:
            log.exception("autonomy: could not store the check result")


# --------------------------------------------------------------------------------------- read model


def pause_overview(session: Session, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """Structured pause state for the API / CLI / Telegram: reasons, labels, ETA, circuit breaker.

    ETAs come from the last autonomy check (``system_state["autonomy"]``) and, for loss limits, are
    computed directly (they do not depend on anything else).
    """
    when = now or utcnow()
    tz = settings.timezone
    reasons = db_state.get_pause_reasons(session)
    last = db_state.get_state(session, db_state.AUTONOMY)
    last_checks = {c.get("code"): c for c in last.get("checks", []) if isinstance(c, dict)}
    open_codes = circuit_codes(reasons)
    auto_enabled = settings.autonomy.enabled and settings.autonomy.auto_resume
    allowed = {str(c) for c in settings.autonomy.auto_resume_reasons}
    items: list[dict[str, Any]] = []
    for r in reasons:
        auto = auto_enabled and r.code in AUTO_RESUMABLE and r.code.value in allowed
        auto = auto and r.code.value not in open_codes
        eta: str | None = None
        if auto and r.code is PauseCode.LOSS_LIMIT_DAILY:
            eta = next_local_midnight(when, tz).isoformat()
        elif auto and r.code is PauseCode.LOSS_LIMIT_WEEKLY:
            eta = next_local_monday(when, tz).isoformat()
        elif auto:
            eta = (last_checks.get(r.code.value) or {}).get("eta")
        items.append(
            {
                "code": r.code.value,
                "label": PAUSE_CODE_LABELS.get(r.code.value, r.code.value),
                "message": r.message,
                "since": r.since.isoformat(),
                "actor": r.actor,
                "count": r.count,
                "auto_resume": auto,
                "eta": eta,
                "last_check": (last_checks.get(r.code.value) or {}).get("detail"),
                "details": r.details,
            }
        )
    login = db_state.get_state(session, db_state.AUTO_LOGIN)
    return {
        "paused": bool(reasons),
        "reasons": items,
        "circuit_open": bool(open_codes) or any(r.code is PauseCode.CIRCUIT_OPEN for r in reasons),
        "circuit_codes": sorted(open_codes),
        "autonomy_enabled": settings.autonomy.enabled,
        "auto_login_enabled": settings.autonomy.enabled and settings.autonomy.auto_login,
        "last_check_at": last.get("checked_at"),
        "last_login": login or None,
    }


__all__ = [
    "AutonomyReport",
    "AutonomyService",
    "ReasonCheck",
    "auto_resume_count",
    "clear_login_block",
    "last_owner_resume",
    "next_local_midnight",
    "next_local_monday",
    "pause_overview",
]
