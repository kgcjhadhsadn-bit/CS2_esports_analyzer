"""Recent pipeline job runs (``job_runs``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from cs2bot.api.deps import DB, Paging, require_auth
from cs2bot.api.formatting import utc
from cs2bot.api.params import ListParam, parse_enum_list
from cs2bot.api.schemas import JobRunOut, JobRunPage
from cs2bot.db.models import JobRun
from cs2bot.domain.enums import JobStatus
from cs2bot.utils.time import ensure_utc

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_auth)])


@router.get("", response_model=JobRunPage)
def list_jobs(db: DB, page: Paging, name: str | None = None, status: ListParam = None) -> JobRunPage:
    stmt = select(JobRun)
    if name:
        stmt = stmt.where(JobRun.job_name == name)
    statuses = parse_enum_list(status, JobStatus, "status")
    if statuses:
        stmt = stmt.where(JobRun.status.in_(statuses))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(page.limit).offset(page.offset)
    )
    items: list[JobRunOut] = []
    for r in rows:
        started = ensure_utc(r.started_at)
        finished = utc(r.finished_at)
        items.append(
            JobRunOut(
                id=r.id,
                job_name=r.job_name,
                status=r.status,
                started_at=started,
                finished_at=finished,
                duration_s=round((finished - started).total_seconds(), 3) if finished is not None else None,
                items=r.items,
                error=r.error,
                details=dict(r.details or {}),
            )
        )
    return JobRunPage(total=total, limit=page.limit, offset=page.offset, items=items)
