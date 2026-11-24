"""Backtest + confidence guard: the CLI command and the unattended weekly refresh share this code.

The owner bets on confidence (PLAN.md section 0), so the high-confidence calibration of
:mod:`cs2bot.models.confidence` decides how much a 90 % pick is trusted (``guard.adjust``, applied by
:mod:`cs2bot.engine.confidence` before the confidence check and the stake tier) and whether a market
type is trusted at all (``decision.require_trusted_calibration``). The guard is fitted on the resolved
predictions of a walk-forward backtest over stored Betclic prices and written to
``models.confidence_guard_file``.

Before the final review (Lens C) only ``cs2bot backtest`` wrote that file - on the VPS, which runs with
no human, it would never exist, and the guard would stay at its cautious no-history prior forever.
:func:`refresh_confidence_guard` (scheduler job ``confidence_guard_refresh``, train lane, daily slot)
re-runs the backtest over the last ``models.guard_backtest_days`` once the file is older than
``models.guard_refresh_days`` and the database holds enough priced finished matches. Every attempt is
remembered in ``system_state["confidence_guard"]`` so a window without prices is not re-tried every day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import (
    BookmakerEvent,
    BookmakerMarket,
    BookmakerSelection,
    Match,
    ModelRun,
    OddsSnapshot,
)
from cs2bot.domain.enums import JobStatus, LinkStatus, MatchStatus
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

#: ``system_state`` key of the last refresh attempt (time, outcome, trusted market types).
GUARD_STATE_KEY = "confidence_guard"
ACTOR = "confidence_guard"


@dataclass
class GuardRefreshReport:
    """What :func:`refresh_confidence_guard` / :func:`run_backtest_with_guard` did."""

    ran: bool = False
    written: bool = False
    skipped: str | None = None
    path: str | None = None
    n_predictions: int = 0
    n_bets: int = 0
    trusted: list[str] = field(default_factory=list)
    priced_matches: int | None = None
    backtest_run_id: int | None = None
    report: Any = field(default=None, repr=False)

    @property
    def items(self) -> int:
        return self.n_predictions

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "written": self.written,
            "skipped": self.skipped,
            "path": self.path,
            "n_predictions": self.n_predictions,
            "n_bets": self.n_bets,
            "trusted": list(self.trusted),
            "priced_matches": self.priced_matches,
            "backtest_run_id": self.backtest_run_id,
        }


def guard_path(settings: Settings) -> Path:
    return resolve_path(settings.models.confidence_guard_file)


def save_guard_from_report(settings: Settings, report: Any) -> tuple[Path | None, list[str], int]:
    """Fit and write the guard from a backtest report's resolved predictions.

    Returns ``(path or None when nothing was resolved, trusted market types, n predictions)``. The file
    is written atomically (temporary file + rename): the betting cycle may read it at any moment.
    """
    predictions = list(getattr(report, "predictions", None) or [])
    if not predictions:
        return None, [], 0
    from cs2bot.models.confidence import POOLED, ConfidenceGuard

    guard = ConfidenceGuard.from_predictions(predictions)
    target = guard_path(settings)
    tmp = target.with_name(target.name + ".tmp")
    guard.save(tmp)
    tmp.replace(target)
    trusted = sorted(m for m in guard.trust if m != POOLED and guard.is_trusted(m))
    return target, trusted, len(predictions)


def run_backtest_with_guard(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    since: datetime,
    until: datetime,
    sims: int = 2000,
    ensemble: bool = True,
    retrain_every: timedelta = timedelta(days=7),
    params: dict[str, Any] | None = None,
) -> GuardRefreshReport:
    """Walk-forward DB backtest (the live model: ensemble + tuned ratings), stored in ``model_runs``
    (kind ``backtest``), then the confidence guard fitted from it (see the module docstring)."""
    from cs2bot.backtest import BacktestConfig, PipelineConfig, run_db_backtest
    from cs2bot.services.registry import load_rating_config

    rating_config = load_rating_config(settings)
    started = utcnow()
    out = GuardRefreshReport(ran=True)
    with session_factory() as session:
        report = run_db_backtest(
            session,
            settings=settings,
            since=ensure_utc(since),
            until=ensure_utc(until),
            config=BacktestConfig(retrain_every=retrain_every),
            pipeline_config=PipelineConfig(
                simulate=True, n_sims=sims, use_ensemble=ensemble, rating_config=rating_config
            ),
        )
        if rating_config is not None:
            report.warnings.append(
                "rating model: the tuned configuration of `cs2bot models tune` (tuned on a holdout that may "
                "overlap this window)"
            )
        run = ModelRun(
            kind="backtest",
            model_version=f"bt{started:%Y%m%dT%H%M%S}",
            status=JobStatus.SUCCESS,
            started_at=started,
            finished_at=utcnow(),
            params={
                "from": ensure_utc(since).date().isoformat(),
                "to": ensure_utc(until).date().isoformat(),
                "sims": sims,
                "ensemble": ensemble,
                "rating_config": "tuned" if rating_config is not None else "default",
                **(params or {}),
            },
            metrics=json.loads(report.model_dump_json()),
        )
        session.add(run)
        session.commit()
        out.backtest_run_id = run.id
    out.report = report
    out.n_bets = int(getattr(report, "n_bets", 0) or 0)
    path, trusted, n = save_guard_from_report(settings, report)
    out.n_predictions, out.trusted = n, trusted
    if path is not None:
        out.written, out.path = True, str(path)
    else:
        out.skipped = "the backtest resolved no predictions (no stored Betclic prices for finished matches?)"
    return out


def priced_finished_matches(session: Session, *, since: datetime, until: datetime) -> int:
    """Finished in-scope matches in ``[since, until)`` with at least one stored Betclic price."""
    stmt = (
        select(func.count(func.distinct(Match.id)))
        .select_from(OddsSnapshot)
        .join(BookmakerSelection, BookmakerSelection.id == OddsSnapshot.selection_id)
        .join(BookmakerMarket, BookmakerMarket.id == BookmakerSelection.market_id)
        .join(BookmakerEvent, BookmakerEvent.id == BookmakerMarket.event_id)
        .join(Match, Match.id == BookmakerEvent.match_id)
        .where(
            Match.status == MatchStatus.FINISHED,
            Match.in_scope.is_(True),
            Match.scheduled_at >= since,
            Match.scheduled_at < until,
            BookmakerEvent.link_status.in_([LinkStatus.AUTO, LinkStatus.CONFIRMED]),
        )
    )
    return int(session.scalar(stmt) or 0)


def refresh_confidence_guard(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    now: datetime | None = None,
    force: bool = False,
    runner: Any = None,
) -> GuardRefreshReport:
    """Rewrite the guard from a fresh backtest when it is due (module docstring).

    Due when ``models.guard_refresh_days > 0``, the last attempt (successful or not) is at least that
    old, the guard file is missing or at least that old, and at least ``models.guard_min_priced_matches``
    finished matches in the window have stored prices. ``force`` skips the age checks. ``runner``
    replaces :func:`run_backtest_with_guard` (tests).
    """
    now = ensure_utc(now or utcnow())
    ms = settings.models
    out = GuardRefreshReport()
    if ms.guard_refresh_days <= 0 and not force:
        out.skipped = "disabled (models.guard_refresh_days = 0)"
        return out
    every = timedelta(days=max(ms.guard_refresh_days, 0.0))
    path = guard_path(settings)
    with session_factory() as session:
        memo = db_state.get_state(session, GUARD_STATE_KEY)
    if not force:
        last = _parse_time(memo.get("last_attempt"))
        if last is not None and now - last < every:
            out.skipped = f"last attempt {last.isoformat()} is younger than {ms.guard_refresh_days:g} days"
            return out
        if path.is_file():
            age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
            if age < every:
                out.skipped = f"the guard file is {age.total_seconds() / 86400:.1f} days old"
                return out
    since = now - timedelta(days=ms.guard_backtest_days)
    with session_factory() as session:
        out.priced_matches = priced_finished_matches(session, since=since, until=now)
    if out.priced_matches < ms.guard_min_priced_matches:
        out.skipped = (
            f"only {out.priced_matches} finished matches with stored Betclic prices in the last "
            f"{ms.guard_backtest_days} days (need {ms.guard_min_priced_matches}); the cautious no-history "
            "prior stays in force"
        )
        _remember(session_factory, now, out)
        return out
    run = runner or run_backtest_with_guard
    try:
        result: GuardRefreshReport = run(
            session_factory,
            settings,
            since=since,
            until=now,
            sims=ms.guard_backtest_sims,
            ensemble=True,
            retrain_every=timedelta(days=ms.guard_backtest_retrain_days),
            params={"scheduled": True},
        )
    except Exception as exc:
        out.ran = True
        out.skipped = f"backtest failed: {type(exc).__name__}: {exc}"[:500]
        _remember(session_factory, now, out)
        raise
    result.priced_matches = out.priced_matches
    _remember(session_factory, now, result)
    return result


def _remember(session_factory: sessionmaker[Session], now: datetime, report: GuardRefreshReport) -> None:
    try:
        with session_factory() as session:
            db_state.set_state(
                session, GUARD_STATE_KEY, {"last_attempt": now.isoformat(), **report.as_dict()}, actor=ACTOR
            )
            session.commit()
    except Exception:  # bookkeeping only
        log.warning("could not record the confidence-guard refresh", exc_info=True)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None
