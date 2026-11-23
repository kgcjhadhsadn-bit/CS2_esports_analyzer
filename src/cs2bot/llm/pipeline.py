"""Ready-made glue between the analyzer and the database (what the scheduler jobs call).

* :func:`triage_pending_news` - run Haiku triage over news that has not been triaged yet.
* :func:`refresh_match_context` - select news for a match, run the Opus analysis only when the
  inputs changed (cost guard), persist the flags, and return the flags to use.

Both ``flush()`` but do not ``commit()``; wrap them in ``cs2bot.db.session.session_scope``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cs2bot.db.models import Match, NewsItem, Team
from cs2bot.domain.context import ContextFlag
from cs2bot.domain.match import MatchContext
from cs2bot.llm.combine import active_flags
from cs2bot.llm.context import ContextAnalyzer, LLMStatus
from cs2bot.llm.memo import context_fingerprint
from cs2bot.llm.store import (
    llm_extra,
    load_active_flags,
    merge_llm_extra,
    news_row_to_input,
    persist_flags,
    select_news_for_match,
    store_triage,
)
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import ensure_utc, parse_datetime, utcnow

log = get_logger(__name__)

_CONTEXT_MARKER = "context"
_ATTEMPTS_KEY = "triage_attempts"


@dataclass(frozen=True)
class TriageReport:
    """Summary of one :func:`triage_pending_news` run."""

    status: LLMStatus
    candidates: int = 0
    triaged: int = 0
    unanswered: int = 0
    deferred: int = 0


@dataclass(frozen=True)
class ContextRefresh:
    """Result of :func:`refresh_match_context`.

    ``flags`` are the active, unexpired flags to use for the match right now. ``source`` tells where
    they came from: ``"llm"`` (fresh Opus call), ``"memo"`` (in-memory cache), ``"db"`` (unchanged
    inputs, flags loaded from the database) or ``"stale"`` (analysis unavailable - disabled, refused,
    failed or backing off - so the previously stored flags are kept).
    """

    flags: list[ContextFlag] = field(default_factory=list)
    status: LLMStatus = LLMStatus.OK
    source: str = "db"
    note: str = ""
    n_news: int = 0
    fingerprint: str = ""


def load_known_teams(session: Session) -> dict[int, str]:
    """``teams.id -> teams.name`` for every team (the ``known_teams`` argument of ``triage``)."""
    return {int(team_id): name for team_id, name in session.execute(select(Team.id, Team.name)) if name}


def triage_pending_news(
    session: Session,
    analyzer: ContextAnalyzer,
    *,
    limit: int = 200,
    max_age_days: int = 30,
    now: datetime | None = None,
) -> TriageReport:
    """Triage up to ``limit`` not-yet-triaged news items (newest first) and store the verdicts.

    Items older than ``max_age_days`` are left alone (they cannot affect upcoming matches). An item
    the model repeatedly fails to classify for content-related reasons (refused, truncated, skipped)
    is given up on after ``tuning.triage_max_attempts`` attempts (counted in
    ``news_items.extra["llm"]``). It then stays untriaged, which ``select_news_for_match`` still shows
    to the context model. Transient failures (rate limit, network, a rejected request, disabled) do
    not count as attempts.
    """
    current = ensure_utc(now) if now is not None else utcnow()
    since = current - timedelta(days=max(0, max_age_days))
    max_attempts = max(1, analyzer.tuning.triage_max_attempts)
    rows = session.scalars(
        select(NewsItem)
        .where(
            NewsItem.triaged_at.is_(None),
            or_(
                NewsItem.published_at >= since,
                and_(NewsItem.published_at.is_(None), NewsItem.created_at >= since),
            ),
        )
        .order_by(NewsItem.id.desc())
    )
    # Given-up items stay untriaged (``triaged_at`` NULL), so they are filtered here rather than with
    # a SQL LIMIT - otherwise a pile of them at the top could starve newer work. The window is at
    # most ``max_age_days`` of news (a few hundred rows).
    pending: dict[int, NewsItem] = {}
    for row in rows:
        if len(pending) >= limit:
            break
        if _attempts(row) >= max_attempts:
            continue
        pending[row.id] = row
    if not pending:
        return TriageReport(LLMStatus.OK)
    if not analyzer.enabled:
        return TriageReport(LLMStatus.DISABLED, candidates=len(pending), deferred=len(pending))
    outcome = analyzer.triage_detailed(
        [(news_id, news_row_to_input(row)) for news_id, row in pending.items()], load_known_teams(session)
    )
    stored = 0
    for verdict in outcome.items:
        if store_triage(session, verdict.news_id, verdict, now=current, model=outcome.model):
            stored += 1
    for news_id in outcome.unanswered_ids:
        row = pending[news_id]
        row.extra = merge_llm_extra(row.extra, {_ATTEMPTS_KEY: _attempts(row) + 1})
    session.flush()
    return TriageReport(
        outcome.status,
        candidates=len(pending),
        triaged=stored,
        unanswered=len(outcome.unanswered_ids),
        deferred=len(outcome.deferred_ids),
    )


def _attempts(row: NewsItem) -> int:
    value = llm_extra(row.extra).get(_ATTEMPTS_KEY, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def refresh_match_context(
    session: Session,
    analyzer: ContextAnalyzer,
    ctx: MatchContext,
    *,
    now: datetime | None = None,
    form_summary: Mapping[str, Any] | None = None,
    days: int = 14,
    force: bool = False,
) -> ContextRefresh:
    """Bring the match's context flags up to date and return the flags to use.

    1. Select recent, relevant news for the match (``select_news_for_match``).
    2. Cost guard: skip the Opus call when the inputs' fingerprint equals the one stored with the
       last successful analysis (``matches.extra["llm"]["context"]``) and that analysis is younger
       than ``analyzer.reanalysis_interval(ctx, now)``; the in-memory memo covers the same case
       within one process. ``force=True`` bypasses both.
    3. On a successful analysis the flags are persisted (replacing the active ones) and the marker
       is updated. On any other outcome nothing is written and the stored flags stay in force - a
       failed call never wipes an earlier veto.
    """
    current = ensure_utc(now) if now is not None else utcnow()
    news = select_news_for_match(
        session,
        ctx,
        days=days,
        limit=analyzer.settings.llm.max_news_items_per_match,
        now=current,
        min_relevance=analyzer.tuning.min_relevance,
        max_global=analyzer.tuning.max_global_news,
    )
    fingerprint = context_fingerprint(ctx, news)
    match = session.get(Match, ctx.match_id)
    stored = load_active_flags(session, ctx.match_id, now=current) if match is not None else []
    if not analyzer.enabled:
        return ContextRefresh(stored, LLMStatus.DISABLED, "stale", n_news=len(news), fingerprint=fingerprint)
    marker = llm_extra(match.extra).get(_CONTEXT_MARKER) if match is not None else None
    if not force and isinstance(marker, dict) and marker.get("fingerprint") == fingerprint:
        analyzed_at = _marker_time(marker)
        if analyzed_at is not None:
            age = current - analyzed_at
            if timedelta(0) <= age < analyzer.reanalysis_interval(ctx, current):
                return ContextRefresh(
                    stored,
                    LLMStatus.OK,
                    "db",
                    note=str(marker.get("note") or ""),
                    n_news=len(news),
                    fingerprint=fingerprint,
                )
    outcome = analyzer.analyze_match_detailed(
        ctx, news, form_summary=form_summary, now=current, use_memo=not force
    )
    if not outcome.ok:
        return ContextRefresh(stored, outcome.status, "stale", n_news=len(news), fingerprint=fingerprint)
    if match is not None:
        persist_flags(
            session,
            ctx.match_id,
            outcome.flags,
            model=outcome.model,
            team_ids=(ctx.team1_id, ctx.team2_id),
            now=current,
        )
        match.extra = merge_llm_extra(
            match.extra,
            {
                _CONTEXT_MARKER: {
                    "fingerprint": fingerprint,
                    "analyzed_at": (outcome.analyzed_at or current).isoformat(),
                    "model": outcome.model,
                    "note": outcome.note,
                    "n_news": len(news),
                    "n_flags": len(outcome.flags),
                }
            },
        )
        session.flush()
    else:
        log.warning("match %s is not in the database - context flags were not persisted", ctx.match_id)
    return ContextRefresh(
        active_flags(outcome.flags, now=current),
        LLMStatus.OK,
        "memo" if outcome.from_cache else "llm",
        note=outcome.note,
        n_news=len(news),
        fingerprint=fingerprint,
    )


def _marker_time(marker: Mapping[str, Any]) -> datetime | None:
    try:
        return parse_datetime(marker.get("analyzed_at"))
    except (ValueError, TypeError, OverflowError):
        return None
