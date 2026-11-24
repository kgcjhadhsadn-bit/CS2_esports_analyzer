"""Thin job functions around the ingest packages (PandaScore, HLTV, news triage, demos, odds).

Every function opens its own session(s) from ``session_factory``, commits what it did (also the
partial work of a run the source blocked half-way) and returns the package's own report object
(``domain.sync.SyncReport``, ``ProcessReport``, ``TriageReport``, ``CollectReport`` ...). They never
swallow a programming error: an exception propagates so the scheduler records the run as FAILED and
backs off.

Sources are injectable (``client=`` / ``fetcher=`` / ``downloader=`` / ``source=``) for tests and for
callers that keep one open. Without one, a source is created for the call and closed afterwards.

Thread rule (Playwright): the HLTV fetcher, the demo ``BrowserDownloader`` and the HLTV manual-check
window use the same persistent browser profile (``settings.hltv.browser_profile_dir``) and Playwright's
sync API is thread-bound. They are created, used and closed inside the call, i.e. inside the
scheduler lane that runs the job; all of them belong to ONE lane (``flows.jobs`` puts them in the
``hltv`` lane), so the profile is never opened twice at the same time. The Betclic odds browser runs
async Playwright in its own thread (``BetclicBrowserSource``) and uses its own profile-less context.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings
from cs2bot.db import state as db_state
from cs2bot.domain.sync import SyncReport
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

#: ``system_state`` key holding the resumable backfill progress.
BACKFILL_STATE_KEY = "backfill"
#: Results jobs look this far back (late results, corrections, HLTV's "Match over" settling).
RESULTS_LOOKBACK = timedelta(days=2)


class ReportLike(Protocol):
    """What :func:`report_to_tracker` needs: every ingest report has these."""

    @property
    def items(self) -> int: ...

    @property
    def ok(self) -> bool: ...

    def as_dict(self) -> dict[str, Any]: ...


class MissingCredentialError(RuntimeError):
    """A job needs a token / key the owner has not configured (message says which variable)."""


@contextmanager
def _session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success; on an exception roll back and re-raise."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _commit_partial(session: Session, report: SyncReport) -> None:
    """Sync functions keep partial progress in the session when the source blocks: keep it."""
    if report.blocked:
        log.warning("%s: source blocked us; keeping the partial work (%d items)", report.job, report.items)


# ------------------------------------------------------------------------------------------ PandaScore


def require_pandascore(settings: Settings) -> None:
    if settings.pandascore.api_token is None:
        raise MissingCredentialError(
            "PandaScore token missing: set CS2BOT_PANDASCORE__API_TOKEN in .env (see docs/WINDOWS_SETUP.md)"
        )


@contextmanager
def _pandascore_client(settings: Settings, client: Any | None) -> Iterator[Any]:
    if client is not None:
        yield client
        return
    require_pandascore(settings)
    from cs2bot.ingest.pandascore import PandaScoreClient

    with PandaScoreClient(settings.pandascore) as owned:
        yield owned


def pandascore_upcoming(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    client: Any | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Upcoming + running matches from PandaScore (fixtures, lineups, schedule changes)."""
    from cs2bot.ingest.pandascore import sync_upcoming

    with _pandascore_client(settings, client) as c, _session(session_factory) as session:
        report = sync_upcoming(session, c, settings, now=now)
        _commit_partial(session, report)
        return report


def pandascore_results(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    client: Any | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Matches that began in the last two days (results, map scores, round history)."""
    from cs2bot.ingest.pandascore import sync_past

    now = ensure_utc(now or utcnow())
    since = ensure_utc(since) if since is not None else now - RESULTS_LOOKBACK
    with _pandascore_client(settings, client) as c, _session(session_factory) as session:
        report = sync_past(session, c, settings, since, now=now)
        _commit_partial(session, report)
        return report


def pandascore_match(
    session_factory: sessionmaker[Session],
    settings: Settings,
    pandascore_match_id: int,
    *,
    client: Any | None = None,
) -> SyncReport:
    from cs2bot.ingest.pandascore import sync_match

    with _pandascore_client(settings, client) as c, _session(session_factory) as session:
        return sync_match(session, c, settings, pandascore_match_id)


# ------------------------------------------------------------------------------------------------ HLTV


@contextmanager
def _hltv_fetcher(settings: Settings, fetcher: Any | None) -> Iterator[Any]:
    if fetcher is not None:
        yield fetcher
        return
    from cs2bot.ingest.hltv import HLTVFetcher

    with HLTVFetcher(settings.hltv) as owned:
        yield owned


def hltv_upcoming(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    fetcher: Any | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Upcoming HLTV matches (lineups and vetoes appear here about an hour before the start)."""
    from cs2bot.ingest.hltv import sync_upcoming

    with _hltv_fetcher(settings, fetcher) as f, _session(session_factory) as session:
        report = sync_upcoming(session, f, settings=settings, now=now)
        _commit_partial(session, report)
        return report


def hltv_results(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    fetcher: Any | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Recent HLTV results with map stats (stops at the first page of already-complete matches)."""
    from cs2bot.ingest.hltv import sync_results

    now = ensure_utc(now or utcnow())
    since = ensure_utc(since) if since is not None else now - RESULTS_LOOKBACK
    with _hltv_fetcher(settings, fetcher) as f, _session(session_factory) as session:
        report = sync_results(session, f, settings=settings, since=since)
        _commit_partial(session, report)
        return report


def hltv_match(
    session_factory: sessionmaker[Session],
    settings: Settings,
    hltv_match_id: int,
    *,
    fetcher: Any | None = None,
) -> SyncReport:
    """One HLTV match page (plus its map stats pages), whatever its scope."""
    from cs2bot.ingest.hltv import sync_match

    with _hltv_fetcher(settings, fetcher) as f, _session(session_factory) as session:
        return sync_match(session, f, hltv_match_id, settings=settings)


def hltv_rankings(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    fetcher: Any | None = None,
    ranking_date: date | None = None,
) -> SyncReport:
    """The latest (or a dated) HLTV ranking; refreshes the scope of open matches afterwards."""
    from cs2bot.ingest.hltv import sync_rankings

    with _hltv_fetcher(settings, fetcher) as f, _session(session_factory) as session:
        report = sync_rankings(session, f, ranking_date, settings=settings)
        _commit_partial(session, report)
        return report


def hltv_news(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    fetcher: Any | None = None,
    months: int = 1,
    now: datetime | None = None,
) -> SyncReport:
    from cs2bot.ingest.hltv import sync_news

    with _hltv_fetcher(settings, fetcher) as f, _session(session_factory) as session:
        report = sync_news(session, f, months=months, now=now)
        _commit_partial(session, report)
        return report


# ------------------------------------------------------------------------------------- scope, triage


def refresh_scope(
    session_factory: sessionmaker[Session], settings: Settings, *, now: datetime | None = None
) -> int:
    """Re-evaluate ``matches.in_scope`` of open matches (after a ranking change); returns flags changed."""
    from cs2bot.ingest.scope import refresh_upcoming_scope

    with _session(session_factory) as session:
        return refresh_upcoming_scope(session, settings, now=now)


def triage_news(
    session_factory: sessionmaker[Session],
    analyzer: Any,
    *,
    limit: int = 200,
    now: datetime | None = None,
) -> Any:
    """Claude Haiku triage of news items not yet classified (``llm.triage_pending_news``)."""
    from cs2bot.llm import triage_pending_news

    with _session(session_factory) as session:
        return triage_pending_news(session, analyzer, limit=limit, now=now)


# -------------------------------------------------------------------------------------------- demos


@dataclass
class DemoJobReport:
    """``ProcessReport`` of the demo pipeline plus the aggregate / role rebuild that follows it."""

    process: Any
    aggregates: dict[str, int] = field(default_factory=dict)

    @property
    def items(self) -> int:
        return int(self.process.items)

    @property
    def ok(self) -> bool:
        return bool(self.process.ok)

    def as_dict(self) -> dict[str, Any]:
        return {**self.process.as_dict(), "aggregates": dict(self.aggregates)}


def rebuild_aggregates(session_factory: sessionmaker[Session]) -> dict[str, int]:
    """Position / duel aggregates (window ending today) and inferred player roles."""
    from cs2bot.features import update_player_roles
    from cs2bot.ingest.demos import rebuild_duel_aggregates, rebuild_position_aggregates

    with _session(session_factory) as session:
        positions = rebuild_position_aggregates(session)
        duels = rebuild_duel_aggregates(session)
        roles = update_player_roles(session)
    return {"positions_agg": positions, "duels_agg": duels, "roles_changed": roles}


def process_demos(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    limit: int = 5,
    downloader: Any | None = None,
    parser_factory: Any | None = None,
    now: datetime | None = None,
) -> DemoJobReport:
    """Download + parse pending demos, then rebuild the aggregates when anything new was parsed.

    Without ``downloader`` a :class:`~cs2bot.ingest.demos.BrowserDownloader` on the HLTV browser
    profile is opened for the call (HLTV serves demos behind the same Cloudflare check as its pages).
    """
    from cs2bot.ingest.demos import BrowserDownloader, process_pending_demos

    kwargs: dict[str, Any] = {"limit": limit, "now": now}
    if parser_factory is not None:
        kwargs["parser_factory"] = parser_factory
    if downloader is not None:
        report = process_pending_demos(session_factory, settings, downloader=downloader, **kwargs)
    else:
        with BrowserDownloader.from_settings(settings) as owned:
            report = process_pending_demos(session_factory, settings, downloader=owned, **kwargs)
    out = DemoJobReport(process=report)
    if getattr(report, "parsed", 0):
        out.aggregates = rebuild_aggregates(session_factory)
    return out


# --------------------------------------------------------------------------------------------- odds


def build_odds_source(settings: Settings) -> Any:
    """``BetclicApiSource`` when ``betclic.offering_api_base`` is set, else the browser source."""
    from cs2bot.odds import BetclicApiSource, BetclicBrowserSource

    if settings.betclic.offering_api_base:
        return BetclicApiSource(settings)
    return BetclicBrowserSource(settings)


def collect_odds(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    source: Any | None = None,
    mapper: Any | None = None,
) -> Any:
    """One Betclic collection pass (``odds.collect_once``); returns its ``CollectReport``."""
    from cs2bot.odds import MarketMapper, collect_once

    mapper = mapper or MarketMapper.from_yaml(settings.betclic.market_rules_file)
    owned = source is None
    src = source or build_odds_source(settings)
    try:
        with _session(session_factory) as session:
            return collect_once(session, src, mapper, settings=settings)
    finally:
        if owned and hasattr(src, "close"):
            try:
                src.close()
            except Exception:  # pragma: no cover - best effort
                log.debug("odds source close failed", exc_info=True)


# ----------------------------------------------------------------------------------------- backfill


@dataclass
class BackfillReport:
    since: datetime
    until: datetime
    pandascore: SyncReport | None = None
    hltv: SyncReport | None = None
    resumed_from: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def items(self) -> int:
        return sum(r.items for r in (self.pandascore, self.hltv) if r is not None)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in (self.pandascore, self.hltv) if r is not None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "resumed_from": self.resumed_from.isoformat() if self.resumed_from else None,
            "pandascore": self.pandascore.as_dict() if self.pandascore else None,
            "hltv": self.hltv.as_dict() if self.hltv else None,
            "notes": self.notes,
        }


def _since_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    return datetime.combine(value, time(), tzinfo=UTC)


def backfill(
    session_factory: sessionmaker[Session],
    settings: Settings,
    since: date | datetime,
    *,
    until: datetime | None = None,
    pandascore_client: Any | None = None,
    hltv_fetcher: Any | None = None,
    use_pandascore: bool | None = None,
    use_hltv: bool = True,
    resume: bool = True,
    commit_every: int = 20,
    max_pages: int = 10_000,
) -> BackfillReport:
    """Historical ingestion from ``since`` (default CLI value: the CS2 release), slow and polite.

    Resumable: PandaScore progress (``completed_until`` of ``sync_past``, committed every
    ``commit_every`` matches) is stored in ``system_state["backfill"]`` and a restarted backfill with
    the same ``since`` continues from there. The HLTV results walk is newest first and skips matches
    that are already complete without fetching them again (its pages are cached), so a restart only
    re-reads listing pages. A block by HLTV stops the walk; run the command again later.
    ``use_pandascore=None`` uses PandaScore only when a token is configured.
    """
    from cs2bot.ingest.hltv import HLTVSyncOptions, sync_results
    from cs2bot.ingest.pandascore import PandaScoreSyncOptions, sync_past

    start = _since_datetime(since)
    end = ensure_utc(until) if until is not None else utcnow()
    report = BackfillReport(since=start, until=end)
    if use_pandascore is None:
        use_pandascore = pandascore_client is not None or settings.pandascore.api_token is not None
    with session_factory() as session:
        progress = db_state.get_state(session, BACKFILL_STATE_KEY)
    ps_from = start
    if resume and progress.get("since") == start.isoformat() and progress.get("pandascore_completed_until"):
        try:
            ps_from = max(start, ensure_utc(datetime.fromisoformat(progress["pandascore_completed_until"])))
            report.resumed_from = ps_from
        except ValueError:
            ps_from = start
    if use_pandascore:
        with _pandascore_client(settings, pandascore_client) as c, _session(session_factory) as session:
            ps = sync_past(
                session, c, settings, ps_from, end, options=PandaScoreSyncOptions(commit_every=commit_every)
            )
            report.pandascore = ps
            done = ps.details.get("completed_until")
            db_state.set_state(
                session,
                BACKFILL_STATE_KEY,
                {
                    "since": start.isoformat(),
                    "pandascore_completed_until": done,
                    "updated_at": utcnow().isoformat(),
                },
                actor="backfill",
            )
    else:
        report.notes.append("PandaScore skipped (no CS2BOT_PANDASCORE__API_TOKEN)")
    if use_hltv:
        with _hltv_fetcher(settings, hltv_fetcher) as f, _session(session_factory) as session:
            hl = sync_results(
                session,
                f,
                settings=settings,
                since=start,
                max_pages=max_pages,
                stop_when_nothing_new=False,
                options=HLTVSyncOptions(commit_every=commit_every),
            )
            report.hltv = hl
            if hl.blocked:
                report.notes.append("HLTV blocked the backfill: run it again later (progress is kept)")
    return report


# ------------------------------------------------------------------------------------------ helpers


class JobFailedError(RuntimeError):
    """A job did nothing and its report has errors (recorded as FAILED, so the scheduler backs off)."""


def report_to_tracker(tracker: Any, report: ReportLike, *, fail_when_empty: bool = True) -> None:
    """Copy a report into a :class:`~cs2bot.db.jobs.JobTracker` (items, details, PARTIAL/FAILED).

    A report that is not ok marks the run PARTIAL; with ``fail_when_empty`` a not-ok report that did
    nothing at all raises :class:`JobFailedError` so the run is FAILED and the scheduler backs off
    (e.g. HLTV blocking us).
    """
    details = report.as_dict()
    tracker.add(int(report.items))
    tracker.details = details
    if not report.ok:
        errors = details.get("errors") or []
        message = "; ".join(str(e) for e in list(errors)[:3]) or (
            "source blocked" if details.get("blocked") else "completed with problems"
        )
        if fail_when_empty and report.items == 0:
            raise JobFailedError(message[:1000])
        tracker.partial_error = message[:1000]
