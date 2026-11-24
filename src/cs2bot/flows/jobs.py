"""The jobs ``cs2bot up`` schedules: what runs, in which lane, how often.

Lanes (one thread each, jobs of a lane run one at a time in definition order):

* ``fast``  - money path: ``odds_collect`` -> ``predict_upcoming`` -> ``bet_run`` -> ``settle_bets``
  (+ ``executor_health``). Odds every ``scheduler.odds_poll_s``; the betting cycle runs right after
  each odds pass so it sees fresh quotes.
* ``sync``  - HTTP-only ingestion: PandaScore fixtures / results, Claude news triage, scope refresh.
* ``hltv``  - everything that drives the HLTV browser profile (pages, rankings, news, demo downloads),
  so that profile is only ever opened by one thread at a time (Playwright is thread-bound).
* ``train`` - the nightly retrain (``Daily`` at ``scheduler.retrain_cron``), so a long fit never
  delays odds or HLTV syncs.
* ``notify`` - Telegram notifications (``telegram_notify``, new events from the database) and the daily
  P&L report (``telegram_daily_report`` at ``telegram.daily_report_time``, Warsaw).
* ``telegram`` - the Telegram command poller (``telegram_commands``: one long-polling session of
  ``telegram.poll_session_s``, then the next run starts at once).

Cloud autonomy: ``autonomy_check`` (fast lane, after settlement so UNCERTAIN bets are resolved first)
lifts automatic pauses once their cause is resolved (:mod:`cs2bot.services.autonomy`).

A failing job never stops the others: the scheduler isolates every run, records it in ``job_runs``
and backs off. Job names contain the keywords the dashboard uses to find each job's expected
interval (odds, predict, bet, settle, upcoming, result, news, ranking, demo, retrain).
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings
from cs2bot.db.jobs import JobTracker
from cs2bot.flows.scheduler import Daily, Interval, Job, Trigger
from cs2bot.services import ingestion
from cs2bot.services.autonomy import AutonomyService
from cs2bot.services.health import HealthCache, refresh_executor_health
from cs2bot.services.registry import ModelRegistry, NotEnoughDataError

log = logging.getLogger(__name__)

FAST, SYNC, HLTV, TRAIN, NOTIFY, TELEGRAM = "fast", "sync", "hltv", "train", "notify", "telegram"
DEFAULT_RETRAIN_AT = time(5, 0)
EXECUTOR_HEALTH_EVERY = timedelta(minutes=15)
RANKINGS_EVERY = timedelta(hours=12)


def parse_daily_cron(expr: str, tz: str) -> Daily:
    """``"M H * * *"`` -> :class:`Daily` at H:M local time; anything else falls back to 05:00.

    Only the daily form is supported (the scheduler has no general cron engine); the fallback is
    logged so a typo in ``CS2BOT_SCHEDULER__RETRAIN_CRON`` is visible.
    """
    match = re.fullmatch(r"\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*", expr or "")
    if match:
        minute, hour = int(match.group(1)), int(match.group(2))
        if 0 <= minute < 60 and 0 <= hour < 24:
            return Daily(time(hour, minute), tz=tz)
    log.warning("unsupported retrain_cron %r (only 'M H * * *'); using 05:00", expr)
    return Daily(DEFAULT_RETRAIN_AT, tz=tz)


@dataclass
class Runtime:
    """Everything the jobs share inside one ``cs2bot up`` process.

    ``executor`` is a :class:`~cs2bot.executor.BetExecutor` (``None``: decisions only). ``analyzer`` is a
    :class:`~cs2bot.llm.ContextAnalyzer` or None. The factories create sources lazily inside the job's
    own lane thread (``odds_source_factory`` once, reused; the HLTV / PandaScore / demo ones per job
    when given, closed again after the job, else the ingestion service opens and closes its own).
    ``stop_event`` is set by ``run_up`` at shutdown; the betting cycle checks it before every placement.
    """

    settings: Settings
    session_factory: sessionmaker[Session]
    registry: ModelRegistry
    executor: Any | None = None
    analyzer: Any | None = None
    health_cache: HealthCache = field(default_factory=HealthCache)
    odds_source_factory: Callable[[], Any] | None = None
    hltv_fetcher_factory: Callable[[], Any] | None = None
    pandascore_client_factory: Callable[[], Any] | None = None
    demo_downloader_factory: Callable[[], Any] | None = None
    mapper: Any | None = None
    hltv_enabled: bool = True
    demo_limit: int = 5
    #: Set when ``cs2bot up`` shuts down: a running betting cycle places nothing more.
    stop_event: threading.Event = field(default_factory=threading.Event)
    #: :class:`~cs2bot.services.telegram.TelegramClient` (None: Telegram off / not configured).
    telegram: Any | None = None
    _odds_source: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def odds_source(self) -> Any:
        with self._lock:
            if self._odds_source is None:
                factory = self.odds_source_factory or (lambda: ingestion.build_odds_source(self.settings))
                self._odds_source = factory()
            return self._odds_source

    def market_mapper(self) -> Any:
        with self._lock:
            if self.mapper is None:
                from cs2bot.odds import MarketMapper

                self.mapper = MarketMapper.from_yaml(self.settings.betclic.market_rules_file)
            return self.mapper

    @property
    def llm_ready(self) -> bool:
        return self.analyzer is not None and bool(getattr(self.analyzer, "enabled", False))

    @property
    def pandascore_ready(self) -> bool:
        return self.pandascore_client_factory is not None or self.settings.pandascore.api_token is not None

    def close(self) -> None:
        """Close what the runtime created (the odds source); the caller closes executor/analyzer."""
        with self._lock:
            src, self._odds_source = self._odds_source, None
        if src is not None and hasattr(src, "close"):
            try:
                src.close()
            except Exception:
                log.warning("could not close the odds source", exc_info=True)


def _maybe(factory: Callable[[], Any] | None) -> Any | None:
    return factory() if factory is not None else None


def _close_quietly(obj: Any) -> None:
    if obj is not None and hasattr(obj, "close"):
        try:
            obj.close()
        except Exception:
            log.warning("could not close %s", type(obj).__name__, exc_info=True)


def _with_source(
    factory: Callable[[], Any] | None, run: Callable[[Any | None], ingestion.ReportLike]
) -> ingestion.ReportLike:
    """Create a source from ``factory`` in the calling (lane) thread, run, and close it again.

    An injected source is not closed by the ingestion services (they only close what they open), so
    a source made here per job would otherwise leak a browser / HTTP client on every run.
    """
    source = _maybe(factory)
    try:
        return run(source)
    finally:
        _close_quietly(source)


# ------------------------------------------------------------------------------------------ job bodies


def _odds_collect(rt: Runtime, tracker: JobTracker) -> None:
    report = ingestion.collect_odds(
        rt.session_factory, rt.settings, source=rt.odds_source(), mapper=rt.market_mapper()
    )
    tracker.add(int(getattr(report, "snapshots_written", 0)))
    tracker.details = report.model_dump(mode="json")
    if not report.ok:
        tracker.partial_error = "; ".join(report.errors[:3])[:1000] or "odds collection had errors"


def _predict(rt: Runtime, tracker: JobTracker) -> None:
    from cs2bot.services.prediction import predict_upcoming

    bundle = rt.registry.get()
    if bundle is None:
        tracker.details = {"skipped": "no trained model yet (cs2bot models train)"}
        return
    analyzer = rt.analyzer if rt.llm_ready else None
    report = predict_upcoming(rt.session_factory, rt.settings, bundle, analyzer=analyzer)
    tracker.add(report.items)
    tracker.details = report.as_dict()
    if report.errors:
        tracker.partial_error = "; ".join(report.errors[:3])[:1000]


def _bet(rt: Runtime, tracker: JobTracker) -> None:
    from cs2bot.services.betting import run_betting_cycle

    bundle = rt.registry.get()
    if bundle is None:
        tracker.details = {"skipped": "no trained model yet"}
        return
    analyzer = rt.analyzer if rt.llm_ready else None
    report = run_betting_cycle(
        rt.session_factory,
        rt.settings,
        rt.executor,
        bundle,
        analyzer=analyzer,
        should_stop=rt.stop_event.is_set,
    )
    tracker.add(report.items)
    tracker.details = report.as_dict()
    if report.errors:
        tracker.partial_error = "; ".join(report.errors[:3])[:1000]


def _settle(rt: Runtime, tracker: JobTracker) -> None:
    from cs2bot.services.settlement import settle_bets

    # the executor_health job keeps the health fresh; settlement does not open the account page twice
    report = settle_bets(
        rt.session_factory, rt.settings, rt.executor, health_cache=rt.health_cache, check_health=False
    )
    tracker.add(report.items)
    tracker.details = report.as_dict()
    if report.errors:
        tracker.partial_error = "; ".join(report.errors[:3])[:1000]


def _executor_health(rt: Runtime, tracker: JobTracker) -> None:
    if rt.executor is None:
        tracker.details = {"skipped": "no executor"}
        return
    health = refresh_executor_health(rt.session_factory, rt.executor, rt.health_cache)
    tracker.details = health.model_dump(mode="json")
    if not health.ok:
        tracker.partial_error = (health.message or "executor not healthy")[:1000]


def _autonomy(rt: Runtime, tracker: JobTracker) -> None:
    service = AutonomyService(rt.session_factory, rt.settings, rt.executor, health_cache=rt.health_cache)
    report = service.run_once()
    tracker.add(report.items)
    tracker.details = report.as_dict()
    if report.errors:
        tracker.partial_error = "; ".join(report.errors[:3])[:1000]


def _telegram_notify(rt: Runtime, tracker: JobTracker) -> None:
    from cs2bot.services.telegram import TelegramNotifier

    if rt.telegram is None:
        tracker.details = {"skipped": "telegram not configured"}
        return
    sent = TelegramNotifier(rt.session_factory, rt.settings, rt.telegram).scan_once()
    tracker.add(sent)
    tracker.details = {"sent": sent, **rt.telegram.status()}
    if rt.telegram.last_error:
        tracker.partial_error = f"telegram: {rt.telegram.last_error}"[:1000]


def _telegram_daily_report(rt: Runtime, tracker: JobTracker) -> None:
    from cs2bot.services.telegram import TelegramNotifier

    if rt.telegram is None:
        tracker.details = {"skipped": "telegram not configured"}
        return
    ok = TelegramNotifier(rt.session_factory, rt.settings, rt.telegram).daily_report()
    tracker.add(1 if ok else 0)
    if not ok:
        tracker.partial_error = f"telegram: {rt.telegram.last_error or 'not sent'}"[:1000]


def _telegram_commands(rt: Runtime, tracker: JobTracker) -> None:
    from cs2bot.services.telegram import TelegramCommands

    if rt.telegram is None:
        tracker.details = {"skipped": "telegram not configured"}
        return
    commands = TelegramCommands(rt.session_factory, rt.settings, rt.telegram)
    handled = commands.poll(
        should_stop=rt.stop_event.is_set,
        session_s=rt.settings.telegram.poll_session_s,
        sleep=rt.stop_event.wait,
    )
    tracker.add(handled)
    tracker.details = {"handled": handled}


def _sync_job(fn: Callable[[], ingestion.ReportLike]) -> Callable[[JobTracker], None]:
    def run(tracker: JobTracker) -> None:
        ingestion.report_to_tracker(tracker, fn())

    return run


def _scope(rt: Runtime, tracker: JobTracker) -> None:
    changed = ingestion.refresh_scope(rt.session_factory, rt.settings)
    tracker.add(changed)
    tracker.details = {"in_scope_changed": changed}


def _triage(rt: Runtime, tracker: JobTracker) -> None:
    report = ingestion.triage_news(rt.session_factory, rt.analyzer)
    tracker.add(int(report.triaged))
    tracker.details = {
        "status": str(getattr(report.status, "value", report.status)),
        "candidates": report.candidates,
        "triaged": report.triaged,
        "unanswered": report.unanswered,
        "deferred": report.deferred,
    }


def _retrain(rt: Runtime, tracker: JobTracker) -> None:
    try:
        report = rt.registry.retrain(rt.session_factory)
    except NotEnoughDataError as exc:
        tracker.partial_error = str(exc)
        return
    tracker.add(report.n_map_results)
    tracker.details = report.as_dict()
    if report.warnings:
        tracker.details["warnings"] = report.warnings


def _demos(rt: Runtime, tracker: JobTracker) -> None:
    report = _with_source(
        rt.demo_downloader_factory,
        lambda downloader: ingestion.process_demos(
            rt.session_factory, rt.settings, limit=rt.demo_limit, downloader=downloader
        ),
    )
    ingestion.report_to_tracker(tracker, report, fail_when_empty=False)


# ----------------------------------------------------------------------------------------------- build


def build_jobs(rt: Runtime) -> list[Job]:
    """All jobs of ``cs2bot up`` with intervals from ``settings.scheduler`` (see the module docstring)."""
    s = rt.settings
    sch = s.scheduler
    tz = s.timezone

    def every(seconds: float) -> Trigger:
        return Interval(timedelta(seconds=max(seconds, 1)))

    def minutes(m: float) -> Trigger:
        return every(m * 60)

    ps_client = rt.pandascore_client_factory
    hltv_fetcher = rt.hltv_fetcher_factory

    def ps(fn: Callable[..., ingestion.ReportLike]) -> Callable[[JobTracker], None]:
        return _sync_job(
            lambda: _with_source(ps_client, lambda client: fn(rt.session_factory, s, client=client))
        )

    def hl(fn: Callable[..., ingestion.ReportLike]) -> Callable[[JobTracker], None]:
        return _sync_job(
            lambda: _with_source(hltv_fetcher, lambda fetcher: fn(rt.session_factory, s, fetcher=fetcher))
        )

    hltv_on = rt.hltv_enabled
    return [
        # fast lane: the money path, in this order
        Job("odds_collect", lambda t: _odds_collect(rt, t), every(sch.odds_poll_s), lane=FAST),
        Job("predict_upcoming", lambda t: _predict(rt, t), minutes(sch.predict_min), lane=FAST),
        Job("bet_run", lambda t: _bet(rt, t), every(sch.odds_poll_s), lane=FAST),
        Job("settle_bets", lambda t: _settle(rt, t), minutes(sch.settle_min), lane=FAST),
        Job(
            "executor_health",
            lambda t: _executor_health(rt, t),
            Interval(EXECUTOR_HEALTH_EVERY),
            lane=FAST,
            enabled=rt.executor is not None,
        ),
        Job(
            "autonomy_check",
            lambda t: _autonomy(rt, t),
            minutes(s.autonomy.check_interval_min),
            lane=FAST,
            enabled=s.autonomy.enabled,
        ),
        # notify / telegram lanes: never in the money path
        Job(
            "telegram_notify",
            lambda t: _telegram_notify(rt, t),
            every(s.telegram.notify_interval_s),
            lane=NOTIFY,
            enabled=rt.telegram is not None,
        ),
        Job(
            "telegram_daily_report",
            lambda t: _telegram_daily_report(rt, t),
            Daily(s.telegram.daily_report_time, tz=tz),
            lane=NOTIFY,
            enabled=rt.telegram is not None and s.telegram.daily_report,
        ),
        Job(
            "telegram_commands",
            lambda t: _telegram_commands(rt, t),
            every(1),
            lane=TELEGRAM,
            enabled=rt.telegram is not None and s.telegram.commands_enabled,
        ),
        # sync lane: HTTP APIs and database-only work
        Job(
            "ingest_upcoming_pandascore",
            ps(ingestion.pandascore_upcoming),
            minutes(sch.upcoming_sync_min),
            lane=SYNC,
            enabled=rt.pandascore_ready,
        ),
        Job(
            "ingest_results_pandascore",
            ps(ingestion.pandascore_results),
            minutes(sch.results_sync_min),
            lane=SYNC,
            enabled=rt.pandascore_ready,
        ),
        Job("scope_refresh_upcoming", lambda t: _scope(rt, t), minutes(sch.upcoming_sync_min), lane=SYNC),
        Job(
            "news_triage",
            lambda t: _triage(rt, t),
            minutes(sch.news_poll_min),
            lane=SYNC,
            enabled=rt.llm_ready,
        ),
        # hltv lane: one browser profile, one thread
        Job(
            "ingest_upcoming_hltv",
            hl(ingestion.hltv_upcoming),
            minutes(sch.upcoming_sync_min),
            lane=HLTV,
            enabled=hltv_on,
        ),
        Job(
            "ingest_results_hltv",
            hl(ingestion.hltv_results),
            minutes(sch.results_sync_min),
            lane=HLTV,
            enabled=hltv_on,
        ),
        Job(
            "ingest_rankings_hltv",
            hl(ingestion.hltv_rankings),
            Interval(RANKINGS_EVERY),
            lane=HLTV,
            enabled=hltv_on,
        ),
        Job(
            "ingest_news_hltv",
            hl(ingestion.hltv_news),
            minutes(sch.news_poll_min),
            lane=HLTV,
            enabled=hltv_on,
        ),
        Job(
            "demos_process",
            lambda t: _demos(rt, t),
            minutes(sch.demo_process_min),
            lane=HLTV,
            enabled=hltv_on,
        ),
        # train lane
        Job("models_retrain", lambda t: _retrain(rt, t), parse_daily_cron(sch.retrain_cron, tz), lane=TRAIN),
    ]
