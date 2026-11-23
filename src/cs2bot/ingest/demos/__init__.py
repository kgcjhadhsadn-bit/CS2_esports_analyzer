"""cs2bot.ingest.demos - demo download, extraction, parsing (demoparser2), persistence and aggregates.

Typical use (see docs/dev/reports/demos.md for the full interface)::

    from cs2bot.ingest.demos import BrowserDownloader, process_pending_demos
    from cs2bot.ingest.demos import rebuild_duel_aggregates, rebuild_position_aggregates

    with BrowserDownloader.from_settings(settings) as downloader:
        report = process_pending_demos(session_factory, settings, downloader=downloader, limit=5)
    with session_factory() as session:
        rebuild_position_aggregates(session)
        rebuild_duel_aggregates(session)
        session.commit()
"""

from cs2bot.ingest.demos.aggregates import ALL_MAPS, rebuild_duel_aggregates, rebuild_position_aggregates
from cs2bot.ingest.demos.archive import (
    RarTool,
    detect_archive_kind,
    ensure_rar_backend,
    extract_archive,
    find_rar_tools,
    match_dem_files_to_maps,
    read_demo_map_name,
)
from cs2bot.ingest.demos.browser import BrowserDownloader
from cs2bot.ingest.demos.download import Downloader, download_demo, http_download
from cs2bot.ingest.demos.errors import (
    ArchiveError,
    ArchiveToolMissingError,
    DemoDownloadBlockedError,
    DemoDownloadError,
    DemoError,
    DemoParseError,
    DemoPersistError,
)
from cs2bot.ingest.demos.parse import (
    ECO_MAX_AVG_EQUIPMENT,
    FORCE_MAX_AVG_EQUIPMENT,
    PISTOL_ROUNDS,
    ParsedDemo,
    ParseOptions,
    classify_buy_type,
    demoparser_version,
    map_round_end_reason,
    parse_demo,
)
from cs2bot.ingest.demos.persist import persist_parsed_demo, resolve_steamids
from cs2bot.ingest.demos.pipeline import (
    JOB_NAME,
    PipelineOptions,
    ProcessReport,
    process_pending_demos,
    select_pending_demo_ids,
)

__all__ = [
    "ALL_MAPS",
    "ECO_MAX_AVG_EQUIPMENT",
    "FORCE_MAX_AVG_EQUIPMENT",
    "JOB_NAME",
    "PISTOL_ROUNDS",
    "ArchiveError",
    "ArchiveToolMissingError",
    "BrowserDownloader",
    "DemoDownloadBlockedError",
    "DemoDownloadError",
    "DemoError",
    "DemoParseError",
    "DemoPersistError",
    "Downloader",
    "ParseOptions",
    "ParsedDemo",
    "PipelineOptions",
    "ProcessReport",
    "RarTool",
    "classify_buy_type",
    "demoparser_version",
    "detect_archive_kind",
    "download_demo",
    "ensure_rar_backend",
    "extract_archive",
    "find_rar_tools",
    "http_download",
    "map_round_end_reason",
    "match_dem_files_to_maps",
    "parse_demo",
    "persist_parsed_demo",
    "process_pending_demos",
    "read_demo_map_name",
    "rebuild_duel_aggregates",
    "rebuild_position_aggregates",
    "resolve_steamids",
    "select_pending_demo_ids",
]
