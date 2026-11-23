"""The demo job: download -> extract -> match files to maps -> parse -> persist -> clean up.

``process_pending_demos`` works through ``demos`` rows created by the HLTV sync. Every demo is
handled in its own short transactions and its own ``try`` block, so one broken archive never stops
the batch; raw files are deleted afterwards unless ``settings.demos.keep_archives`` /
``keep_dem_files`` say otherwise (also after a failure - a retry downloads again, a full disk helps
nobody).

Status flow of a row: ``PENDING`` -> ``DOWNLOADING`` -> ``DOWNLOADED`` -> ``PARSED``, or ``FAILED``
with ``error`` set (retried while ``attempts < max_attempts``), or ``UNAVAILABLE`` when it has no
URL. A row left in ``DOWNLOADING`` / ``DOWNLOADED`` by a crash is picked up again once it is older
than ``stale_after``.

A downloader that raises an "I am being blocked" error (any exception class named
``HLTVBlockedError`` or ``DemoDownloadBlockedError`` in its MRO, i.e. the HLTV fetcher's or the
browser downloader's) stops the batch immediately: the row goes back to ``PENDING`` without using up
an attempt and ``report.blocked`` is set so the caller backs off.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from demoparser2 import DemoParser
from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import Demo, MapPlayed, Match
from cs2bot.domain.enums import DemoStatus
from cs2bot.ingest.demos.archive import extract_archive, match_dem_files_to_maps
from cs2bot.ingest.demos.download import Downloader, download_demo
from cs2bot.ingest.demos.errors import ArchiveError, DemoError
from cs2bot.ingest.demos.parse import ParserFactory, demoparser_version, parse_demo
from cs2bot.ingest.demos.persist import persist_parsed_demo, resolve_steamids
from cs2bot.utils.logging import get_logger
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import utcnow

log = get_logger(__name__)

SessionFactory = Callable[[], Session]
JOB_NAME = "demos.process"


@dataclass(frozen=True)
class PipelineOptions:
    """Operational limits of the demo job (module-level defaults; not part of ``Settings``)."""

    max_attempts: int = 3
    stale_after: timedelta = timedelta(hours=3)
    # Stop the batch when the demo volume runs low: a BO3 archive plus its extracted demos needs ~2 GB.
    min_free_bytes: int = 3 * 1024**3
    max_error_chars: int = 2000


@dataclass
class ProcessReport:
    """Outcome of one :func:`process_pending_demos` call."""

    job: str = JOB_NAME
    selected: int = 0
    parsed: int = 0
    failed: int = 0
    unavailable: int = 0
    maps_persisted: int = 0
    rounds_persisted: int = 0
    blocked: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    demo_ids: list[int] = field(default_factory=list)
    match_ids: list[int] = field(default_factory=list)

    @property
    def items(self) -> int:
        return self.parsed

    @property
    def ok(self) -> bool:
        return not self.errors and not self.blocked

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly summary for ``JobTracker.details``."""
        return {
            "job": self.job,
            "selected": self.selected,
            "parsed": self.parsed,
            "failed": self.failed,
            "unavailable": self.unavailable,
            "maps_persisted": self.maps_persisted,
            "rounds_persisted": self.rounds_persisted,
            "blocked": self.blocked,
            "errors": self.errors[:50],
            "warnings": self.warnings[:50],
            "demo_ids": self.demo_ids[:500],
            "match_ids": self.match_ids[:500],
        }


class _Blocked(Exception):
    """Internal signal: the download source is blocking us; stop the batch."""


_BLOCK_ERROR_NAMES = frozenset({"HLTVBlockedError", "DemoDownloadBlockedError"})


def _is_block_error(exc: BaseException) -> bool:
    """Recognised by class name so that this package does not have to import the HLTV package."""
    return any(cls.__name__ in _BLOCK_ERROR_NAMES for cls in type(exc).__mro__)


def select_pending_demo_ids(
    session: Session, *, limit: int, now: datetime | None = None, options: PipelineOptions | None = None
) -> list[int]:
    """Ids of the demos to work on next: untouched ones first, newest matches first."""
    options = options or PipelineOptions()
    stale_before = (now or utcnow()) - options.stale_after
    eligible = or_(
        Demo.status == DemoStatus.PENDING,
        and_(Demo.status == DemoStatus.FAILED, Demo.attempts < options.max_attempts),
        and_(
            Demo.status.in_([DemoStatus.DOWNLOADING, DemoStatus.DOWNLOADED]),
            Demo.updated_at < stale_before,
        ),
    )
    statement = (
        select(Demo.id)
        .join(Match, Match.id == Demo.match_id)
        .where(eligible)
        .order_by(
            case((Demo.status == DemoStatus.PENDING, 0), else_=1),
            Match.scheduled_at.desc().nulls_last(),
            Demo.id.desc(),
        )
        .limit(max(0, limit))
    )
    return list(session.scalars(statement))


def _absolute_url(url: str, settings: Settings) -> str | None:
    """Absolute ``http(s)`` download URL, or ``None`` when the stored value is not one.

    Demo URLs come from scraped pages: a relative path is resolved against the HLTV base URL, a
    protocol-relative one gets ``https:``, and every other scheme (``file:``, ``javascript:``, ...)
    is refused so that no downloader is ever pointed at a local file.
    """
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = settings.hltv.base_url.rstrip("/") + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return url


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


def _header_reader(parser_factory: ParserFactory) -> Callable[[Path], str | None]:
    def read(path: Path) -> str | None:
        try:
            raw = parser_factory(str(path)).parse_header().get("map_name")
        except Exception as exc:
            log.warning("cannot read demo header of %s: %s", path.name, exc)
            return None
        return normalize_map_name(str(raw)) if raw else None

    return read


def _remove_empty_dirs(*dirs: Path) -> None:
    for directory in dirs:
        try:
            directory.rmdir()
        except OSError:
            continue


def _cleanup(work_dir: Path, archive: Path | None, dem_paths: list[Path], settings: Settings) -> None:
    """Delete raw files according to the keep flags (a plain ``.dem`` download is both things).

    Only files inside ``work_dir`` are ever deleted: an injected downloader that returns a path
    elsewhere (a cache, a file the owner downloaded by hand) keeps that file.
    """
    keep_archive, keep_dem = settings.demos.keep_archives, settings.demos.keep_dem_files
    for dem in dem_paths:
        is_archive = archive is not None and dem == archive
        if not (keep_dem or (is_archive and keep_archive)) and _inside(dem, work_dir):
            dem.unlink(missing_ok=True)
    if archive is not None and archive not in dem_paths and not keep_archive and _inside(archive, work_dir):
        archive.unlink(missing_ok=True)
    if work_dir.is_dir():
        for leftover in work_dir.rglob("*.part"):
            leftover.unlink(missing_ok=True)
    _remove_empty_dirs(work_dir / "dem", work_dir, work_dir.parent)


def _process_one(
    session_factory: SessionFactory,
    demo_id: int,
    settings: Settings,
    downloader: Downloader | None,
    parser_factory: ParserFactory,
    options: PipelineOptions,
    report: ProcessReport,
) -> None:
    with session_factory() as session:
        demo = session.get(Demo, demo_id)
        if demo is None:
            return
        report.demo_ids.append(demo_id)
        if not demo.url:
            demo.status = DemoStatus.UNAVAILABLE
            demo.error = "no download url"
            session.commit()
            report.unavailable += 1
            return
        if demo.attempts >= options.max_attempts:
            # Only reachable for a row a crashed run left in DOWNLOADING / DOWNLOADED on its last attempt.
            demo.status = DemoStatus.FAILED
            demo.error = demo.error or "interrupted during the last allowed attempt"
            session.commit()
            report.failed += 1
            return
        url = _absolute_url(demo.url, settings)
        if url is None:
            demo.status = DemoStatus.UNAVAILABLE
            demo.error = "download url is not an http(s) url"
            session.commit()
            report.unavailable += 1
            return
        previous_status, previous_error = demo.status, demo.error
        demo.status = DemoStatus.DOWNLOADING
        demo.attempts += 1
        demo.error = None
        session.commit()
        match_id = demo.match_id

    work_dir = resolve_path(settings.demos.storage_dir) / f"match_{match_id}" / f"demo_{demo_id}"
    archive: Path | None = None
    dem_paths: list[Path] = []
    try:
        try:
            archive = download_demo(
                url, work_dir, downloader=downloader, timeout_s=settings.demos.download_timeout_s
            )
        except Exception as exc:
            if _is_block_error(exc):
                with session_factory() as session:
                    demo = session.get(Demo, demo_id)
                    if demo is not None:
                        demo.status = (
                            previous_status if previous_status is DemoStatus.FAILED else DemoStatus.PENDING
                        )
                        demo.attempts = max(0, demo.attempts - 1)
                        demo.error = previous_error
                        session.commit()
                raise _Blocked(str(exc)) from exc
            raise
        with session_factory() as session:
            demo = session.get(Demo, demo_id)
            if demo is not None:
                demo.status = DemoStatus.DOWNLOADED
                demo.archive_path = str(archive)
                demo.size_bytes = archive.stat().st_size
                demo.downloaded_at = utcnow()
                session.commit()

        dem_paths = extract_archive(archive, work_dir / "dem")
        with session_factory() as session:
            maps = [
                (m.map_number, m.map_name, m.id)
                for m in session.scalars(select(MapPlayed).where(MapPlayed.match_id == match_id))
            ]
        if not maps:
            raise DemoError(f"match {match_id} has no maps_played rows yet")
        assignment = match_dem_files_to_maps(
            dem_paths, [(number, name) for number, name, _id in maps], _header_reader(parser_factory)
        )
        if not assignment:
            names = ", ".join(p.name for p in dem_paths)
            raise ArchiveError(f"no demo file matches the maps of match {match_id} (files: {names})")
        for extra in sorted(set(dem_paths) - set(assignment.values())):
            report.warnings.append(f"demo {demo_id}: {extra.name} matches no map of match {match_id}")
        if len(dem_paths) > 1:
            # A whole-match archive (a per-map download holds a single demo): say which maps it lacks.
            for number, name, _id in sorted(maps):
                if number not in assignment:
                    report.warnings.append(f"demo {demo_id}: no demo file for map {number} ({name})")

        map_ids = {number: map_id for number, _name, map_id in maps}
        map_errors: list[str] = []
        for number, dem_path in sorted(assignment.items()):
            try:
                parsed = parse_demo(dem_path, settings.demos, parser_factory=parser_factory)
                with session_factory() as session:
                    try:
                        mapping = resolve_steamids(session, match_id, parsed)
                        rounds = persist_parsed_demo(session, map_ids[number], parsed, mapping)
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
                report.maps_persisted += 1
                report.rounds_persisted += rounds
            except Exception as exc:
                log.warning("demo %s map %s failed: %s", demo_id, number, exc)
                map_errors.append(f"map {number} ({dem_path.name}): {type(exc).__name__}: {exc}")
        if map_errors:
            raise DemoError("; ".join(map_errors))

        kept_archive = archive is not None and (
            settings.demos.keep_archives or (archive in dem_paths and settings.demos.keep_dem_files)
        )
        with session_factory() as session:
            demo = session.get(Demo, demo_id)
            if demo is not None:
                demo.status = DemoStatus.PARSED
                demo.parser_version = demoparser_version()
                demo.parsed_at = utcnow()
                demo.error = None
                demo.dem_files = [assignment[number].name for number in sorted(assignment)]
                demo.archive_path = str(archive) if kept_archive else None
                session.commit()
        report.parsed += 1
        if match_id not in report.match_ids:
            report.match_ids.append(match_id)
    except _Blocked:
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[: options.max_error_chars]
        log.warning("demo %s failed: %s", demo_id, message)
        with session_factory() as session:
            demo = session.get(Demo, demo_id)
            if demo is not None:
                demo.status = DemoStatus.FAILED
                demo.error = message
                demo.archive_path = (
                    str(archive) if archive is not None and settings.demos.keep_archives else None
                )
                session.commit()
        report.failed += 1
        report.errors.append(f"demo {demo_id}: {message}")
    finally:
        try:
            _cleanup(work_dir, archive, dem_paths, settings)
        except OSError as exc:
            report.warnings.append(f"demo {demo_id}: cleanup failed: {exc}")


def process_pending_demos(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    downloader: Downloader | None = None,
    parser_factory: ParserFactory = DemoParser,
    limit: int = 5,
    options: PipelineOptions | None = None,
    now: datetime | None = None,
) -> ProcessReport:
    """Download, parse and store up to ``limit`` pending demos; returns a :class:`ProcessReport`.

    ``session_factory`` is a ``sessionmaker`` (or any ``() -> Session``); ``downloader`` is
    ``(url, dest_dir) -> Path`` (pass the HLTV browser downloader in production - the default httpx
    transfer is for unprotected sources); ``parser_factory`` is ``demoparser2.DemoParser``.
    Demos are processed one after another (``settings.demos.max_concurrent_downloads`` is 1 by
    design: politeness towards the source matters more than throughput).
    """
    options = options or PipelineOptions()
    report = ProcessReport()
    with session_factory() as session:
        demo_ids = select_pending_demo_ids(session, limit=limit, now=now, options=options)
    report.selected = len(demo_ids)
    storage = resolve_path(settings.demos.storage_dir)
    for demo_id in demo_ids:
        try:
            storage.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(storage).free
        except OSError as exc:
            report.errors.append(f"demo storage {storage} is not usable: {exc}")
            break
        if free < options.min_free_bytes:
            report.warnings.append(f"only {free // 1024**2} MB free in {storage}: demo processing paused")
            break
        try:
            _process_one(session_factory, demo_id, settings, downloader, parser_factory, options, report)
        except _Blocked as exc:
            report.blocked = True
            report.warnings.append(f"download source is blocking us, batch stopped: {exc}")
            break
        except Exception as exc:  # bookkeeping itself failed (e.g. database down): keep going
            log.exception("demo %s: unexpected pipeline error", demo_id)
            report.failed += 1
            report.errors.append(f"demo {demo_id}: {type(exc).__name__}: {exc}")
    return report
