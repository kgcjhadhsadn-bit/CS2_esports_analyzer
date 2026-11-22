"""Shared result type for every ingestion sync (PandaScore, HLTV, ...).

One malformed record must never abort a whole sync: errors are collected here instead of raised, and the
scheduler copies :meth:`SyncReport.as_dict` into ``job_runs.details``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SyncReport:
    """Outcome of one sync call."""

    job: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    fetched_pages: int = 0
    blocked: bool = False  # the source refused us (e.g. a challenge page); back off
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    match_ids: list[int] = field(default_factory=list)  # internal match ids touched
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def items(self) -> int:
        return self.created + self.updated

    @property
    def ok(self) -> bool:
        return not self.errors and not self.blocked

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def merge(self, other: SyncReport) -> None:
        self.created += other.created
        self.updated += other.updated
        self.skipped += other.skipped
        self.failed += other.failed
        self.fetched_pages += other.fetched_pages
        self.blocked = self.blocked or other.blocked
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        for match_id in other.match_ids:
            if match_id not in self.match_ids:
                self.match_ids.append(match_id)

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly summary (e.g. for ``JobTracker.details``); long lists are truncated."""
        return {
            "job": self.job,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "fetched_pages": self.fetched_pages,
            "blocked": self.blocked,
            "errors": self.errors[:50],
            "warnings": self.warnings[:50],
            "match_ids": self.match_ids[:500],
            **self.details,
        }
