"""Pipeline health for the dashboard: DB, job runs, data freshness, executor, trading state, alerts.

Job names are owned by the scheduler; they are classified by keywords (``odds``, ``upcoming``,
``result``, ``news``, ``predict``, ``settle``, ``demo``, ``ranking``, ``train`` ...) to find the
expected run interval in :class:`cs2bot.config.SchedulerSettings`. A job is *stale* when its last
successful run is older than ``2 x interval + 10 min``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from cs2bot import __version__
from cs2bot.api.bankroll import compute_bankroll
from cs2bot.api.deps import ApiContext
from cs2bot.api.formatting import age_seconds, money_str, utc
from cs2bot.api.schemas import (
    AlertLevel,
    DbHealth,
    ExecutorHealthOut,
    FreshnessOut,
    HealthAlert,
    HealthOut,
    JobHealth,
    SyncJobFreshness,
    TradingStateOut,
)
from cs2bot.api.trading import trading_state
from cs2bot.config import SchedulerSettings
from cs2bot.db import state
from cs2bot.db.models import (
    BetDecisionRow,
    BookmakerEvent,
    JobRun,
    Match,
    MatchAnalysis,
    OddsSnapshot,
    Prediction,
)
from cs2bot.domain.betting import ExecutorHealth
from cs2bot.domain.enums import JobStatus, LinkStatus
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import ensure_utc

log = get_logger(__name__)

# (kind, word prefixes, exact words) - first match wins. Action words come before "upcoming", which
# is usually a modifier ("predict_upcoming" is a prediction job, "ingest_upcoming" a sync job).
_KIND_RULES: tuple[tuple[str, tuple[str, ...], frozenset[str]], ...] = (
    ("odds", ("odds",), frozenset()),
    ("predict", ("predict",), frozenset()),
    ("settle", ("settle",), frozenset()),
    ("bet", (), frozenset({"bet", "bets", "betting"})),
    ("results", ("result",), frozenset()),
    ("news", ("news",), frozenset()),
    ("demos", ("demo",), frozenset()),
    ("rankings", ("ranking",), frozenset()),
    ("retrain", ("retrain", "train"), frozenset()),
    ("backup", ("backup",), frozenset()),
    ("reconcile", ("reconcil",), frozenset()),
    ("upcoming", ("upcoming", "fixture", "schedule"), frozenset()),
)
SYNC_KINDS: tuple[str, ...] = ("odds", "upcoming", "results", "news", "rankings")
_HISTORY = 12
_CREDENTIALS_IN_URL = re.compile(r"(\w+://)[^/\s@]+@")
_LEVEL_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}


def job_kind(name: str) -> str | None:
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    for kind, prefixes, exact in _KIND_RULES:
        if any(w in exact or w.startswith(prefixes) for w in words):  # startswith(()) is False
            return kind
    return None


def expected_interval_s(kind: str | None, sched: SchedulerSettings) -> float | None:
    table: dict[str, float] = {
        "odds": float(sched.odds_poll_s),
        "upcoming": sched.upcoming_sync_min * 60.0,
        "results": sched.results_sync_min * 60.0,
        "news": sched.news_poll_min * 60.0,
        "predict": sched.predict_min * 60.0,
        "bet": sched.predict_min * 60.0,
        "settle": sched.settle_min * 60.0,
        "demos": sched.demo_process_min * 60.0,
        "rankings": 86400.0,
        "retrain": 86400.0,
        "backup": 86400.0,
        "reconcile": 86400.0,
    }
    return table.get(kind) if kind is not None else None


def stale_after_s(interval_s: float) -> float:
    return 2 * interval_s + 600.0


def _fmt_duration(seconds: float) -> str:
    if seconds < 120:
        return f"{int(seconds)} s"
    if seconds < 7200:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _fmt_age(seconds: float | None) -> str:
    return "never" if seconds is None else f"{_fmt_duration(seconds)} ago"


def _sanitize_error(exc: BaseException) -> str:
    """Short, single-line error text without credentials that may sit in a DB URL."""
    first_line = (str(exc).strip().splitlines() or [""])[0]
    cleaned = _CREDENTIALS_IN_URL.sub(r"\1***@", first_line)[:200]
    return f"{type(exc).__name__}: {cleaned}"


class _Alerts:
    def __init__(self) -> None:
        self.items: list[HealthAlert] = []

    def add(self, level: AlertLevel, code: str, message: str) -> None:
        self.items.append(HealthAlert(level=level, code=code, message=message))

    def sorted(self) -> list[HealthAlert]:
        return sorted(self.items, key=lambda a: _LEVEL_ORDER.get(a.level, 9))

    def status(self) -> str:
        levels = {a.level for a in self.items}
        if "critical" in levels:
            return "critical"
        if "warning" in levels:
            return "warning"
        return "ok"


# --------------------------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------------------------


def job_health(session: Session, sched: SchedulerSettings, now: datetime) -> list[JobHealth]:
    names = sorted(session.scalars(select(JobRun.job_name).distinct()))
    out: list[JobHealth] = []
    for name in names:
        runs: Sequence[JobRun] = list(
            session.scalars(
                select(JobRun)
                .where(JobRun.job_name == name)
                .order_by(JobRun.started_at.desc(), JobRun.id.desc())
                .limit(_HISTORY)
            )
        )
        if not runs:
            continue
        last = runs[0]
        success = next((r for r in runs if r.status in (JobStatus.SUCCESS, JobStatus.PARTIAL)), None)
        if success is None:
            success = session.scalar(
                select(JobRun)
                .where(JobRun.job_name == name, JobRun.status.in_([JobStatus.SUCCESS, JobStatus.PARTIAL]))
                .order_by(JobRun.started_at.desc(), JobRun.id.desc())
                .limit(1)
            )
        failures = 0
        for r in runs:
            if r.status is JobStatus.RUNNING and failures == 0 and r is last:
                continue
            if r.status is JobStatus.FAILED:
                failures += 1
            else:
                break
        kind = job_kind(name)
        interval = expected_interval_s(kind, sched)
        last_started = ensure_utc(last.started_at)
        success_at = utc(success.finished_at or success.started_at) if success is not None else None
        success_age = age_seconds(success_at, now)
        stale = False
        if interval is not None:
            limit = stale_after_s(interval)
            if success_age is not None:
                stale = success_age > limit
            else:
                oldest = ensure_utc(runs[-1].started_at)
                stale = (now - oldest).total_seconds() > limit
        finished = utc(last.finished_at)
        out.append(
            JobHealth(
                name=name,
                kind=kind,
                status=last.status,
                last_started_at=last_started,
                last_finished_at=finished,
                age_s=round((now - last_started).total_seconds(), 1),
                duration_s=round((finished - last_started).total_seconds(), 3) if finished else None,
                items=last.items,
                error=(last.error or None) and str(last.error)[:500],
                last_success_at=success_at,
                last_success_age_s=success_age,
                consecutive_failures=failures,
                expected_interval_s=interval,
                stale=stale,
            )
        )
    return out


def _job_alerts(jobs: Sequence[JobHealth], ctx: ApiContext, alerts: _Alerts) -> None:
    opts = ctx.options
    for job in jobs:
        if job.stale and job.kind == "odds":
            alerts.add(
                "warning",
                "odds_collector_stale",
                f"Odds collector stale: last successful run {_fmt_age(job.last_success_age_s)} ({job.name})",
            )
        elif job.stale:
            alerts.add(
                "warning",
                "job_stale",
                f"Job '{job.name}' is stale: last successful run {_fmt_age(job.last_success_age_s)}",
            )
        if job.consecutive_failures >= opts.failing_job_threshold:
            detail = f": {job.error[:160]}" if job.error else ""
            alerts.add(
                "warning",
                "job_failing",
                f"Job '{job.name}' failing ({job.consecutive_failures} consecutive failures){detail}",
            )
        if job.status is JobStatus.RUNNING and job.age_s > opts.stuck_job_after_s:
            alerts.add(
                "warning",
                "job_stuck",
                f"Job '{job.name}' has been running for {_fmt_duration(job.age_s)} (stuck?)",
            )


# --------------------------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------------------------


def _parse_executor_health(value: dict[str, Any]) -> ExecutorHealth | None:
    if not value:
        return None
    try:
        return ExecutorHealth.model_validate(value)
    except ValidationError:
        pass
    # Tolerate partially written records; a missing check time is reported as very old.
    logged_in = value.get("logged_in")
    selectors = value.get("selectors_ok")
    try:
        return ExecutorHealth(
            ok=bool(value.get("ok", False)),
            logged_in=logged_in if isinstance(logged_in, bool) else None,
            message=str(value.get("message") or ""),
            selectors_ok={str(k): bool(v) for k, v in selectors.items()}
            if isinstance(selectors, dict)
            else {},
            checked_at=value.get("checked_at") or datetime.fromtimestamp(0, tz=UTC),
        )
    except (ValidationError, TypeError, ValueError):
        return None


def executor_health(
    ctx: ApiContext, session: Session | None, now: datetime, alerts: _Alerts
) -> ExecutorHealthOut:
    exe = ctx.settings.executor
    health: ExecutorHealth | None = None
    source: Literal["provider", "system_state", "none"] = "none"
    if ctx.executor_health_provider is not None:
        try:
            health = ctx.executor_health_provider()
        except Exception as exc:  # provider belongs to the executor package - never break /health
            log.warning("executor health provider failed: %s", type(exc).__name__)
            alerts.add(
                "warning", "executor_health_error", f"Executor health check failed ({type(exc).__name__})"
            )
        if health is not None:
            source = "provider"
    if health is None and session is not None:
        health = _parse_executor_health(state.get_state(session, state.EXECUTOR_HEALTH))
        if health is not None:
            source = "system_state"
    checked_at = utc(health.checked_at) if health is not None else None
    out = ExecutorHealthOut(
        source=source,
        enabled=exe.enabled,
        dry_run=exe.dry_run,
        ok=health.ok if health is not None else None,
        logged_in=health.logged_in if health is not None else None,
        balance=money_str(health.balance) if health is not None else None,
        selectors_ok=dict(health.selectors_ok) if health is not None else {},
        message=health.message if health is not None else "",
        checked_at=checked_at,
        age_s=age_seconds(checked_at, now),
    )
    if not exe.enabled:
        alerts.add("info", "executor_disabled", "Executor is disabled: no bets are placed")
        return out
    if health is None:
        alerts.add("info", "executor_health_unknown", "Executor health unknown: no health check recorded yet")
        return out
    if health.logged_in is False:
        alerts.add(
            "critical", "betclic_login_required", "Betclic login required: open the login window and sign in"
        )
    if not health.ok:
        alerts.add(
            "warning", "executor_unhealthy", f"Executor unhealthy: {health.message or 'no details'}"[:300]
        )
    failing = sorted(k for k, ok in health.selectors_ok.items() if not ok)
    if failing:
        alerts.add(
            "warning",
            "executor_selectors_failing",
            f"Betclic page layout changed? Failing selectors: {', '.join(failing)[:200]}",
        )
    if out.age_s is not None and out.age_s > ctx.options.executor_health_stale_s:
        alerts.add(
            "warning", "executor_health_stale", f"Executor health check is stale ({_fmt_age(out.age_s)})"
        )
    return out


# --------------------------------------------------------------------------------------------
# freshness / trading
# --------------------------------------------------------------------------------------------


def freshness(session: Session, jobs: Sequence[JobHealth], now: datetime) -> FreshnessOut:
    def latest(column: Any) -> datetime | None:
        return utc(session.scalar(select(func.max(column))))

    snapshot_at = latest(OddsSnapshot.captured_at)
    event_seen = latest(BookmakerEvent.last_seen_at)
    match_sync = latest(Match.last_synced_at)
    sync_jobs: list[SyncJobFreshness] = []
    for kind in SYNC_KINDS:
        for job in jobs:
            if job.kind == kind:
                sync_jobs.append(
                    SyncJobFreshness(
                        kind=kind,
                        name=job.name,
                        last_success_at=job.last_success_at,
                        age_s=job.last_success_age_s,
                    )
                )
    return FreshnessOut(
        last_odds_snapshot_at=snapshot_at,
        last_odds_snapshot_age_s=age_seconds(snapshot_at, now),
        last_event_seen_at=event_seen,
        last_event_seen_age_s=age_seconds(event_seen, now),
        last_match_sync_at=match_sync,
        last_match_sync_age_s=age_seconds(match_sync, now),
        last_prediction_at=latest(Prediction.created_at),
        last_analysis_at=latest(MatchAnalysis.created_at),
        last_decision_at=latest(BetDecisionRow.decided_at),
        sync_jobs=sync_jobs,
    )


def _odds_collector_alerts(
    jobs: Sequence[JobHealth], fresh: FreshnessOut, ctx: ApiContext, alerts: _Alerts
) -> None:
    if any(j.kind == "odds" for j in jobs):
        return  # handled per job
    limit = stale_after_s(float(ctx.settings.scheduler.odds_poll_s))
    if fresh.last_event_seen_age_s is None:
        alerts.add(
            "warning", "odds_collector_never_ran", "Odds collector has not run yet: no Betclic odds collected"
        )
    elif fresh.last_event_seen_age_s > limit:
        alerts.add(
            "warning",
            "odds_collector_stale",
            f"Odds collector stale: Betclic events last seen {_fmt_age(fresh.last_event_seen_age_s)}",
        )


def _trading_alerts(trading: TradingStateOut, alerts: _Alerts) -> None:
    if trading.kill_switch.on:
        reason = f": {trading.kill_switch.reason}" if trading.kill_switch.reason else ""
        alerts.add("critical", "kill_switch_on", f"Kill switch is ON{reason}"[:300])
    if trading.paused.on:
        reason = f": {trading.paused.reason}" if trading.paused.reason else ""
        alerts.add("warning", "trading_paused", f"Trading paused{reason}"[:300])


def _loss_limit_alerts(session: Session, ctx: ApiContext, now: datetime, alerts: _Alerts) -> None:
    try:
        bankroll = compute_bankroll(session, ctx.settings, dry_run=False, now=now, max_points=1)
    except SQLAlchemyError:
        raise
    except Exception as exc:  # bad data must not take the health page down
        log.warning("loss-limit check failed: %s", type(exc).__name__)
        alerts.add(
            "warning", "bankroll_unavailable", f"Bankroll / loss limits unavailable ({type(exc).__name__})"
        )
        return
    for limit in (bankroll.daily, bankroll.weekly):
        if limit.reached:
            label = "Daily" if limit.period == "day" else "Weekly"
            alerts.add(
                "critical",
                "loss_limit_reached",
                f"{label} loss limit reached: P&L {limit.realized_pnl} {bankroll.currency} "
                f"(limit -{limit.limit_amount})",
            )


def compute_health(ctx: ApiContext) -> HealthOut:
    now = ctx.now()
    settings = ctx.settings
    alerts = _Alerts()
    if settings.executor.dry_run:
        alerts.add("warning", "dry_run_on", "Dry-run mode is ON: bets are simulated, no real money is placed")

    db = DbHealth(ok=False)
    trading: TradingStateOut | None = None
    jobs: list[JobHealth] = []
    fresh: FreshnessOut | None = None
    pending_reviews: int | None = None
    exec_out: ExecutorHealthOut | None = None
    session = ctx.session_factory()
    reachable = False
    try:
        started = time.perf_counter()
        session.execute(text("SELECT 1"))
        reachable = True
        db = DbHealth(ok=True, latency_ms=round((time.perf_counter() - started) * 1000, 2))
        trading = trading_state(session)
        _trading_alerts(trading, alerts)
        jobs = job_health(session, settings.scheduler, now)
        _job_alerts(jobs, ctx, alerts)
        fresh = freshness(session, jobs, now)
        _odds_collector_alerts(jobs, fresh, ctx, alerts)
        pending_reviews = int(
            session.scalar(
                select(func.count(BookmakerEvent.id)).where(
                    BookmakerEvent.link_status == LinkStatus.PENDING_REVIEW
                )
            )
            or 0
        )
        if pending_reviews:
            alerts.add(
                "info",
                "link_review_pending",
                f"{pending_reviews} Betclic event(s) need link review (never bet until confirmed)",
            )
        _loss_limit_alerts(session, ctx, now, alerts)
        exec_out = executor_health(ctx, session, now, alerts)
    except SQLAlchemyError as exc:
        session.rollback()
        db = DbHealth(ok=False, latency_ms=db.latency_ms, error=_sanitize_error(exc))
        if reachable:
            alerts.add(
                "critical",
                "db_error",
                "Database error - are the migrations applied? (run: cs2bot db upgrade)",
            )
        else:
            alerts.add("critical", "db_unreachable", "Database unreachable: the bot cannot run")
    finally:
        session.close()
    if exec_out is None:
        exec_out = executor_health(ctx, None, now, alerts)
    status = alerts.status()
    return HealthOut(
        status="critical" if status == "critical" else "warning" if status == "warning" else "ok",
        now=now,
        version=__version__,
        db=db,
        trading=trading,
        dry_run=settings.executor.dry_run,
        executor=exec_out,
        jobs=jobs,
        freshness=fresh,
        pending_link_reviews=pending_reviews,
        alerts=alerts.sorted(),
    )
