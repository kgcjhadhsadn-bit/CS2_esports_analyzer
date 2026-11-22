"""Tests for the built-in scheduler and the thread-bound worker."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select

from cs2bot.db.models import JobRun
from cs2bot.domain.enums import JobStatus
from cs2bot.flows.scheduler import Daily, Interval, Job, Scheduler, last_run_times, recent_failures
from cs2bot.flows.worker import ThreadWorker


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def test_interval_trigger() -> None:
    t0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
    trig = Interval(timedelta(minutes=3))
    assert trig.next_due(None, t0) == t0
    assert trig.next_due(t0, t0) == t0 + timedelta(minutes=3)


def test_daily_trigger_catch_up_and_next_slot() -> None:
    trig = Daily(time(5, 0), tz="Europe/Warsaw")  # 05:00 Warsaw == 04:00 UTC in winter
    now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    # never ran -> today's 04:00 UTC slot already passed -> due now (catch-up)
    assert trig.next_due(None, now) == now
    # ran after today's slot -> next slot is tomorrow 04:00 UTC
    assert trig.next_due(datetime(2026, 1, 10, 4, 1, tzinfo=UTC), now) == datetime(2026, 1, 11, 4, tzinfo=UTC)
    # last ran two days ago (PC was off) -> due now, once
    assert trig.next_due(datetime(2026, 1, 8, 4, 0, tzinfo=UTC), now) == now
    # before today's slot, ran yesterday after its slot -> today's slot
    early = datetime(2026, 1, 10, 2, 0, tzinfo=UTC)
    assert trig.next_due(datetime(2026, 1, 9, 4, 30, tzinfo=UTC), early) == datetime(
        2026, 1, 10, 4, tzinfo=UTC
    )


def test_daily_trigger_dst() -> None:
    trig = Daily(time(5, 0), tz="Europe/Warsaw")
    summer = datetime(2026, 7, 1, 12, tzinfo=UTC)  # CEST: 05:00 local == 03:00 UTC
    assert trig.next_due(datetime(2026, 7, 1, 3, 30, tzinfo=UTC), summer) == datetime(
        2026, 7, 2, 3, tzinfo=UTC
    )


def test_scheduler_runs_due_jobs_records_and_backs_off(session_factory) -> None:
    clock = FakeClock(datetime(2026, 3, 1, 10, tzinfo=UTC))
    calls: list[str] = []

    def ok(tracker) -> None:
        calls.append("ok")
        tracker.add(2)

    def boom(tracker) -> None:
        calls.append("boom")
        raise RuntimeError("fail")

    jobs = [Job("ok", ok, Interval(timedelta(minutes=5))), Job("boom", boom, Interval(timedelta(minutes=1)))]
    sch = Scheduler(jobs, session_factory, clock=clock)
    sch.restore_state()
    assert sch.run_lane_once("fast") == 2
    assert calls == ["ok", "boom"]
    # nothing due right after
    assert sch.run_lane_once("fast") == 0
    # after 1 minute: interval says boom is due, but backoff (30 s after 1 failure) is shorter -> due
    clock.advance(minutes=1)
    assert [j.name for j in sch.due_jobs("fast")] == ["boom"]
    sch.run_lane_once("fast")
    # two failures -> backoff 60 s; ok job due at +5 min
    clock.advance(minutes=4)
    names = [j.name for j in sch.due_jobs("fast")]
    assert "ok" in names
    with session_factory() as s:
        rows = list(s.scalars(select(JobRun).order_by(JobRun.id)))
    assert [r.job_name for r in rows] == ["ok", "boom", "boom"]
    assert rows[0].status is JobStatus.SUCCESS and rows[0].items == 2
    assert rows[1].status is JobStatus.FAILED
    assert recent_failures(session_factory, "boom") == 2


def test_backoff_capped(session_factory) -> None:
    clock = FakeClock(datetime(2026, 3, 1, 10, tzinfo=UTC))
    job = Job("x", lambda t: None, Interval(timedelta(seconds=10)), max_backoff=timedelta(minutes=2))
    sch = Scheduler([job], session_factory, clock=clock)
    job.last_run = clock.now
    job.failures = 20
    assert sch._compute_next_due(job, clock.now) == clock.now + timedelta(minutes=2)


def test_catch_up_after_restart(session_factory) -> None:
    """A job that last ran yesterday (PC was off) is due immediately after a restart, exactly once."""
    t_old = datetime(2026, 3, 1, 8, tzinfo=UTC)
    with session_factory() as s:
        s.add(JobRun(job_name="odds", status=JobStatus.SUCCESS, started_at=t_old, finished_at=t_old))
        s.commit()
    assert last_run_times(session_factory, ["odds", "other"]) == {"odds": t_old}
    clock = FakeClock(datetime(2026, 3, 2, 9, tzinfo=UTC))
    ran: list[int] = []
    sch = Scheduler(
        [Job("odds", lambda t: ran.append(1), Interval(timedelta(minutes=3)))], session_factory, clock=clock
    )
    sch.restore_state()
    assert sch.run_lane_once("fast") == 1
    assert sch.run_lane_once("fast") == 0  # coalesced: not one run per missed 3-minute slot
    assert ran == [1]


def test_lanes_run_in_threads(session_factory) -> None:
    started = threading.Event()
    release = threading.Event()
    fast_ran = threading.Event()

    def slow(tracker) -> None:
        started.set()
        release.wait(5)

    def fast(tracker) -> None:
        fast_ran.set()

    jobs = [
        Job("slow", slow, Interval(timedelta(hours=1)), lane="slow"),
        Job("fast", fast, Interval(timedelta(hours=1)), lane="fast"),
    ]
    sch = Scheduler(jobs, session_factory, tick_s=0.05)
    sch.start()
    try:
        assert started.wait(5)
        assert fast_ran.wait(5), "fast lane must not be blocked by the slow lane"
    finally:
        release.set()
        sch.stop()
    assert sch.stopped


def test_duplicate_job_names_rejected(session_factory) -> None:
    with pytest.raises(ValueError):
        Scheduler(
            [
                Job("a", lambda t: None, Interval(timedelta(1))),
                Job("a", lambda t: None, Interval(timedelta(1))),
            ],
            session_factory,
        )


def test_thread_worker_owns_resource_in_one_thread() -> None:
    created_in: list[str] = []
    closed: list[bool] = []

    def factory() -> dict[str, str]:
        created_in.append(threading.current_thread().name)
        return {"thread": threading.current_thread().name}

    w: ThreadWorker[dict[str, str]] = ThreadWorker(
        factory, name="wtest", closer=lambda r: closed.append(True)
    )
    assert w.call(lambda r: r["thread"]) == "wtest"
    assert w.call(lambda r: threading.current_thread().name) == "wtest"
    with pytest.raises(ZeroDivisionError):
        w.call(lambda r: 1 / 0)
    assert w.call(lambda r: "still alive") == "still alive"
    w.reset()
    w.call(lambda r: None)
    assert created_in == ["wtest", "wtest"]
    w.stop()
    assert closed == [True, True]
