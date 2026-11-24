"""Scheduler job wiring: names, lanes, enabled flags, one fast-lane pass against the synthetic world,
failure isolation and the ingestion report -> job_runs mapping."""

from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

import cs2bot.db.models as M
from cs2bot.config import Settings
from cs2bot.db.jobs import JobTracker
from cs2bot.domain.enums import BetStatus, JobStatus
from cs2bot.domain.sync import SyncReport
from cs2bot.executor import FakeExecutor
from cs2bot.flows.jobs import FAST, HLTV, NOTIFY, SYNC, TELEGRAM, TRAIN, Runtime, build_jobs, parse_daily_cron
from cs2bot.flows.scheduler import Daily, Scheduler
from cs2bot.ingest.hltv import HLTVBlockedError
from cs2bot.odds import StaticOddsSource
from cs2bot.services import ingestion
from cs2bot.services.registry import ModelRegistry
from tests.services.conftest import World
from tests.services.test_services_e2e import prepare_market


def _runtime(world: World, **kw: Any) -> Runtime:
    base: dict[str, Any] = {
        "settings": world.settings,
        "session_factory": world.factory,
        "registry": ModelRegistry(world.settings, world.bundle),
        "odds_source_factory": lambda: StaticOddsSource([]),
    }
    base.update(kw)
    return Runtime(**base)


def _runs(world: World) -> dict[str, list[M.JobRun]]:
    out: dict[str, list[M.JobRun]] = {}
    with world.factory() as s:
        for row in s.scalars(select(M.JobRun).order_by(M.JobRun.id)):
            out.setdefault(row.job_name, []).append(row)
    return out


def test_parse_daily_cron() -> None:
    assert parse_daily_cron("0 5 * * *", "Europe/Warsaw") == Daily(time(5, 0), tz="Europe/Warsaw")
    assert parse_daily_cron("30 23 * * *", "UTC").at == time(23, 30)
    assert parse_daily_cron("*/5 * * * *", "UTC").at == time(5, 0)  # unsupported -> default
    assert parse_daily_cron("99 5 * * *", "UTC").at == time(5, 0)


def test_job_names_lanes_and_enabled_flags(world: World) -> None:
    rt = _runtime(world)
    jobs = {j.name: j for j in build_jobs(rt)}
    assert len(jobs) == len(build_jobs(rt))
    fast = [n for n, j in jobs.items() if j.lane == FAST]
    assert fast[:4] == [
        "odds_collect",
        "predict_upcoming",
        "bet_run",
        "settle_bets",
    ]  # the money path, in order
    assert {j.lane for j in jobs.values()} == {FAST, SYNC, HLTV, TRAIN, NOTIFY, TELEGRAM}
    # cloud autonomy and Telegram are off unless configured
    assert not jobs["autonomy_check"].enabled and not jobs["telegram_notify"].enabled
    assert not jobs["telegram_commands"].enabled and not jobs["telegram_daily_report"].enabled
    assert all(jobs[n].lane == HLTV for n in jobs if "hltv" in n or n.startswith("demos"))
    assert isinstance(jobs["models_retrain"].trigger, Daily)
    # no token, no analyzer, no executor -> those jobs are off; the money path is always on
    assert not jobs["ingest_upcoming_pandascore"].enabled and not jobs["news_triage"].enabled
    assert not jobs["executor_health"].enabled
    assert jobs["bet_run"].enabled and jobs["settle_bets"].enabled
    settings: Settings = world.settings.model_copy(deep=True)
    settings.pandascore.api_token = "x"  # type: ignore[assignment]
    rt2 = _runtime(world, settings=settings, executor=FakeExecutor(), hltv_enabled=False)
    jobs2 = {j.name: j for j in build_jobs(rt2)}
    assert jobs2["ingest_upcoming_pandascore"].enabled and jobs2["executor_health"].enabled
    assert not jobs2["ingest_upcoming_hltv"].enabled


def test_fast_lane_pass_predicts_bets_and_settles(world: World) -> None:
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rt = _runtime(world, executor=ex)
    sched = Scheduler(build_jobs(rt), world.factory, tick_s=0.01)
    sched.restore_state()
    ran = sched.run_lane_once(FAST)
    assert ran == 5
    runs = _runs(world)
    for name in ("odds_collect", "predict_upcoming", "bet_run", "settle_bets", "executor_health"):
        assert runs[name][-1].status in (JobStatus.SUCCESS, JobStatus.PARTIAL), (name, runs[name][-1].error)
    assert runs["bet_run"][-1].details["bets_created"] >= 1
    with world.factory() as s:
        rows = list(s.scalars(select(M.Bet)))
        assert rows and all(b.status is BetStatus.DRY_RUN for b in rows)
        assert s.get(M.SystemState, "executor_health") is not None
    assert rt.health_cache.get() is not None
    # nothing is due again right away (intervals), so a second pass runs nothing
    assert sched.run_lane_once(FAST) == 0
    rt.close()


def test_a_failing_job_never_stops_the_others(world: World) -> None:
    def broken_client() -> Any:
        raise RuntimeError("PandaScore down")

    class BlockedFetcher:
        def get(self, path_or_url: str, *, max_age_minutes: int | None = None) -> str:
            raise HLTVBlockedError("challenge", retry_after_s=900)

    settings = world.settings.model_copy(deep=True)
    rt = _runtime(
        world,
        settings=settings,
        pandascore_client_factory=broken_client,
        hltv_fetcher_factory=BlockedFetcher,
    )
    sched = Scheduler(build_jobs(rt), world.factory, tick_s=0.01)
    sched.restore_state()
    assert sched.run_lane_once(SYNC) >= 3
    sched.jobs = [j for j in sched.jobs if j.name != "demos_process"]  # needs a real browser downloader
    assert sched.run_lane_once(HLTV) == 4
    runs = _runs(world)
    assert runs["ingest_upcoming_pandascore"][-1].status is JobStatus.FAILED
    assert "PandaScore down" in (runs["ingest_upcoming_pandascore"][-1].error or "")
    assert runs["scope_refresh_upcoming"][-1].status is JobStatus.SUCCESS  # ran after the failure
    # HLTV blocking us: nothing done -> FAILED (scheduler backs off), details kept
    upcoming = runs["ingest_upcoming_hltv"][-1]
    assert upcoming.status is JobStatus.FAILED and upcoming.details.get("blocked") is True
    failing = next(j for j in sched.jobs if j.name == "ingest_upcoming_hltv")
    assert failing.failures == 1 and failing.next_due_at is not None and failing.last_run is not None
    assert failing.next_due_at >= failing.last_run + timedelta(
        minutes=world.settings.scheduler.upcoming_sync_min
    )


def test_report_to_tracker_maps_ok_partial_and_failed() -> None:
    ok = SyncReport(job="x", created=2)
    t = JobTracker()
    ingestion.report_to_tracker(t, ok)
    assert t.items == 2 and t.partial_error is None and t.details["created"] == 2

    partial = SyncReport(job="x", updated=1, errors=["one bad row"])
    t = JobTracker()
    ingestion.report_to_tracker(t, partial)
    assert t.partial_error == "one bad row"

    nothing = SyncReport(job="x", blocked=True)
    with pytest.raises(ingestion.JobFailedError, match="blocked"):
        ingestion.report_to_tracker(JobTracker(), nothing)
    t = JobTracker()
    ingestion.report_to_tracker(t, nothing, fail_when_empty=False)
    assert t.partial_error == "source blocked"


def test_pandascore_jobs_need_a_token(world: World) -> None:
    with pytest.raises(ingestion.MissingCredentialError, match="CS2BOT_PANDASCORE__API_TOKEN"):
        ingestion.pandascore_upcoming(world.factory, world.settings)


def test_retrain_job_swaps_the_active_bundle(world: World) -> None:
    rt = _runtime(world)
    before = rt.registry.get()
    job = next(j for j in build_jobs(rt) if j.name == "models_retrain")
    sched = Scheduler([job], world.factory, tick_s=0.01)
    assert sched.run_job(job)
    after = rt.registry.get()
    assert after is not None and before is not None and after.version != before.version
    runs = _runs(world)
    assert runs["models_retrain"][-1].status is JobStatus.SUCCESS
    with world.factory() as s:
        assert s.scalar(select(M.ModelRun).where(M.ModelRun.model_version == after.version)) is not None
