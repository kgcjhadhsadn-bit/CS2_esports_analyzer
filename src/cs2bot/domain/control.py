"""Control-plane contracts: structured pause reasons and auto-login outcomes (cloud autonomy).

A pause is a set of :class:`PauseReason` entries stored in ``system_state["paused"]`` (see
:mod:`cs2bot.db.state`). Every automatic pause carries a machine-readable :class:`PauseCode`, so the
auto-resume service (:mod:`cs2bot.services.autonomy`) can decide per reason whether its cause is
resolved. A pause by a person (dashboard, Telegram, CLI) is ``manual`` and is never lifted
automatically; neither is ``circuit_open`` (the circuit breaker gave up) nor the kill switch, which is
not a pause reason at all.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PauseCode(StrEnum):
    MANUAL = "manual"
    LOSS_LIMIT_DAILY = "loss_limit_daily"
    LOSS_LIMIT_WEEKLY = "loss_limit_weekly"
    UNCERTAIN_BET = "uncertain_bet"
    LOGIN_REQUIRED = "login_required"
    TWO_FA_REQUIRED = "2fa_required"
    BALANCE_MISMATCH = "balance_mismatch"
    SELECTOR_ERROR = "selector_error"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    CIRCUIT_OPEN = "circuit_open"
    #: An automatic pause without a more specific code (or one stored before codes existed).
    OTHER = "other"


#: Codes the auto-resume service may ever lift (subject to ``autonomy.auto_resume_reasons``).
AUTO_RESUMABLE: frozenset[PauseCode] = frozenset(
    {
        PauseCode.LOSS_LIMIT_DAILY,
        PauseCode.LOSS_LIMIT_WEEKLY,
        PauseCode.UNCERTAIN_BET,
        PauseCode.LOGIN_REQUIRED,
        PauseCode.TWO_FA_REQUIRED,
        PauseCode.BALANCE_MISMATCH,
        PauseCode.SELECTOR_ERROR,
        PauseCode.EXECUTOR_UNAVAILABLE,
    }
)

#: Codes that only the owner can lift (a resume from the dashboard / Telegram / CLI).
OWNER_ONLY: frozenset[PauseCode] = frozenset({PauseCode.MANUAL, PauseCode.CIRCUIT_OPEN, PauseCode.OTHER})

#: Short owner-facing description of each code (dashboard / Telegram).
PAUSE_CODE_LABELS: dict[str, str] = {
    PauseCode.MANUAL: "paused by the owner",
    PauseCode.LOSS_LIMIT_DAILY: "daily loss limit reached (lifts at the next Warsaw midnight)",
    PauseCode.LOSS_LIMIT_WEEKLY: "weekly loss limit reached (lifts on Monday 00:00 Warsaw time)",
    PauseCode.UNCERTAIN_BET: "unconfirmed bet placements (lifts after reconciliation with Betclic)",
    PauseCode.LOGIN_REQUIRED: "Betclic login required (lifts after a successful auto-login)",
    PauseCode.TWO_FA_REQUIRED: "Betclic asks for an SMS / 2FA code - needs the owner",
    PauseCode.BALANCE_MISMATCH: "balance mismatch (lifts once the difference is booked)",
    PauseCode.SELECTOR_ERROR: "Betclic page layout changed (lifts after a passing health check)",
    PauseCode.EXECUTOR_UNAVAILABLE: "bet executor unavailable (lifts after a passing health check)",
    PauseCode.CIRCUIT_OPEN: "circuit breaker open: a pause kept coming back - needs the owner",
    PauseCode.OTHER: "paused (needs the owner)",
}


def parse_pause_code(value: object) -> PauseCode:
    """A stored code -> :class:`PauseCode`; unknown values become ``OTHER`` (owner-only, safe)."""
    try:
        return PauseCode(str(value))
    except ValueError:
        return PauseCode.OTHER


class PauseReason(BaseModel):
    """One reason the bot is paused (``system_state["paused"]["reasons"][i]``)."""

    code: PauseCode
    message: str
    since: datetime
    actor: str = "system"
    #: How often this reason was (re)reported while it was active.
    count: int = 1
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def auto_resumable(self) -> bool:
        return self.code in AUTO_RESUMABLE


class LoginStatus(StrEnum):
    """Outcome of an automatic Betclic login (:meth:`BetclicExecutor.auto_login`)."""

    LOGGED_IN = "logged_in"  # the form was submitted and the logged-in indicator appeared
    ALREADY_LOGGED_IN = "already_logged_in"
    TWO_FA_REQUIRED = "2fa_required"  # Betclic asks for an SMS / 2FA code: stop, alert, retry later
    BAD_CREDENTIALS = "bad_credentials"  # wrong username / password: stop retrying, alert
    ACCOUNT_LOCKED = "account_locked"  # account blocked / suspended: stop retrying, alert
    NO_CREDENTIALS = "no_credentials"  # credentials_source none or incomplete
    FORM_NOT_FOUND = "form_not_found"  # the login form / its fields could not be found (selectors)
    FAILED = "failed"  # no recognisable outcome before the timeout, or a browser error

    @property
    def stops_retrying(self) -> bool:
        """Retrying cannot help (and could lock the account): wait for the owner."""
        return self in (LoginStatus.BAD_CREDENTIALS, LoginStatus.ACCOUNT_LOCKED, LoginStatus.NO_CREDENTIALS)

    @property
    def logged_in(self) -> bool:
        return self in (LoginStatus.LOGGED_IN, LoginStatus.ALREADY_LOGGED_IN)


class LoginAttempt(BaseModel):
    status: LoginStatus
    message: str = ""
    at: datetime
