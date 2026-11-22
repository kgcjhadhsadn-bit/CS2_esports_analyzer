"""Sync the HLTV world ranking into ``team_rankings`` and ``teams.hltv_rank``."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.db.models import Team
from cs2bot.db.repo import resolve_team
from cs2bot.ingest.hltv.models import SyncReport
from cs2bot.ingest.hltv.parsers import parse_ranking_page
from cs2bot.ingest.hltv.sync.common import (
    SOURCE,
    HLTVSyncOptions,
    PageSource,
    fetch_page,
    record_team_rank,
    source_base_url,
    utc_midnight,
)
from cs2bot.ingest.hltv.urls import HLTV_BASE_URL, absolute_url, ranking_monday, ranking_path
from cs2bot.utils.time import utcnow

logger = logging.getLogger(__name__)


def sync_rankings(
    session: Session,
    fetcher: PageSource,
    ranking_date: date | None = None,
    *,
    options: HLTVSyncOptions | None = None,
) -> SyncReport:
    """Upsert one weekly HLTV ranking (the latest when ``ranking_date`` is None).

    ``Team.hltv_rank`` / ``hltv_rank_updated_at`` only move forward in time. After syncing the *latest*
    full ranking, teams that dropped out of it lose their ``hltv_rank`` (otherwise a team that fell
    out of the top N would stay in scope forever).
    """
    opts = options or HLTVSyncOptions()
    report = SyncReport(job="hltv.sync_rankings")
    path = ranking_path(ranking_date)
    html = fetch_page(fetcher, report, path, "ranking")
    if html is None:
        return report
    base_url = source_base_url(fetcher, HLTV_BASE_URL)
    rows = parse_ranking_page(html, url=absolute_url(path, base_url), ranking_date=ranking_date)
    if not rows:
        report.failed += 1
        report.add_error("ranking page contained no ranked teams (layout change or blocked page?)")
        return report
    ranked_on = rows[0].ranking_date or (
        ranking_monday(ranking_date) if ranking_date else ranking_monday(utcnow().date())
    )
    seen: set[int] = set()
    try:
        with session.begin_nested():
            for row in rows:
                team = resolve_team(session, name=row.team_name, source=SOURCE, external_id=row.hltv_team_id)
                if team is None or team.id in seen:
                    report.skipped += 1
                    continue
                seen.add(team.id)
                report.updated += 1  # one team_rankings row upserted
                if record_team_rank(session, team, row.rank, ranked_on, row.points):
                    report.details["current_rank_updates"] = report.details.get("current_rank_updates", 0) + 1
            is_latest = ranking_date is None
            if is_latest and len(rows) >= opts.min_ranking_rows_for_cleanup:
                cutoff = utc_midnight(ranked_on)
                for team in session.scalars(select(Team).where(Team.hltv_rank.is_not(None))):
                    if team.id not in seen and (
                        team.hltv_rank_updated_at is None or team.hltv_rank_updated_at < cutoff
                    ):
                        team.hltv_rank = None
                        team.hltv_rank_updated_at = cutoff
                        report.details["unranked"] = report.details.get("unranked", 0) + 1
            session.flush()
    except Exception as exc:
        logger.exception("persisting HLTV ranking failed")
        report.failed += 1
        report.add_error(f"ranking: persisting failed: {type(exc).__name__}: {exc}")
        return report
    report.details.update({"ranked_on": ranked_on.isoformat(), "teams": len(rows)})
    return report
