"""Backtests over the real database (what ``cs2bot backtest --from --to`` runs).

* :func:`load_backtest_matches` - finished, complete matches with their actual result
  (:func:`cs2bot.datasets.load_match_actuals`) and pre-match context
  (:func:`cs2bot.datasets.load_match_context` as of kick-off, without LLM flags and without the
  starting sides, which only exist for maps that were played);
* :func:`load_historical_data` - every training record (map results, vetoes, rounds, player stat
  lines) the walk-forward engine slices by time;
* :func:`run_db_backtest` - both, plus the stored Betclic price history (:class:`DbOddsHistory`)
  and the full pipeline trainer, in one call.

Nothing here writes to the database.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot import datasets
from cs2bot.backtest.odds_history import DbOddsHistory, OddsHistory
from cs2bot.backtest.pipeline import PipelineConfig, PipelineTrainer
from cs2bot.backtest.report import BacktestReport
from cs2bot.backtest.runner import BacktestConfig, run_backtest
from cs2bot.backtest.types import BacktestMatch, HistoricalData, Trainer
from cs2bot.config import Settings
from cs2bot.db.models import Match
from cs2bot.domain.enums import MatchStatus
from cs2bot.utils.time import ensure_utc

logger = logging.getLogger(__name__)

__all__ = ["load_backtest_matches", "load_historical_data", "run_db_backtest"]


def load_backtest_matches(
    session: Session,
    *,
    settings: Settings,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
    with_context: bool = True,
    context_since: datetime | None = None,
) -> list[BacktestMatch]:
    """Finished matches kicking off in ``[since, until)`` whose result is complete.

    Matches without both teams, without a format or start time, or whose actuals are incomplete
    (settlement would answer UNKNOWN) are skipped. A failing context load only drops the context.
    ``context_since`` limits the (slow) context loading to matches kicking off at or after it -
    earlier matches only serve as training history and never need one.
    """
    context_from = ensure_utc(context_since) if context_since is not None else None
    stmt = select(Match).where(
        Match.status == MatchStatus.FINISHED,
        Match.team1_id.is_not(None),
        Match.team2_id.is_not(None),
        Match.scheduled_at.is_not(None),
        Match.format.is_not(None),
    )
    if in_scope_only:
        stmt = stmt.where(Match.in_scope.is_(True))
    if since is not None:
        stmt = stmt.where(Match.scheduled_at >= ensure_utc(since))
    if until is not None:
        stmt = stmt.where(Match.scheduled_at < ensure_utc(until))
    out: list[BacktestMatch] = []
    for match in session.scalars(stmt.order_by(Match.scheduled_at, Match.id)):
        try:
            actuals = datasets.load_match_actuals(session, match.id)
        except Exception as exc:  # one malformed match must not abort the batch
            logger.warning("skipping match %s: actuals failed (%s)", match.id, exc)
            continue
        if actuals is None or not actuals.complete or match.scheduled_at is None or match.format is None:
            continue
        if match.team1_id is None or match.team2_id is None:  # pragma: no cover - filtered by the query
            continue
        start = ensure_utc(match.scheduled_at)
        context = None
        if with_context and (context_from is None or start >= context_from):
            try:
                context = datasets.load_match_context(
                    session, match.id, settings=settings, now=start, include_context_flags=False
                )
                # Starting sides are stored from the played maps only (map rows exist once a map is
                # played), so before a match the live bot never knows them - and here they would tell
                # which maps were actually played (e.g. a decider). Final review: drop them.
                context = context.model_copy(update={"start_sides": {}})
            except Exception as exc:
                logger.warning("match %s: no pre-match context (%s)", match.id, exc)
        out.append(
            BacktestMatch(
                match_id=match.id,
                start_time=start,
                format=match.format,
                team1_id=match.team1_id,
                team2_id=match.team2_id,
                actuals=actuals,
                context=context,
                tier=match.tier,
                is_lan=bool(match.is_lan),
            )
        )
    return out


def load_historical_data(
    session: Session,
    *,
    matches: list[BacktestMatch],
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
    with_rounds: bool = True,
    with_player_maps: bool = False,
    result_delay: timedelta = timedelta(hours=1),
) -> HistoricalData:
    """Every training record in ``[since, until)`` wrapped for the walk-forward engine."""
    return HistoricalData(
        map_results=datasets.load_map_results(session, since=since, until=until, in_scope_only=in_scope_only),
        vetoes=datasets.load_veto_records(session, since=since, until=until, in_scope_only=in_scope_only),
        rounds=datasets.load_round_records(session, since=since, until=until, in_scope_only=in_scope_only)
        if with_rounds
        else (),
        player_maps=datasets.load_player_map_records(
            session, since=since, until=until, in_scope_only=in_scope_only
        )
        if with_player_maps
        else (),
        matches=matches,
        result_delay=result_delay,
    )


def run_db_backtest(
    session: Session,
    *,
    settings: Settings,
    since: datetime,
    until: datetime,
    history_since: datetime | None = None,
    config: BacktestConfig | None = None,
    pipeline_config: PipelineConfig | None = None,
    trainer: Trainer | None = None,
    odds: OddsHistory | None = None,
    in_scope_only: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> BacktestReport:
    """Walk-forward backtest of the matches kicking off in ``[since, until)``.

    Training history starts at ``history_since`` (default ``settings.scope.backfill_start``, the CS2
    release); prices come from the stored Betclic history unless ``odds`` is given; the model is the
    full pipeline (:class:`PipelineTrainer` with ``pipeline_config``) unless ``trainer`` is given.
    """
    start = ensure_utc(since)
    end = ensure_utc(until)
    hist_start = ensure_utc(history_since) if history_since is not None else None
    if hist_start is None:
        b = settings.scope.backfill_start
        hist_start = datetime(b.year, b.month, b.day, tzinfo=start.tzinfo)
    cfg = (config or BacktestConfig()).model_copy(update={"start": start, "end": end})
    pc = pipeline_config or PipelineConfig()
    matches = load_backtest_matches(
        session,
        settings=settings,
        since=hist_start,
        until=end,
        in_scope_only=in_scope_only,
        context_since=start,
    )
    data = load_historical_data(
        session,
        matches=matches,
        since=hist_start,
        until=end,
        in_scope_only=in_scope_only,
        with_rounds=pc.simulate or pc.use_ensemble,
        with_player_maps=pc.player_props or pc.use_ensemble,
    )
    return run_backtest(
        data,
        trainer=trainer or PipelineTrainer(pc, settings=settings),
        odds=odds or DbOddsHistory(session),
        config=cfg,
        settings=settings,
        progress=progress,
    )
