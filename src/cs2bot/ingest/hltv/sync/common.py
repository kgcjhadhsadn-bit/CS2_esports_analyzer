"""Shared pieces of the HLTV -> DB sync: options, the page-source protocol, entity helpers."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import ScopeSettings
from cs2bot.db.models import Player, Team, TeamRanking
from cs2bot.db.repo import resolve_player
from cs2bot.domain.enums import DataSource, Tier
from cs2bot.ingest.hltv.errors import HLTVBlockedError, HLTVFetchError, HLTVNotFoundError
from cs2bot.ingest.hltv.models import SyncReport
from cs2bot.utils.time import parse_datetime, utcnow

logger = logging.getLogger(__name__)

SOURCE = DataSource.HLTV
EXTRA_KEY = "hltv"


class PageSource(Protocol):
    """What the sync functions need from a fetcher (:class:`HLTVFetcher` satisfies it)."""

    def get(self, path_or_url: str, *, max_age_minutes: int | None = None) -> str: ...


@dataclass
class HLTVSyncOptions:
    """Sync knobs that are not (yet) part of :class:`cs2bot.config.Settings`."""

    listing_ttl_minutes: int = 20  # /results and /matches pages within one sync run
    upcoming_match_ttl_minutes: int = 120  # match page of an upcoming match far from its start
    near_start_window_minutes: int = 120  # vetoes / lineups appear ~1 h before the start ...
    near_start_ttl_minutes: int = 10  # ... so re-fetch the match page often in that window
    match_link_window_hours: float = 12.0  # db.repo.find_match window when linking PandaScore matches
    # Also write complete HLTV round histories into ``rounds`` (source=HLTV). Maps that already have
    # DEMO rounds are never touched; demo parsing later replaces the HLTV rows.
    write_round_rows: bool = True
    max_news_articles: int = 100  # article pages fetched per sync_news call
    min_ranking_rows_for_cleanup: int = 30  # clear stale Team.hltv_rank only after a full ranking
    commit_every: int | None = None  # sync_results: commit after every N matches (long backfills)
    # A finished match only counts as complete (skipped by later result syncs) once HLTV shows its demo
    # link and every played map's stats link - or once it started this long ago (some never get one).
    finalize_after_hours: float = 48.0
    # Ranks shown on a match page / an explicitly dated ranking only move ``Team.hltv_rank`` (the
    # *current* rank) when that ranking is at most this old: a backfill must not give a team that is
    # unranked today its rank from years ago (the dated ``team_rankings`` row is always stored).
    current_rank_max_age_days: int = 21


@contextmanager
def fetch_guard(report: SyncReport, what: str) -> Iterator[None]:
    """Turn fetch errors into report entries. A block stops the caller (``report.blocked``)."""
    try:
        yield
    except HLTVBlockedError as exc:
        report.blocked = True
        report.add_error(
            f"{what}: blocked by HLTV ({exc.reason}); retry after {exc.retry_after_s or 0:.0f} s"
        )
        raise
    except HLTVNotFoundError:
        report.skipped += 1
        report.add_warning(f"{what}: page not found")
        raise
    except HLTVFetchError as exc:
        report.failed += 1
        report.add_error(f"{what}: {exc}")
        raise


def fetch_page(
    source: PageSource, report: SyncReport, path: str, what: str, *, max_age_minutes: int | None = None
) -> str | None:
    """Fetch one page; on any fetch error record it in ``report`` and return None.

    A :class:`ValueError` from the fetcher (a scraped link pointing at another host, refused by
    :func:`cs2bot.ingest.hltv.fetcher.hltv_url`) is recorded as a failure too, never raised.
    """
    try:
        with fetch_guard(report, what):
            html = source.get(path, max_age_minutes=max_age_minutes)
    except (HLTVBlockedError, HLTVNotFoundError, HLTVFetchError):
        return None
    except ValueError as exc:
        report.failed += 1
        report.add_error(f"{what}: {exc}")
        return None
    report.fetched_pages += 1
    return html


def drop_cached(source: PageSource, path: str) -> None:
    """Forget a cached page that turned out to be unparsable (e.g. a truncated or error page).

    Map stats pages and articles are cached for a month, so a broken copy would otherwise block the
    data until it expires. Sources without a cache (fakes) are left alone.
    """
    invalidate = getattr(source, "invalidate", None)
    if not callable(invalidate):
        return
    try:
        invalidate(path)
    except Exception as exc:  # a cache problem must never break the sync
        logger.debug("could not drop the cached copy of %s: %s", path, exc)


def source_base_url(source: PageSource, default: str) -> str:
    base = getattr(source, "base_url", None)
    return base if isinstance(base, str) and base else default


# ------------------------------------------------------------------------------------- scope


def _external_is_in_scope() -> Callable[..., Any] | None:
    """``cs2bot.ingest.scope.is_in_scope`` when that module exists (built in parallel by P1)."""
    try:
        module = importlib.import_module("cs2bot.ingest.scope")
    except ImportError:
        return None
    fn = getattr(module, "is_in_scope", None)
    return fn if callable(fn) else None


def rank_as_of(
    session: Session, team: Team | None, when: datetime | None, *, now: datetime | None = None
) -> int | None:
    """HLTV rank of ``team`` that applies to a match at ``when``.

    Delegates to ``cs2bot.ingest.scope.team_rank_as_of`` (ranking history for old matches, so a
    backfill does not pull in 2023 matches of teams that are only good today); falls back to the
    current ``Team.hltv_rank`` when that module is not available.
    """
    if team is None:
        return None
    try:
        module = importlib.import_module("cs2bot.ingest.scope")
        fn = getattr(module, "team_rank_as_of", None)
    except ImportError:
        fn = None
    if callable(fn):
        try:
            value = fn(session, team, when, now=now)
            return value if value is None or isinstance(value, int) else None
        except Exception as exc:  # incompatible signature during parallel development
            logger.debug("ingest.scope.team_rank_as_of failed (%s); using the current rank", exc)
    return team.hltv_rank


def in_scope(tier: Tier | None, rank1: int | None, rank2: int | None, scope: ScopeSettings) -> bool:
    """Scope rule of PLAN.md 4.1 (tier S/A/B or a top-N HLTV team), via ``ingest.scope`` when available."""
    external = _external_is_in_scope()
    if external is not None:
        try:
            return bool(external(tier or Tier.UNRANKED, rank1, rank2, scope))
        except Exception as exc:  # incompatible signature during parallel development
            logger.debug("ingest.scope.is_in_scope failed (%s); using the HLTV fallback rule", exc)
    tiers = {t.lower() for t in scope.pandascore_tiers}
    if tier is not None and tier.value in tiers:
        return True
    return any(r is not None and 0 < r <= scope.hltv_top_n for r in (rank1, rank2))


# ------------------------------------------------------------------------------------- entities


def merge_extra(
    current: dict[str, Any] | None, updates: dict[str, Any], *, key: str = EXTRA_KEY
) -> dict[str, Any]:
    """Return a NEW extra dict (JSON columns do not track in-place mutation) with ``updates`` merged."""
    base = dict(current or {})
    section = dict(base.get(key) or {})
    section.update({k: v for k, v in updates.items() if v is not None})
    base[key] = section
    return base


def resolve_hltv_player(
    session: Session,
    *,
    nickname: str,
    hltv_id: int | None,
    team_id: int,
    seen_at: datetime | None,
    country: str | None = None,
    real_name: str | None = None,
    move_team: bool = True,
) -> Player | None:
    """Find or create a player seen in an HLTV match of ``team_id`` at ``seen_at``.

    When syncing old matches (a backfill walks newest -> oldest) blindly moving ``current_team_id``
    would leave players on their *oldest* team, so for players already known by HLTV id the current
    team only moves when this match is newer than the last HLTV sighting
    (``players.extra['hltv_team_seen_at']``). Stand-ins (``move_team=False``) never move a known
    player's team (``db.repo.resolve_player(update_current_team=False)``); a brand-new player still
    gets the team they were seen with.
    """
    if not nickname:
        return None
    player = session.scalar(select(Player).where(Player.hltv_id == hltv_id)) if hltv_id is not None else None
    fields = {"country": country, "real_name": real_name}
    if player is None:
        player = resolve_player(
            session,
            nickname=nickname,
            source=SOURCE,
            external_id=hltv_id,
            team_id=team_id,
            update_current_team=move_team,
            country=country,
            real_name=real_name,
        )
        if player is not None and seen_at is not None:
            player.extra = {**(player.extra or {}), "hltv_team_seen_at": seen_at.isoformat()}
        return player
    last_seen = parse_datetime((player.extra or {}).get("hltv_team_seen_at"))
    if move_team and (seen_at is None or last_seen is None or seen_at >= last_seen):
        player.current_team_id = team_id
        if nickname and player.nickname != nickname:
            player.nickname = nickname  # HLTV is the reference for nickname changes
        if seen_at is not None:
            player.extra = {**(player.extra or {}), "hltv_team_seen_at": seen_at.isoformat()}
    for key, value in fields.items():
        if value and not getattr(player, key):
            setattr(player, key, value)
    return player


def utc_midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def record_team_rank(
    session: Session,
    team: Team,
    rank: int,
    ranked_on: date,
    points: int | None = None,
    *,
    current_max_age_days: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Upsert a ``team_rankings`` row and move ``Team.hltv_rank`` forward if this ranking is newer.

    With ``current_max_age_days`` the current rank is only touched when ``ranked_on`` is at most that
    many days before ``now``: an old match page (backfill) must not set the *current* rank of a team
    that has no newer ranking (e.g. one that is unranked today). Returns True if the current rank was
    updated.
    """
    row = session.scalar(
        select(TeamRanking).where(
            TeamRanking.team_id == team.id, TeamRanking.source == SOURCE, TeamRanking.ranked_on == ranked_on
        )
    )
    if row is None:
        session.add(
            TeamRanking(team_id=team.id, source=SOURCE, rank=rank, points=points, ranked_on=ranked_on)
        )
    else:
        row.rank = rank
        if points is not None:
            row.points = points
    ranked_at = utc_midnight(ranked_on)
    if current_max_age_days is not None:
        today = (now or utcnow()).date()
        if ranked_on < today - timedelta(days=current_max_age_days):
            return False
    if team.hltv_rank_updated_at is None or team.hltv_rank_updated_at <= ranked_at:
        team.hltv_rank = rank
        team.hltv_rank_updated_at = ranked_at
        return True
    return False
