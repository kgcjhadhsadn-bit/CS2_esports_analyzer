"""Betclic odds sources: protocol, raw-payload archive and the shared parse pipeline.

How Betclic's web apps load odds (research, 2026-11; nothing could be verified live because
betclic.pl only serves Polish IPs - every detail below is an assumption to confirm on the
owner's PC, which is why every raw payload is archived)
------------------------------------------------------------------------------------------------
1. **Historic REST "offering" API** (2019-2023 open-source scrapers: pretrehr/Sports-betting,
   dmartin35/pronosfoot, DownD/ArbitrageScanner, EtienneMMichel/LapisChallenge):
   ``https://offer.cdn.begmedia.com/api/pub/v4/events?application=2&countrycode=fr&language=fr
   &sitecode=frfr&sportIds=1&limit=..&offset=0&sortBy=ByLiveRankingPreliveDate
   &fetchMultipleDefaultMarkets=true`` (listing), ``.../v4|v5/events/{id}?...`` (one event, all
   markets) and ``.../v2/competitions/{id}``; country hosts ``offer.cdn.betclic.fr`` /
   ``offer.cdn.begmedia.com``; ``application`` 2 = FR web, 1024 = PT web; Poland would use
   ``countrycode=pl&language=pl&sitecode=plpl``. JSON: ``unifiedEvents[] -> {id, name, date,
   isLive, contestants[{name}], competition{name}, markets[{id, name, mtc, selections[{id, name,
   odds}]}]}``. Reports from 2025-2026 say these routes now answer 404 "Route not found".
2. **gRPC-web** (current; TapsHTS/betclic-api, tomasmourato/bettrackr - verified 2026-09 -
   and czareq089/value-bets, which targets **betclic.pl** with ``Origin: https://www.betclic.pl``,
   ``X-Bg-Regulation: PL``, ``X-Bg-Ref-Regulator-Zone: PL``, ``X-Bg-Ref-Brand: BETCLIC``,
   ``X-Bg-Ref-Platform: DESKTOP``, ``X-Grpc-Web: 1``): binary protobuf POSTed to
   ``https://offering.begmedia.com/web/offering.access.api/offering.access.api.MatchService/
   GetMatchesBySportWithNotifications`` and ``.../GetMatchWithNotification`` (server streams).
   See :mod:`cs2bot.odds.grpc_web` for the reverse-engineered field numbers. Event pages only
   preload the "Top" category (25 of 238 markets in one measured football match); the browser
   fetches the other categories (``ca_*`` ids) one by one when their tabs are opened.
3. **Angular transfer state**: the server-rendered page embeds its gRPC responses decoded to JSON
   in ``<script id="ng-state" type="application/json">`` (``matchId``, ``matchDateUtc``,
   ``categories[{id, name}]``, selections ``{id, name, odds, betslipMarketId}`` in
   ``mainSelections`` / ``selectionMatrix``, ``requestHeaders.Appversion``). A page also embeds
   many unrelated matches (recommendations). Event URLs end in ``-m<matchId>`` and Betclic
   redirects to the canonical slug, so ``/<sport>-s<id>/<comp>-c<id>/<slug>-m<id>`` patterns work.

Hence the sources capture **all three**: JSON network responses, gRPC-web bodies (via a fetch
hook in the page, because these streams never "finish" for Playwright's ``response.body()``)
and the embedded JSON state; everything is saved under
``<data_dir>/raw/betclic/<UTC date>/`` and parsed with the generic
:mod:`cs2bot.odds.payload_parser` walker (gRPC bodies are converted to JSON first).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from cs2bot.config import Settings
from cs2bot.domain.odds import CollectedEvent
from cs2bot.odds.grpc_web import BetclicProtoSchema, decode_betclic_grpc_body, looks_like_grpc_web
from cs2bot.odds.payload_parser import (
    ParsedEvent,
    PayloadKeys,
    event_id_from_url,
    extract_event_records,
    extract_json_documents,
    merge_parsed_events,
    sport_matches,
)
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import ensure_utc, utcnow

logger = logging.getLogger(__name__)

PayloadKind = Literal["json", "grpc", "html"]
_DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


@runtime_checkable
class OddsSource(Protocol):
    """Anything that can list the current CS2 events with their markets."""

    def fetch_cs2_events(self) -> list[CollectedEvent]: ...


@dataclass
class FetchInfo:
    """Diagnostics of the last ``fetch_cs2_events()`` call (optional source capability)."""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    pages_visited: int = 0
    payloads_captured: int = 0
    raw_paths_by_event: dict[str, list[str]] = field(default_factory=dict)
    # when each event's prices were captured (latest payload) - a crawl can take minutes, so the
    # collector stamps prices with these instead of the time the run started. Sources also set
    # ``CollectedEvent.captured_at`` (same value), which the collector prefers.
    captured_at_by_event: dict[str, datetime] = field(default_factory=dict)
    # event id -> {(market raw_name, market external_id) -> capture time of the freshest payload
    # showing that market}. One event is usually assembled from several payloads captured minutes
    # apart (its own page, plus "recommended matches" widgets on later pages that only repeat the
    # match winner); stamping every market with the event's latest payload would make the markets
    # that were NOT refreshed look fresher than they are.
    captured_at_by_market: dict[str, dict[tuple[str, str], datetime]] = field(default_factory=dict)
    # events whose market list is known to be incomplete (e.g. seen on the listing only): the
    # collector must not mark their missing markets unavailable
    partial_event_ids: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "pages_visited": self.pages_visited,
            "payloads_captured": self.payloads_captured,
            "partial_events": len(self.partial_event_ids),
            "errors": self.errors[:20],
            **self.notes,
        }


@runtime_checkable
class SupportsFetchInfo(Protocol):
    def last_fetch_info(self) -> FetchInfo: ...


@dataclass
class CapturedPayload:
    kind: PayloadKind
    data: bytes
    captured_at: datetime
    url: str | None = None
    page_url: str | None = None
    content_type: str | None = None
    request_body: bytes | None = None
    path: Path | None = None


# --------------------------------------------------------------------------- raw archive


class RawPayloadStore:
    """Archive of raw payloads: ``<root>/<UTC date>/<HHMMSS>_<seq>_<kind>_<slug>.<ext>`` + ``index.jsonl``.

    Nothing secret is stored: response bodies and URLs only (no cookies, no request headers).
    """

    _EXT: ClassVar[dict[str, str]] = {"json": "json", "grpc": "bin", "html": "html"}

    def __init__(self, root: Path | None, *, enabled: bool = True, retention_days: int | None = 14) -> None:
        self.root = root
        self.enabled = enabled and root is not None
        self.retention_days = retention_days
        self._seq = 0

    @classmethod
    def from_settings(
        cls, settings: Settings, *, enabled: bool = True, retention_days: int | None = 14
    ) -> RawPayloadStore:
        return cls(
            resolve_path(settings.data_dir) / "raw" / "betclic",
            enabled=enabled,
            retention_days=retention_days,
        )

    def save(self, payload: CapturedPayload) -> Path | None:
        if not self.enabled or self.root is None:
            return None
        try:
            day_dir = self.root / payload.captured_at.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            slug = _SLUG_RE.sub("-", (payload.url or payload.page_url or "payload").split("?", 1)[0])[
                -60:
            ].strip("-")
            name = f"{payload.captured_at:%H%M%S}_{self._seq:04d}_{payload.kind}_{slug or 'payload'}"
            path = day_dir / f"{name}.{self._EXT[payload.kind]}"
            path.write_bytes(payload.data)
            meta = {
                "file": path.name,
                "kind": payload.kind,
                "url": payload.url,
                "page_url": payload.page_url,
                "content_type": payload.content_type,
                "captured_at": payload.captured_at.isoformat(),
                "bytes": len(payload.data),
            }
            if payload.request_body:
                req_path = day_dir / f"{name}.request.bin"
                req_path.write_bytes(payload.request_body)
                meta["request_file"] = req_path.name
            with (day_dir / "index.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
            payload.path = path
            return path
        except OSError:
            logger.warning("could not archive raw Betclic payload", exc_info=True)
            return None

    def cleanup(self, now: datetime | None = None) -> int:
        """Delete date directories older than ``retention_days``; returns how many were removed."""
        if not self.enabled or self.root is None or not self.retention_days or not self.root.is_dir():
            return 0
        cutoff = ((now or utcnow()) - timedelta(days=self.retention_days)).date()
        removed = 0
        for child in self.root.iterdir():
            if not child.is_dir() or not _DAY_DIR_RE.match(child.name):
                continue
            try:
                day = date.fromisoformat(child.name)
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed


# --------------------------------------------------------------------------- parse pipeline


def payload_documents(payload: CapturedPayload, *, schema: BetclicProtoSchema | None = None) -> list[Any]:
    """JSON documents contained in one captured payload (never raises)."""
    try:
        if payload.kind == "html":
            return extract_json_documents(payload.data.decode("utf-8", "replace"))
        if payload.kind == "grpc":
            return [decode_betclic_grpc_body(payload.data, content_type=payload.content_type, schema=schema)]
        return [json.loads(payload.data.decode("utf-8", "replace"))]
    except (ValueError, RecursionError):
        logger.debug("unparseable %s payload from %s", payload.kind, payload.url, exc_info=True)
        return []


@dataclass
class CapturedParse:
    records: list[ParsedEvent]
    paths: dict[str, list[str]]  # event id -> archived payload files it was found in
    captured_at: dict[str, datetime]  # event id -> capture time of its latest payload
    # event id -> {(market raw_name, external_id) -> capture time of the market's freshest payload}
    market_times: dict[str, dict[tuple[str, str], datetime]] = field(default_factory=dict)

    def collected(self, records: Iterable[ParsedEvent] | None = None) -> list[CollectedEvent]:
        """``records`` (default: all) as :class:`CollectedEvent` with ``captured_at`` filled in."""
        chosen = self.records if records is None else records
        return [r.to_collected(captured_at=self.captured_at.get(r.external_id)) for r in chosen]

    def fill_info(self, info: FetchInfo, records: Iterable[ParsedEvent]) -> None:
        """Copy archive paths and capture times of ``records`` into a :class:`FetchInfo`."""
        ids = [r.external_id for r in records]
        info.raw_paths_by_event = {eid: self.paths.get(eid, []) for eid in ids}
        info.captured_at_by_event = {eid: self.captured_at[eid] for eid in ids if eid in self.captured_at}
        info.captured_at_by_market = {
            eid: self.market_times[eid] for eid in ids if self.market_times.get(eid)
        }


def parse_captured_full(
    payloads: Sequence[CapturedPayload],
    *,
    keys: PayloadKeys | None = None,
    schema: BetclicProtoSchema | None = None,
) -> CapturedParse:
    """Parse payloads in capture order: merged records, archived paths and capture time per event."""
    records: list[ParsedEvent] = []
    paths: dict[str, list[str]] = {}
    times: dict[str, datetime] = {}
    for payload in payloads:
        docs = payload_documents(payload, schema=schema)
        if not docs:
            continue
        found = extract_event_records(docs, source_url=payload.page_url or payload.url, keys=keys)
        for rec in found:
            for parsed_market in rec.markets.values():
                parsed_market.captured_at = payload.captured_at
            if payload.path is not None:
                paths.setdefault(rec.external_id, [])
                if str(payload.path) not in paths[rec.external_id]:
                    paths[rec.external_id].append(str(payload.path))
            if rec.markets and (rec.external_id not in times or payload.captured_at > times[rec.external_id]):
                times[rec.external_id] = payload.captured_at
        records.extend(found)
    merged = merge_parsed_events(records)
    return CapturedParse(
        records=merged,
        paths=paths,
        captured_at=times,
        market_times={r.external_id: r.market_times() for r in merged},
    )


def parse_captured(
    payloads: Sequence[CapturedPayload],
    *,
    keys: PayloadKeys | None = None,
    schema: BetclicProtoSchema | None = None,
) -> tuple[list[ParsedEvent], dict[str, list[str]]]:
    """Parse payloads in capture order; returns merged records + archived paths per event id."""
    parsed = parse_captured_full(payloads, keys=keys, schema=schema)
    return parsed.records, parsed.paths


def select_cs2(
    records: Iterable[ParsedEvent],
    *,
    pattern: str | re.Pattern[str],
    allowed_ids: set[str] | None = None,
    keep_unknown: bool = False,
) -> list[ParsedEvent]:
    """Keep CS2 events: ids discovered from CS2 links, or sport/competition/url hints matching."""
    regex = re.compile(pattern) if isinstance(pattern, str) else pattern
    kept: list[ParsedEvent] = []
    for rec in records:
        if allowed_ids is not None and rec.external_id in allowed_ids:
            kept.append(rec)
            continue
        verdict = sport_matches(rec, regex)
        if verdict or (verdict is None and keep_unknown):
            kept.append(rec)
    return kept


def classify_body(body: bytes, content_type: str | None, url: str | None = None) -> PayloadKind | None:
    """json / grpc / html (or ``None`` for anything else) from content type and a body sniff."""
    ctype = (content_type or "").lower()
    if "grpc" in ctype or "protobuf" in ctype:
        return "grpc"
    if "json" in ctype:
        return "json"
    if "html" in ctype:
        return "html"
    head = body[:1].strip()
    if head in (b"{", b"["):
        return "json"
    if looks_like_grpc_web(body, content_type):
        return "grpc"
    return None


# --------------------------------------------------------------------------- simple sources


class StaticOddsSource:
    """Returns a fixed list of events (tests, demos, integration wiring)."""

    def __init__(self, events: Sequence[CollectedEvent], *, partial_event_ids: Iterable[str] = ()) -> None:
        self.events = list(events)
        self._info = FetchInfo(partial_event_ids=set(partial_event_ids))

    def fetch_cs2_events(self) -> list[CollectedEvent]:
        now = utcnow()
        self._info.started_at = self._info.finished_at = now
        return [e.model_copy(deep=True) for e in self.events]

    def last_fetch_info(self) -> FetchInfo:
        return self._info


class RawPayloadReplaySource:
    """Re-parse archived raw payloads (``*.json`` / ``*.html`` / ``*.bin``) - for fitting the parser.

    ``paths`` may contain files or directories (searched recursively). Events are filtered with
    the CS2 pattern unless ``cs2_pattern`` is ``None``. Replayed events are treated as partial
    (a replay must never mark live markets unavailable).

    Prices keep the time they were ORIGINALLY captured (``captured_at`` from the archive's
    ``index.jsonl``, else the file's modification time) - never "now": replaying yesterday's
    archive into the live database must not make stale prices look fresh to
    :func:`cs2bot.odds.quotes.latest_quotes`. The collector additionally ignores observations
    older than what it already stored for a selection.
    """

    _KIND_BY_EXT: ClassVar[dict[str, PayloadKind]] = {
        ".json": "json",
        ".html": "html",
        ".htm": "html",
        ".bin": "grpc",
    }

    def __init__(
        self,
        paths: Sequence[str | Path],
        *,
        cs2_pattern: str | None = None,
        keep_unknown_sport: bool = True,
        keys: PayloadKeys | None = None,
        schema: BetclicProtoSchema | None = None,
    ) -> None:
        self.paths = [Path(p) for p in paths]
        self.cs2_pattern = cs2_pattern
        self.keep_unknown_sport = keep_unknown_sport
        self.keys = keys
        self.schema = schema
        self._info = FetchInfo()

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for path in self.paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob("*") if p.is_file()))
            elif path.is_file():
                files.append(path)
        return [
            f for f in files if f.suffix.lower() in self._KIND_BY_EXT and not f.name.endswith(".request.bin")
        ]

    @staticmethod
    def _index_times(directory: Path, cache: dict[Path, dict[str, tuple[datetime, str | None]]]) -> dict[
        str, tuple[datetime, str | None]
    ]:
        """``file name -> (captured_at, page url)`` from the directory's ``index.jsonl`` (if any)."""
        if directory not in cache:
            entries: dict[str, tuple[datetime, str | None]] = {}
            try:
                lines = (directory / "index.jsonl").read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    meta = json.loads(line)
                    stamp = ensure_utc(datetime.fromisoformat(str(meta["captured_at"])))
                    page_url = meta.get("page_url") or meta.get("url")
                    entries[str(meta["file"])] = (stamp, page_url if isinstance(page_url, str) else None)
                except (ValueError, KeyError, TypeError):
                    continue  # one damaged index line must not hide the others
            cache[directory] = entries
        return cache[directory]

    def fetch_cs2_events(self) -> list[CollectedEvent]:
        info = FetchInfo(started_at=utcnow())
        payloads: list[CapturedPayload] = []
        index_cache: dict[Path, dict[str, tuple[datetime, str | None]]] = {}
        for file in self._files():
            try:
                indexed = self._index_times(file.parent, index_cache).get(file.name)
                if indexed is not None:
                    captured_at, page_url = indexed
                else:
                    captured_at = datetime.fromtimestamp(file.stat().st_mtime, tz=UTC)
                    page_url = None
                payloads.append(
                    CapturedPayload(
                        kind=self._KIND_BY_EXT[file.suffix.lower()],
                        data=file.read_bytes(),
                        captured_at=captured_at,
                        url=page_url or file.name,
                        path=file,
                    )
                )
            except OSError as exc:
                info.errors.append(f"{file}: {exc}")
        payloads.sort(key=lambda payload: payload.captured_at)  # later prices must win the merge
        parsed = parse_captured_full(payloads, keys=self.keys, schema=self.schema)
        records = parsed.records
        if self.cs2_pattern:
            records = select_cs2(records, pattern=self.cs2_pattern, keep_unknown=self.keep_unknown_sport)
        info.payloads_captured = len(payloads)
        parsed.fill_info(info, records)
        info.partial_event_ids = {r.external_id for r in records}
        info.finished_at = utcnow()
        self._info = info
        return parsed.collected(records)

    def last_fetch_info(self) -> FetchInfo:
        return self._info


def ids_from_links(links: Iterable[str], pattern: str) -> dict[str, str]:
    """``event id -> first URL`` for links that look like event pages (order preserved)."""
    out: dict[str, str] = {}
    for link in links:
        event_id = event_id_from_url(link, pattern)
        if event_id and event_id not in out:
            out[event_id] = link
    return out
