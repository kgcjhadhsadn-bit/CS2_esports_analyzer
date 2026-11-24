"""Telegram: notifications to the owner and owner commands (cloud autonomy, PLAN.md 0b).

Three parts, all talking to the Bot API (``https://api.telegram.org/bot<token>/<method>``) with httpx:

* :class:`TelegramClient` - ``sendMessage`` / ``getUpdates`` / ``getMe``. Rate-limited (one message per
  ``min_send_interval_s``), retried on network errors / 5xx / 429 (``retry_after``), and
  :meth:`TelegramClient.send` **never raises**. The bot token is a secret: it is part of every URL, so
  every error text is redacted before it is logged or returned.
* :class:`TelegramNotifier` - a scheduler job (``telegram_notify``) that looks for new events in the
  database and sends them: placed bets (match, market, selection, odds, stake, p, EV), settlements, every
  pause / resume / auto-resume / circuit-breaker / kill-switch / auto-login change (``audit_log``), and
  failing jobs (rate-limited per job). Reading the database instead of hooking into the betting code
  means a notification can never slow down or break a placement, actions from the CLI or the dashboard
  are reported too, and nothing is lost across a restart (the cursor lives in
  ``system_state["telegram"]``). :meth:`TelegramNotifier.daily_report` is the daily P&L report job.
* :class:`TelegramCommands` - the long-polling command loop (``telegram_commands`` job): ``/status``,
  ``/balance``, ``/bets``, ``/pause``, ``/resume`` (lifts a pause, never the kill switch), ``/stop`` (kill
  switch ON), ``/go`` (kill switch OFF - the owner's explicit action), ``/help``. Only messages from the
  configured ``chat_id`` are obeyed; everything else is ignored and logged. Commands older than
  :data:`MAX_COMMAND_AGE_S` are ignored (no stale ``/go`` replayed after a long outage).

Messages are plain text (no parse mode, so team names never break the markup).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings, TelegramSettings
from cs2bot.db import state as db_state
from cs2bot.db.models import AuditLog, Bet, BetDecisionRow, JobRun, Match
from cs2bot.domain.control import PAUSE_CODE_LABELS
from cs2bot.domain.enums import BetStatus, JobStatus
from cs2bot.utils.time import ensure_utc, to_local, utcnow

log = logging.getLogger(__name__)

TELEGRAM_POLL_STATE = "telegram_poll"
MAX_MESSAGE_CHARS = 4096
MAX_MESSAGES_PER_SCAN = 20
MAX_COMMAND_AGE_S = 600
MAX_RETRY_AFTER_S = 30.0
#: Bet ids remembered per kind (placed / settled) so nothing is announced twice.
REMEMBERED_IDS = 2000
NOTIFY_LOOKBACK = timedelta(days=14)
#: failed job runs remembered (so the overlap of two scans never reports one twice)
REMEMBERED_JOB_RUNS = 500
JOB_SCAN_OVERLAP = timedelta(minutes=2)

PLACED_STATUSES = (
    BetStatus.PLACED,
    BetStatus.WON,
    BetStatus.LOST,
    BetStatus.VOID,
)
SETTLED_STATUSES = (BetStatus.WON, BetStatus.LOST, BetStatus.VOID)


class TelegramError(RuntimeError):
    """A Bot API call failed (message already redacted); ``status_code`` of Telegram's answer, if any."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def truncate_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """``text`` cut to Telegram's limit, counted in UTF-16 code units (an emoji counts twice) to be safe."""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    tail = "\n[...] (truncated)"
    budget = limit - len(tail)
    out: list[str] = []
    used = 0
    for ch in text:
        units = 2 if ord(ch) > 0xFFFF else 1
        if used + units > budget:
            break
        out.append(ch)
        used += units
    return "".join(out) + tail


def redact(text: str, token: str | None) -> str:
    """Remove the bot token (and its URL-encoded form) from ``text``."""
    if not token:
        return text
    return text.replace(token, "<token>").replace(token.replace(":", "%3A"), "<token>")


# ================================================================================================ client


class TelegramClient:
    """Minimal Bot API client (``sendMessage``, ``getUpdates``, ``getMe``) - see the module docstring."""

    def __init__(
        self,
        settings: TelegramSettings,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._token = settings.bot_token.get_secret_value() if settings.bot_token is not None else None
        self._http = http or httpx.Client(timeout=settings.request_timeout_s + settings.poll_timeout_s + 5)
        self._own_http = http is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._send_lock = threading.Lock()
        self._last_send: float | None = None
        self.last_ok_at: datetime | None = None
        self.last_error: str | None = None
        #: The last failed send was refused for its content (HTTP 400 not about the chat): sending the
        #: same text again cannot work, so the notifier skips it instead of retrying it forever.
        self.last_failure_permanent = False
        self.sent = 0
        self.failed = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> TelegramClient | None:
        """A client when Telegram is enabled and fully configured, else ``None``."""
        tg = settings.telegram
        if not (tg.enabled and tg.bot_token is not None and tg.chat_id):
            return None
        return cls(tg)

    @property
    def configured(self) -> bool:
        return bool(self._token and self.settings.chat_id)

    def _url(self, method: str) -> str:
        return f"{self.settings.api_base.rstrip('/')}/bot{self._token}/{method}"

    def _redact(self, text: str) -> str:
        return redact(text, self._token)

    def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout_s: float | None = None,
        attempts: int | None = None,
    ) -> Any:
        """POST one Bot API method; returns ``result``. Retries network errors, 5xx and 429.

        ``attempts`` (default ``max_retries + 1``) bounds the tries. Raises :class:`TelegramError`
        (redacted, ``status_code`` set for an HTTP answer) when it finally fails or Telegram answers
        ``ok: false``.
        """
        if not self._token:
            raise TelegramError("no bot token configured (CS2BOT_TELEGRAM__BOT_TOKEN)")
        attempts = max(1, self.settings.max_retries + 1 if attempts is None else attempts)
        last_status: int | None = None
        last = "unknown error"
        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                response = self._http.post(
                    self._url(method),
                    json=payload,
                    timeout=timeout_s if timeout_s is not None else self.settings.request_timeout_s,
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {self._redact(str(exc))}"[:300]
            except Exception as exc:
                # e.g. httpx.InvalidURL (a bad api_base): not retryable. ``from None`` keeps the original
                # exception - whose text or traceback may carry the URL with the token - out of any log
                detail = self._redact(str(exc))[:200]
                raise TelegramError(f"{method}: {type(exc).__name__}: {detail}") from None
            else:
                body: dict[str, Any] = {}
                try:
                    parsed = response.json()
                    body = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    body = {}
                if response.status_code == 200 and body.get("ok") is True:
                    return body.get("result")
                description = self._redact(str(body.get("description") or response.reason_phrase))[:300]
                last = f"HTTP {response.status_code}: {description}"
                last_status = response.status_code
                if response.status_code == 429:
                    params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
                    try:
                        retry_after = float((params or {}).get("retry_after", 1))
                    except (TypeError, ValueError):
                        retry_after = 1.0
                elif response.status_code < 500:
                    # 400 / 401 / 403: retrying cannot help
                    raise TelegramError(f"{method}: {last}", status_code=response.status_code)
            if attempt + 1 < attempts:
                wait = min(retry_after, MAX_RETRY_AFTER_S) if retry_after is not None else float(2**attempt)
                self._sleep(wait)
        raise TelegramError(f"{method}: {last}", status_code=last_status)

    def send(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        silent: bool = False,
        attempts: int | None = None,
        timeout_s: float | None = None,
    ) -> bool:
        """Send a plain-text message; True on success. Never raises (failures are logged, redacted).

        ``attempts`` / ``timeout_s`` bound a message that must not hold anything up (the shutdown
        message: one try, a short timeout, and it does not wait longer than ``timeout_s`` for another
        message being sent).
        """
        target = chat_id or self.settings.chat_id
        if not self.configured or not target:
            return False
        body = truncate_message(text)
        self.last_failure_permanent = False
        lock_wait = -1.0 if timeout_s is None else max(0.0, timeout_s)
        try:
            if not self._send_lock.acquire(timeout=lock_wait):
                raise TelegramError("another message is still being sent")
            try:
                if self._last_send is not None:
                    gap = self.settings.min_send_interval_s - (self._monotonic() - self._last_send)
                    if gap > 0:
                        self._sleep(gap)
                try:
                    self.call(
                        "sendMessage",
                        {
                            "chat_id": target,
                            "text": body,
                            "disable_web_page_preview": True,
                            "disable_notification": silent,
                        },
                        timeout_s=timeout_s,
                        attempts=attempts,
                    )
                finally:
                    self._last_send = self._monotonic()
            finally:
                self._send_lock.release()
        except Exception as exc:  # TelegramError or anything unexpected: never into the caller
            self.failed += 1
            self.last_error = self._redact(str(exc))[:300]
            self.last_failure_permanent = (
                isinstance(exc, TelegramError)
                and exc.status_code == 400
                and "chat" not in str(exc).lower()  # "chat not found" etc.: a setting, not this text
            )
            log.warning("telegram: could not send a message (%s)", self.last_error)
            return False
        self.sent += 1
        self.last_ok_at = utcnow()
        self.last_error = None
        return True

    def get_updates(self, offset: int | None, *, timeout_s: int | None = None) -> list[dict[str, Any]]:
        """Long-poll ``getUpdates`` (only ``message`` updates). Raises :class:`TelegramError`."""
        wait = self.settings.poll_timeout_s if timeout_s is None else timeout_s
        payload: dict[str, Any] = {"timeout": wait, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload, timeout_s=self.settings.request_timeout_s + wait)
        return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []

    def get_me(self) -> dict[str, Any]:
        result = self.call("getMe", {})
        return result if isinstance(result, dict) else {}

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "sent": self.sent,
            "failed": self.failed,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error": self.last_error,
        }

    def close(self) -> None:
        if self._own_http:
            self._http.close()


# ============================================================================================ formatting


def _money(value: Decimal | None) -> str:
    return "-" if value is None else f"{Decimal(value).quantize(Decimal('0.01'))} PLN"


def _odds(value: Decimal | None) -> str:
    return "-" if value is None else f"{Decimal(value).normalize():f}"


def match_name(session: Session, match_id: int | None) -> str:
    if match_id is None:
        return "unknown match"
    match = session.get(Match, match_id)
    if match is None:
        return f"match {match_id}"
    t1 = match.team1.name if match.team1 is not None else "?"
    t2 = match.team2.name if match.team2 is not None else "?"
    return f"{t1} vs {t2}"


def format_bet_placed(session: Session, bet: Bet) -> str:
    decision = session.get(BetDecisionRow, bet.decision_id) if bet.decision_id is not None else None
    head = "DRY RUN bet (not placed)" if bet.dry_run or bet.status is BetStatus.DRY_RUN else "Bet placed"
    lines = [
        f"{head}: {match_name(session, bet.match_id)}",
        f"{bet.market_raw} - {bet.selection_raw}",
        f"odds {_odds(bet.placed_odds or bet.requested_odds)}, stake {_money(bet.stake)}",
    ]
    if decision is not None:
        lines.append(f"p {decision.p_win:.3f}, EV {decision.ev:+.3f}")
    if bet.external_bet_id:
        lines.append(f"Betclic id {bet.external_bet_id}")
    return "\n".join(lines)


def format_settlement(session: Session, bet: Bet) -> str:
    word = {BetStatus.WON: "WON", BetStatus.LOST: "LOST", BetStatus.VOID: "VOID (refunded)"}.get(
        bet.status, bet.status.value
    )
    return "\n".join(
        [
            f"Bet {word}: {match_name(session, bet.match_id)}",
            f"{bet.market_raw} - {bet.selection_raw} @ {_odds(bet.placed_odds or bet.requested_odds)}",
            f"stake {_money(bet.stake)}, payout {_money(bet.payout)}, P&L {_money(bet.pnl)}",
        ]
    )


def format_audit(row: AuditLog) -> str | None:
    """Owner-facing text for an ``audit_log`` row, or ``None`` when the action is not announced."""
    d = row.details if isinstance(row.details, dict) else {}
    action, actor = row.action, row.actor
    if action == "pause":
        code = str(d.get("code") or "")
        label = PAUSE_CODE_LABELS.get(code, "")
        head = f"PAUSED by {actor} [{code or 'manual'}]" + (f" - {label}" if label else "")
        return f"{head}\n{d.get('reason') or ''}".strip()
    if action == "resume":
        if d.get("auto"):
            return "RESUMED automatically: every pause reason is resolved - betting continues."
        return f"RESUMED by {actor}{': ' + str(d['reason']) if d.get('reason') else ''}"
    if action == "auto_resume":
        tail = " (other pause reasons remain)" if d.get("still_paused") else ""
        return f"Auto-resume: {d.get('label') or d.get('code')} - {d.get('resolved_by') or 'resolved'}{tail}"
    if action == "circuit_open":
        return (
            f"CIRCUIT BREAKER OPEN for '{d.get('code')}': it lifted itself {d.get('resumes')} times in the "
            "window. The bot stays paused - check it, then send /resume."
        )
    if action == "kill_switch_on":
        why = f": {d['reason']}" if d.get("reason") else ""
        return f"KILL SWITCH ON by {actor}{why}. Send /go to turn it off."
    if action == "kill_switch_off":
        return f"Kill switch OFF by {actor}."
    if action == "auto_login":
        status = str(d.get("status") or "")
        if status in ("logged_in", "already_logged_in"):
            return "Betclic auto-login successful."
        if status == "2fa_required":
            return (
                "Betclic asks for an SMS / 2FA code. The bot stays paused: log in once by hand "
                "(dashboard login button or VNC). It retries the login later."
            )
        if status in ("bad_credentials", "account_locked", "no_credentials") or d.get("blocked"):
            return f"Betclic auto-login STOPPED ({status}): {d.get('message') or ''} - fix it, then /resume."
        return f"Betclic auto-login failed ({status}): {d.get('message') or ''} - retrying later."
    if action == "bet_uncertain":
        return f"Unconfirmed placement (UNCERTAIN), bet {d.get('bet_id')}: {d.get('message') or ''}"
    if action == "reconciliation_mismatch":
        return (
            f"Balance mismatch: Betclic {d.get('observed_balance')} PLN vs ledger "
            f"{d.get('ledger_balance')} PLN (difference {d.get('difference')})."
        )
    if action == "balance_adjustment_accepted":
        return "Balance difference booked as a reconciliation entry; the balance pause can lift."
    if action in ("placement_conflict", "stale_pending_cancelled", "bet_warning"):
        return f"{action.replace('_', ' ')}: {d}"[:600]
    return None


# ============================================================================================= notifier


@dataclass
class _Cursor:
    audit_id: int
    since: datetime
    bet_floor: int  # bets with an id at or below this existed before the first scan (never announced)
    placed_ids: list[int]
    settled_ids: list[int]
    job_check_at: datetime
    job_alerts: dict[str, str]
    #: failed job runs already handled (alerted or muted by the cooldown), newest last
    job_run_ids: list[int] = field(default_factory=list)

    @classmethod
    def load(cls, raw: dict[str, Any], session: Session, now: datetime) -> _Cursor:
        def when(key: str) -> datetime:
            value = raw.get(key)
            try:
                return ensure_utc(datetime.fromisoformat(value)) if isinstance(value, str) else now
            except ValueError:
                return now

        if "audit_id" not in raw:  # first run: start from now, never replay history
            max_id = session.scalar(select(func.max(AuditLog.id))) or 0
            max_bet = session.scalar(select(func.max(Bet.id))) or 0
            return cls(int(max_id), now, int(max_bet), [], [], now, {})
        return cls(
            audit_id=int(raw.get("audit_id") or 0),
            since=when("since"),
            bet_floor=int(raw.get("bet_floor") or 0),
            placed_ids=[int(i) for i in raw.get("placed_ids", []) if isinstance(i, int)],
            settled_ids=[int(i) for i in raw.get("settled_ids", []) if isinstance(i, int)],
            job_check_at=when("job_check_at"),
            job_alerts={str(k): str(v) for k, v in (raw.get("job_alerts") or {}).items()},
            job_run_ids=[int(i) for i in raw.get("job_run_ids", []) if isinstance(i, int)],
        )

    def dump(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "since": self.since.isoformat(),
            "bet_floor": self.bet_floor,
            "placed_ids": self.placed_ids[-REMEMBERED_IDS:],
            "settled_ids": self.settled_ids[-REMEMBERED_IDS:],
            "job_check_at": self.job_check_at.isoformat(),
            "job_alerts": self.job_alerts,
            "job_run_ids": self.job_run_ids[-REMEMBERED_JOB_RUNS:],
        }


class TelegramNotifier:
    """Sends new database events to the owner (see the module docstring). Never raises from a job."""

    def __init__(
        self, session_factory: sessionmaker[Session], settings: Settings, client: TelegramClient
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client

    @property
    def tg(self) -> TelegramSettings:
        return self.settings.telegram

    def scan_once(self, now: datetime | None = None) -> int:
        """Send every new event once; returns the number of messages sent."""
        when = ensure_utc(now) if now is not None else utcnow()
        sent = 0
        with self.session_factory() as session:
            raw = db_state.get_state(session, db_state.TELEGRAM)
            cursor = _Cursor.load(
                raw.get("cursor", {}) if isinstance(raw.get("cursor"), dict) else {}, session, when
            )
            budget = MAX_MESSAGES_PER_SCAN
            ok = True
            if ok and self.tg.notify_control:
                ok, n = self._audit_events(session, cursor, budget)
                sent, budget = sent + n, budget - n
            if ok and self.tg.notify_bets:
                ok, n = self._placed_bets(session, cursor, when, budget)
                sent, budget = sent + n, budget - n
            if ok and self.tg.notify_settlements:
                ok, n = self._settled_bets(session, cursor, when, budget)
                sent, budget = sent + n, budget - n
            if ok and self.tg.notify_errors:
                ok, n = self._job_failures(session, cursor, when, budget)
                sent += n
            db_state.set_state(
                session,
                db_state.TELEGRAM,
                {
                    **raw,
                    "cursor": cursor.dump(),
                    "client": self.client.status(),
                    "scanned_at": when.isoformat(),
                },
                actor="telegram",
            )
            session.commit()
        return sent

    def _send(self, text: str) -> str:
        """``"sent"``, ``"skipped"`` (Telegram refused this text for good: moving on, so one bad message
        never blocks every later one) or ``"failed"`` (stop the scan; retried on the next one)."""
        if self.client.send(text):
            return "sent"
        if getattr(self.client, "last_failure_permanent", False):
            log.warning("telegram: a notification was refused for its content and is skipped")
            return "skipped"
        return "failed"

    def _audit_events(self, session: Session, cursor: _Cursor, budget: int) -> tuple[bool, int]:
        rows = session.scalars(
            select(AuditLog).where(AuditLog.id > cursor.audit_id).order_by(AuditLog.id).limit(500)
        ).all()
        sent = 0
        for row in rows:
            text = format_audit(row)
            if text is not None:
                if sent >= budget:
                    return True, sent  # the rest goes out with the next scan
                outcome = self._send(text)
                if outcome == "failed":
                    return False, sent
                sent += outcome == "sent"
            cursor.audit_id = row.id
        return True, sent

    def _placed_bets(self, session: Session, cursor: _Cursor, now: datetime, budget: int) -> tuple[bool, int]:
        statuses = list(PLACED_STATUSES) + ([BetStatus.DRY_RUN] if self.tg.notify_dry_run_bets else [])
        # ids, not timestamps: ``created_at`` is a database default (clock / precision of the server)
        known = set(cursor.placed_ids)
        bets = session.scalars(
            select(Bet)
            .where(
                Bet.id > cursor.bet_floor,
                Bet.created_at >= now - NOTIFY_LOOKBACK,
                Bet.status.in_(statuses),
            )
            .order_by(Bet.id)
        ).all()
        sent = 0
        for bet in bets:
            if bet.id in known:
                continue
            if bet.status is not BetStatus.DRY_RUN and bet.dry_run and not self.tg.notify_dry_run_bets:
                continue
            if sent >= budget:
                return True, sent
            outcome = self._send(format_bet_placed(session, bet))
            if outcome == "failed":
                return False, sent
            cursor.placed_ids.append(bet.id)
            sent += outcome == "sent"
        return True, sent

    def _settled_bets(
        self, session: Session, cursor: _Cursor, now: datetime, budget: int
    ) -> tuple[bool, int]:
        since = max(cursor.since, now - NOTIFY_LOOKBACK)
        known = set(cursor.settled_ids)
        bets = session.scalars(
            select(Bet)
            .where(Bet.settled_at >= since, Bet.status.in_(SETTLED_STATUSES), Bet.dry_run.is_(False))
            .order_by(Bet.id)
        ).all()
        sent = 0
        for bet in bets:
            if bet.id in known:
                continue
            if sent >= budget:
                return True, sent
            outcome = self._send(format_settlement(session, bet))
            if outcome == "failed":
                return False, sent
            cursor.settled_ids.append(bet.id)
            sent += outcome == "sent"
        return True, sent

    def _job_failures(
        self, session: Session, cursor: _Cursor, now: datetime, budget: int
    ) -> tuple[bool, int]:
        # by the time a run FAILED (``finished_at``), not when it started: a long job that was still
        # running at the previous scan must still be reported. A small overlap covers rows committed a
        # moment after their ``finished_at``; ``job_run_ids`` keeps a run from being reported twice.
        rows = session.scalars(
            select(JobRun)
            .where(
                JobRun.status == JobStatus.FAILED,
                JobRun.finished_at >= cursor.job_check_at - JOB_SCAN_OVERLAP,
            )
            .order_by(JobRun.id)
        ).all()
        cooldown = timedelta(minutes=self.tg.error_alert_cooldown_min)
        seen = set(cursor.job_run_ids)
        sent = 0
        for run in rows:
            if run.id in seen:
                continue
            last_raw = cursor.job_alerts.get(run.job_name)
            try:
                last = ensure_utc(datetime.fromisoformat(last_raw)) if last_raw else None
            except ValueError:
                last = None
            failed_at = ensure_utc(run.finished_at or run.started_at)
            if last is not None and failed_at - last < cooldown:
                cursor.job_run_ids.append(run.id)
                continue
            if sent >= budget:
                return True, sent
            text = f"Job {run.job_name} failed: {(run.error or 'no details')[:400]}"
            outcome = self._send(text)
            if outcome == "failed":
                return False, sent
            cursor.job_alerts[run.job_name] = failed_at.isoformat()
            cursor.job_run_ids.append(run.id)
            sent += outcome == "sent"
        cursor.job_check_at = now
        return True, sent

    # ------------------------------------------------------------------ daily report

    def daily_report(self, now: datetime | None = None) -> bool:
        """Send the daily P&L report (the scheduler runs it at ``telegram.daily_report_time``)."""
        when = ensure_utc(now) if now is not None else utcnow()
        with self.session_factory() as session:
            text = daily_report_text(session, self.settings, when)
            session.rollback()  # compute_state may initialise the ledger; a report never writes
        return self.client.send(text)


def daily_report_text(session: Session, settings: Settings, now: datetime) -> str:
    """Daily report: realized P&L today / this week, bets placed and settled today, balance, pauses."""
    from cs2bot.engine import bankroll
    from cs2bot.services.autonomy import pause_overview

    st = bankroll.compute_state(session, settings, now=now)
    day_start = st.day_start or now
    placed = session.scalar(
        select(func.count(Bet.id)).where(
            Bet.placed_at >= day_start, Bet.status.in_(list(PLACED_STATUSES)), Bet.dry_run.is_(False)
        )
    )
    dry = session.scalar(
        select(func.count(Bet.id)).where(Bet.created_at >= day_start, Bet.status == BetStatus.DRY_RUN)
    )
    settled = session.execute(
        select(Bet.status, func.count(Bet.id))
        .where(Bet.settled_at >= day_start, Bet.status.in_(list(SETTLED_STATUSES)), Bet.dry_run.is_(False))
        .group_by(Bet.status)
    ).all()
    counts = {status.value: n for status, n in settled}
    local = to_local(now, settings.timezone)
    overview = pause_overview(session, settings, now=now)
    lines = [
        f"Daily report {local:%Y-%m-%d} ({'DRY RUN' if settings.executor.dry_run else 'LIVE'})",
        f"P&L today {_money(st.realized_pnl_today)}, this week {_money(st.realized_pnl_week)}",
        f"Bets placed today {placed or 0}" + (f" (+{dry} dry-run)" if dry else ""),
        f"Settled today: won {counts.get('won', 0)}, lost {counts.get('lost', 0)}, "
        f"void {counts.get('void', 0)}",
        f"Balance {_money(st.balance)}, open stakes {_money(st.open_stakes_week)}",
    ]
    if st.kill_switch:
        lines.append("Kill switch: ON")
    if overview["paused"]:
        lines.append("Paused: " + "; ".join(r["code"] for r in overview["reasons"]))
    return "\n".join(lines)


# ============================================================================================= commands

HELP_TEXT = (
    "cs2bot commands:\n"
    "/status - pause reasons, kill switch, mode, today's P&L\n"
    "/balance - ledger and Betclic balance\n"
    "/bets - the last 10 bets\n"
    "/pause [reason] - pause betting\n"
    "/resume - lift every pause (never the kill switch)\n"
    "/stop - kill switch ON (stops all betting)\n"
    "/go - kill switch OFF (your explicit decision)\n"
    "/help - this text"
)


def status_text(session: Session, settings: Settings, now: datetime) -> str:
    from cs2bot.engine import bankroll, ledger
    from cs2bot.services.autonomy import pause_overview

    overview = pause_overview(session, settings, now=now)
    kill = db_state.is_kill_switch_on(session)
    lines = [f"Mode: {'DRY RUN' if settings.executor.dry_run else 'LIVE (real money)'}"]
    lines.append(f"Kill switch: {'ON' if kill else 'off'}")
    if overview["paused"]:
        lines.append("Paused:")
        for r in overview["reasons"]:
            eta = (
                f", lifts ~{to_local(datetime.fromisoformat(r['eta']), settings.timezone):%d.%m %H:%M}"
                if r["eta"]
                else ""
            )
            how = "auto" if r["auto_resume"] else "owner"
            lines.append(f"- {r['code']} ({how}{eta}): {r['message'][:200]}")
        if overview["circuit_open"]:
            lines.append("Circuit breaker OPEN - /resume after checking.")
    else:
        lines.append("Not paused.")
    try:
        st = bankroll.compute_state(session, settings, now=now)
        lines.append(f"P&L today {_money(st.realized_pnl_today)}, week {_money(st.realized_pnl_week)}")
        lines.append(f"Bets today {st.bets_placed_today}, open stakes {_money(st.open_stakes_week)}")
    except Exception as exc:  # an empty / broken ledger must not break /status
        lines.append(f"(bankroll unavailable: {type(exc).__name__})")
        lines.append(f"Ledger balance {_money(ledger.current_balance(session))}")
    health = db_state.get_state(session, db_state.EXECUTOR_HEALTH)
    if health:
        lines.append(
            f"Executor: {'ok' if health.get('ok') else 'NOT ok'} ({str(health.get('message') or '')[:120]}), "
            f"logged in: {health.get('logged_in')}"
        )
    return "\n".join(lines)


def balance_text(session: Session, settings: Settings) -> str:
    from cs2bot.engine import ledger

    lines = [f"Ledger balance: {_money(ledger.current_balance(session))}"]
    health = db_state.get_state(session, db_state.EXECUTOR_HEALTH)
    if health.get("balance") is not None:
        lines.append(
            f"Betclic balance (last health check {health.get('checked_at')}): {health['balance']} PLN"
        )
    rec = db_state.get_state(session, db_state.LAST_RECONCILIATION)
    if rec:
        lines.append(
            f"Last reconciliation {rec.get('at')}: Betclic {rec.get('observed_balance')} vs ledger "
            f"{rec.get('ledger_balance')} (difference {rec.get('difference')})"
        )
    return "\n".join(lines)


def bets_text(session: Session, limit: int = 10) -> str:
    bets = session.scalars(select(Bet).order_by(Bet.id.desc()).limit(limit)).all()
    if not bets:
        return "No bets yet."
    lines = []
    for bet in bets:
        lines.append(
            f"#{bet.id} {bet.status.value}{' (dry)' if bet.dry_run else ''}: "
            f"{match_name(session, bet.match_id)} | "
            f"{bet.market_raw} - {bet.selection_raw} @ {_odds(bet.placed_odds or bet.requested_odds)}, "
            f"{_money(bet.stake)}" + (f", P&L {_money(bet.pnl)}" if bet.pnl is not None else "")
        )
    return "\n".join(lines)


class TelegramCommands:
    """Owner commands over ``getUpdates`` long polling (see the module docstring)."""

    ACTOR = "telegram"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        client: TelegramClient,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client
        self.clock = clock

    def _authorised(self, message: dict[str, Any]) -> bool:
        """Only the configured numeric ``chat_id`` may command the bot.

        Usernames are deliberately not accepted: they can be changed, released and claimed by someone
        else, while a chat id is fixed. (An ``@channel`` chat id still works for notifications, but then
        no command is obeyed.)
        """
        raw_chat = message.get("chat")
        chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
        wanted = str(self.settings.telegram.chat_id or "").strip()
        chat_id = chat.get("id")
        if not wanted or isinstance(chat_id, bool) or not isinstance(chat_id, int):
            return False
        return str(chat_id) == wanted

    def handle_update(self, update: dict[str, Any], now: datetime | None = None) -> str | None:
        """Execute one update's command; returns the reply (None = nothing to answer / ignored)."""
        when = ensure_utc(now) if now is not None else self.clock()
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        if not self._authorised(message):
            raw_chat = message.get("chat")
            chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
            log.warning(
                "telegram: ignored a message from chat %s (only chat_id %s is obeyed)",
                chat.get("id"),
                self.settings.telegram.chat_id,
            )
            return None
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        sent_at = message.get("date")
        if isinstance(sent_at, int | float) and when.timestamp() - float(sent_at) > MAX_COMMAND_AGE_S:
            log.warning(
                "telegram: ignored a stale command %r (sent %.0f s ago)",
                text[:40],
                when.timestamp() - sent_at,
            )
            return None
        head, _, rest = text.partition(" ")
        command = head.split("@", 1)[0].lower()
        arg = rest.strip()
        log.info("telegram: command %s", command)
        with self.session_factory() as session:
            try:
                reply = self._run(session, command, arg, when)
                session.commit()
            except Exception as exc:
                session.rollback()
                log.exception("telegram: command %s failed", command)
                reply = f"{command} failed: {type(exc).__name__}"
        return reply

    def _run(self, session: Session, command: str, arg: str, now: datetime) -> str:
        if command in ("/start", "/help"):
            return HELP_TEXT
        if command == "/status":
            text = status_text(session, self.settings, now)
            session.rollback()  # compute_state may initialise the ledger; reading never writes
            return text
        if command == "/balance":
            return balance_text(session, self.settings)
        if command == "/bets":
            return bets_text(session)
        if command == "/pause":
            db_state.set_paused(session, True, actor=self.ACTOR, reason=arg[:300] or "paused from Telegram")
            return "Paused (manual). Send /resume to continue."
        if command == "/resume":
            db_state.set_paused(session, False, actor=self.ACTOR, reason=arg[:300] or "resumed from Telegram")
            if db_state.is_kill_switch_on(session):
                return "Pause lifted, but the KILL SWITCH is still ON - send /go to allow betting."
            return "Resumed: every pause reason is cleared."
        if command == "/stop":
            db_state.set_kill_switch(
                session, True, actor=self.ACTOR, reason=arg[:300] or "/stop from Telegram"
            )
            return "KILL SWITCH ON: no bets until you send /go."
        if command == "/go":
            db_state.set_kill_switch(
                session, False, actor=self.ACTOR, reason=arg[:300] or "/go from Telegram"
            )
            paused, _ = db_state.get_pause(session)
            return "Kill switch OFF." + (" The bot is still paused (see /status)." if paused else "")
        return f"Unknown command {command}. /help lists the commands."

    # ------------------------------------------------------------------ polling

    def _load_offset(self) -> int | None:
        with self.session_factory() as session:
            value = db_state.get_state(session, TELEGRAM_POLL_STATE).get("offset")
        return int(value) if isinstance(value, int) else None

    def _save_offset(self, offset: int, *, handled: int) -> None:
        with self.session_factory() as session:
            st = db_state.get_state(session, TELEGRAM_POLL_STATE)
            db_state.set_state(
                session,
                TELEGRAM_POLL_STATE,
                {
                    **st,
                    "offset": offset,
                    "polled_at": self.clock().isoformat(),
                    "handled": int(st.get("handled") or 0) + handled,
                },
                actor=self.ACTOR,
            )
            session.commit()

    def poll_once(self) -> int:
        """One ``getUpdates`` round: handle and answer every update, persist the offset. Raises
        :class:`TelegramError` when Telegram cannot be reached."""
        offset = self._load_offset()
        updates = self.client.get_updates(offset)
        if not updates:
            return 0
        handled = 0
        next_offset = offset
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = max(next_offset or 0, update_id + 1)
            reply = self.handle_update(update)
            if reply is not None:
                handled += 1
                self.client.send(reply)
            if next_offset is not None:  # one at a time: a crash never repeats a handled command
                self._save_offset(next_offset, handled=1 if reply is not None else 0)
        return handled

    def poll(
        self, *, should_stop: Callable[[], bool], session_s: float, sleep: Callable[[float], object]
    ) -> int:
        """Poll until ``session_s`` elapsed or ``should_stop()``; network errors back off and retry."""
        deadline = time.monotonic() + session_s
        handled = 0
        failures = 0
        while not should_stop() and time.monotonic() < deadline:
            try:
                handled += self.poll_once()
                failures = 0
            except TelegramError as exc:
                failures += 1
                log.warning("telegram: getUpdates failed (%s)", exc)
                sleep(min(60.0, 5.0 * failures))
        return handled


__all__ = [
    "HELP_TEXT",
    "TelegramClient",
    "TelegramCommands",
    "TelegramError",
    "TelegramNotifier",
    "balance_text",
    "bets_text",
    "daily_report_text",
    "format_audit",
    "redact",
    "status_text",
]
