"""Record pipeline job runs (shown on the dashboard's health page)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.db.models import JobRun
from cs2bot.db.session import get_session_factory
from cs2bot.domain.enums import JobStatus
from cs2bot.utils.time import utcnow


@dataclass
class JobTracker:
    """Mutable handle yielded by :func:`track_job`."""

    items: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    partial_error: str | None = None

    def add(self, n: int = 1) -> None:
        self.items += n


@contextmanager
def track_job(name: str, factory: sessionmaker[Session] | None = None) -> Iterator[JobTracker]:
    """Context manager writing a ``job_runs`` row in its own short transactions.

    Exceptions are recorded and re-raised. Set ``tracker.partial_error`` to mark PARTIAL success.
    """
    factory = factory or get_session_factory()
    with factory() as s:
        row = JobRun(job_name=name, status=JobStatus.RUNNING, started_at=utcnow())
        s.add(row)
        s.commit()
        run_id = row.id
    tracker = JobTracker()
    status, error = JobStatus.SUCCESS, None
    try:
        yield tracker
        if tracker.partial_error:
            status, error = JobStatus.PARTIAL, tracker.partial_error
    except Exception as exc:
        status, error = JobStatus.FAILED, f"{type(exc).__name__}: {exc}"
        raise
    finally:
        with factory() as s:
            row2 = s.get(JobRun, run_id)
            if row2 is not None:
                row2.status = status
                row2.finished_at = utcnow()
                row2.items = tracker.items
                row2.error = error
                row2.details = tracker.details
                s.commit()
