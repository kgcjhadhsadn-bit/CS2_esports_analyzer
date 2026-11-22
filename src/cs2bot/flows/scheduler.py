"""Built-in job scheduler used by ``cs2bot up``.

Why not cron/Prefect: everything runs on the owner's PC in one process, and the PC is not always on.
This scheduler:

* runs **interval** jobs (e.g. odds every 3 minutes) and **daily** jobs (e.g. nightly retrain at a local
  Warsaw time),
* records every run in ``job_runs`` (via :func:`cs2bot.db.jobs.track_job`), and derives "when did this job
  last run" from that table at start-up, so jobs that were missed while the PC was off run **once**
  right away (coalesced catch-up), not once per missed slot,
* backs off exponentially after failures (never faster than the job's own interval allows),
* runs jobs in **lanes**: each lane is a thread executing its jobs one at a time, so a slow backfill in the
  ``slow`` lane never delays odds collection or betting in the ``fast`` lane. Jobs that use thread-bound
  resources (Playwright) must keep those resources inside their lane's thread (or use
  :class:`cs2bot.flows.worker.ThreadWorker`).

The scheduler is deliberately small and fully testable: time and sleeping are injectable.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.db.jobs import JobTracker, track_job
from cs2bot.db.models import JobRun
from cs2bot.domain.enums import JobStatus
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

JobFunc = Callable[[JobTracker], None]


class Trigger(Protocol):
    def next_due(self, last_run: datetime | None, now: datetime) -> datetime:
        """When the job should next run, given the last run start (UTC) and the current time (UTC)."""
        ...


@dataclass(frozen=True)
class Interval:
    """Run every ``every``; a job that never ran (or ran long ago) is due immediately."""

    every: timedelta

    def next_due(self, last_run: datetime | None, now: datetime) -> datetime:
        if last_run is None:
            return now
        return last_run + self.every


@dataclass(frozen=True)
class Daily:
    """Run once per local day at ``at`` (local time in ``tz``).

    If the most recent scheduled slot (today's or yesterday's) passed without a run - e.g. the PC was
    off at 05:00 - the job is due immediately (catch-up), once.
    """

    at: time
    tz: str = "Europe/Warsaw"

    def _slot(self, day_local: datetime) -> datetime:
        zone = ZoneInfo(self.tz)
        local = datetime.combine(day_local.date(), self.at, tzinfo=zone)
        return ensure_utc(local)

    def next_due(self, last_run: datetime | None, now: datetime) -> datetime:
        zone = ZoneInfo(self.tz)
        local_now = now.astimezone(zone)
        today_slot = self._slot(local_now)
        latest_slot = today_slot if today_slot <= now else self._slot(local_now - timedelta(days=1))
        if last_run is None or last_run < latest_slot:
            return now if latest_slot <= now else latest_slot
        return self._slot(local_now + timedelta(days=1)) if today_slot <= now else today_slot


@dataclass
class Job:
    name: str
    func: JobFunc
    trigger: Trigger
    lane: str = "fast"
    enabled: bool = True
    max_backoff: timedelta = timedelta(hours=1)
    # Run-time state (not configuration)
    last_run: datetime | None = field(default=None, repr=False)
    failures: int = field(default=0, repr=False)
    next_due_at: datetime | None = field(default=None, repr=False)


def last_run_times(factory: sessionmaker[Session], names: list[str]) -> dict[str, datetime]:
    """Most recent start time per job from ``job_runs`` (any status), used for catch-up after restarts."""
    out: dict[str, datetime] = {}
    with factory() as s:
        for name in names:
            row = s.scalar(
                select(JobRun).where(JobRun.job_name == name).order_by(JobRun.started_at.desc()).limit(1)
            )
            if row is not None:
                out[name] = row.started_at
    return out


def recent_failures(factory: sessionmaker[Session], name: str, *, limit: int = 10) -> int:
    """Consecutive failures at the end of the job's history (restores the backoff after a restart)."""
    with factory() as s:
        rows = list(
            s.scalars(
                select(JobRun).where(JobRun.job_name == name).order_by(JobRun.started_at.desc()).limit(limit)
            )
        )
    count = 0
    for row in rows:
        if row.status is JobStatus.FAILED:
            count += 1
        else:
            break
    return count


class Scheduler:
    """Lane-based scheduler. ``run_forever`` blocks until ``stop()`` is called."""

    def __init__(
        self,
        jobs: list[Job],
        factory: sessionmaker[Session],
        *,
        tick_s: float = 5.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        names = [j.name for j in jobs]
        if len(set(names)) != len(names):
            raise ValueError("job names must be unique")
        self.jobs = jobs
        self.factory = factory
        self.tick_s = tick_s
        self.clock = clock
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ state
    def restore_state(self) -> None:
        """Load last run times and failure streaks from the DB (call once at start-up)."""
        last = last_run_times(self.factory, [j.name for j in self.jobs])
        now = self.clock()
        for job in self.jobs:
            job.last_run = last.get(job.name)
            job.failures = recent_failures(self.factory, job.name)
            job.next_due_at = self._compute_next_due(job, now)
            if job.last_run is not None and job.next_due_at <= now:
                log.info("job %s missed while offline (last run %s) - catching up", job.name, job.last_run)

    def _compute_next_due(self, job: Job, now: datetime) -> datetime:
        due = job.trigger.next_due(job.last_run, now)
        if job.failures and job.last_run is not None:
            backoff = min(timedelta(seconds=30 * (2 ** min(job.failures - 1, 10))), job.max_backoff)
            due = max(due, job.last_run + backoff)
        return due

    def due_jobs(self, lane: str, now: datetime | None = None) -> list[Job]:
        now = now or self.clock()
        with self._lock:
            return [
                j
                for j in self.jobs
                if j.enabled and j.lane == lane and j.next_due_at is not None and j.next_due_at <= now
            ]

    # ------------------------------------------------------------------ running
    def run_job(self, job: Job) -> bool:
        """Run one job now (records a job_runs row). Returns True on success."""
        started = self.clock()
        ok = True
        try:
            with track_job(job.name, self.factory) as tracker:
                job.func(tracker)
        except Exception:
            ok = False
            log.exception("job %s failed", job.name)
        with self._lock:
            job.last_run = started
            job.failures = 0 if ok else job.failures + 1
            job.next_due_at = self._compute_next_due(job, self.clock())
        return ok

    def run_lane_once(self, lane: str) -> int:
        """Run every currently-due job of a lane once (in definition order). Returns jobs run."""
        ran = 0
        for job in self.due_jobs(lane):
            if self._stop.is_set():
                break
            self.run_job(job)
            ran += 1
        return ran

    def _lane_loop(self, lane: str) -> None:
        while not self._stop.is_set():
            try:
                self.run_lane_once(lane)
            except Exception:  # pragma: no cover - run_job already isolates job errors
                log.exception("lane %s crashed; continuing", lane)
            self._stop.wait(self.tick_s)

    def start(self) -> None:
        """Restore state and start one thread per lane."""
        self.restore_state()
        lanes = sorted({j.lane for j in self.jobs})
        for lane in lanes:
            t = threading.Thread(
                target=self._lane_loop, args=(lane,), name=f"cs2bot-lane-{lane}", daemon=True
            )
            t.start()
            self._threads.append(t)
        log.info("scheduler started: %d jobs in lanes %s", len(self.jobs), lanes)

    def stop(self, timeout_s: float = 30.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout_s)
        self._threads.clear()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(1.0):
                pass
        finally:
            self.stop()

    def status(self) -> list[dict[str, object]]:
        """Snapshot for the dashboard / logs."""
        with self._lock:
            return [
                {
                    "name": j.name,
                    "lane": j.lane,
                    "enabled": j.enabled,
                    "last_run": j.last_run.isoformat() if j.last_run else None,
                    "next_due": j.next_due_at.isoformat() if j.next_due_at else None,
                    "failures": j.failures,
                }
                for j in self.jobs
            ]
