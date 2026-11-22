"""Store Betclic odds: events -> markets -> selections, price history only on change.

:func:`collect_once` runs one collection pass:

1. ``source.fetch_cs2_events()`` (exceptions from the source propagate - nothing was collected);
2. per event, inside a SAVEPOINT so one bad event never aborts the run:

   * upsert ``bookmaker_events`` by (bookmaker, external_id) and record the archived raw payload
     paths in ``extra["raw_payloads"]``;
   * (re)link it with :func:`cs2bot.odds.event_matching.link_event` (manual CONFIRMED / REJECTED
     decisions are preserved);
   * live events are stored and flagged (``is_live``) but their prices are not processed - the
     system is pre-match only and the last pre-match snapshot stays the closing line;
   * upsert ``bookmaker_markets`` by (event_id, raw_name, external_id) and ``bookmaker_selections``
     by (market_id, raw_name, external_id), re-mapping every market on every pass (so link / swap
     changes and edited YAML rules apply immediately);
   * write an ``odds_snapshots`` row only when the odds or the availability differ from
     ``selection.last_odds`` / ``is_available``; ``last_odds_at`` is refreshed on every sighting
     (it is the "price confirmed at" timestamp the quote freshness check uses). Timestamps are
     the source's per-event capture times (``FetchInfo.captured_at_by_event``) because a browser
     crawl can take minutes - stamping with the run start would make fresh prices look stale and
     could put a pre-kick-off price on the wrong side of the closing-line cut-off;
   * selections that vanished from a refreshed event (whole market gone, or a single selection
     gone) are marked unavailable with a snapshot - unless the source reported the event as
     partial (e.g. only seen on the listing page).

Multi-line markets: a market row has ONE spec, so when the selections of one bookmaker market map
to different specs (a card listing several lines without per-line market ids), the market is
split into rows whose ``external_id`` is ``"<original id>~<hash of the spec signature>"``.
:func:`original_market_external_id` recovers Betclic's id (used for :class:`OddsQuote`).
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from cs2bot.config import Settings
from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, OddsSnapshot
from cs2bot.domain.enums import Bookmaker, LinkStatus
from cs2bot.domain.odds import CollectedEvent, CollectedMarket, CollectedSelection, MappedSelection
from cs2bot.odds.event_matching import LinkConfig, link_event
from cs2bot.odds.market_mapping import MarketMapper, PlayerResolver, lineup_player_resolver
from cs2bot.odds.sources import FetchInfo, OddsSource, SupportsFetchInfo
from cs2bot.odds.text import quantize_odds, truncate
from cs2bot.utils.time import utcnow

logger = logging.getLogger(__name__)

SPLIT_SEPARATOR = "~"
PlayerResolverFactory = Callable[[Session, int], PlayerResolver | None]


def original_market_external_id(external_id: str) -> str:
    """Betclic's market id of a (possibly split) market row."""
    return external_id.split(SPLIT_SEPARATOR, 1)[0]


@dataclass(frozen=True)
class CollectorConfig:
    store_live_odds: bool = False
    max_raw_paths_per_event: int = 20
    max_unmapped_in_report: int = 30
    link: LinkConfig = field(default_factory=LinkConfig)


class CollectReport(BaseModel):
    started_at: datetime
    finished_at: datetime | None = None
    events_seen: int = 0
    events_new: int = 0
    events_failed: int = 0
    events_live: int = 0
    events_partial: int = 0
    events_relinked: int = 0
    link_status_counts: dict[str, int] = Field(default_factory=dict)
    markets_seen: int = 0
    markets_new: int = 0
    markets_mapped: int = 0
    markets_disappeared: int = 0
    selections_seen: int = 0
    selections_new: int = 0
    selections_mapped: int = 0
    selections_marked_unavailable: int = 0
    snapshots_written: int = 0
    unmapped_markets: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class _Run:
    session: Session
    mapper: MarketMapper
    settings: Settings
    now: datetime
    config: CollectorConfig
    report: CollectReport
    info: FetchInfo | None
    factory: PlayerResolverFactory
    explicit_now: datetime | None = None
    resolvers: dict[int, PlayerResolver | None] = field(default_factory=dict)
    unmapped: dict[str, None] = field(default_factory=dict)
    fallback_now: datetime | None = None

    def event_time(self, ce: CollectedEvent) -> datetime:
        """Observation time of one event: explicit ``now`` > the source's capture time > post-fetch clock."""
        if self.explicit_now is not None:
            return self.explicit_now
        fallback = self.fallback_now or self.now
        captured = self.info.captured_at_by_event.get(ce.external_id) if self.info is not None else None
        return min(captured, fallback) if captured is not None else fallback

    def resolver_for(self, match_id: int | None) -> PlayerResolver | None:
        if match_id is None:
            return None
        if match_id not in self.resolvers:
            try:
                self.resolvers[match_id] = self.factory(self.session, match_id)
            except Exception:
                logger.warning("player resolver failed for match %s", match_id, exc_info=True)
                self.resolvers[match_id] = None
        return self.resolvers[match_id]


# --------------------------------------------------------------------------- helpers


def _merge_duplicate_events(events: Sequence[CollectedEvent]) -> list[CollectedEvent]:
    merged: dict[str, CollectedEvent] = {}
    for event in events:
        if event.external_id in merged:
            base = merged[event.external_id]
            base.markets = [*base.markets, *event.markets]
            base.is_live = base.is_live or event.is_live
            base.starts_at = base.starts_at or event.starts_at
            base.competition_raw = base.competition_raw or event.competition_raw
            base.url = base.url or event.url
        else:
            merged[event.external_id] = event.model_copy(deep=True)
    return list(merged.values())


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def split_market(
    market: CollectedMarket, event: CollectedEvent, mapper: MarketMapper
) -> list[tuple[str, str, list[CollectedSelection]]]:
    """``[(raw_name, external_id, selections)]`` - one entry per distinct spec signature."""
    names = [s.raw_name for s in market.selections]
    groups: dict[str | None, list[CollectedSelection]] = {}
    for sel in market.selections:
        sig = mapper.group_key(market.raw_name, sel.raw_name, event=event, sibling_selections=names)
        groups.setdefault(sig, []).append(sel)
    distinct = [sig for sig in groups if sig is not None]
    raw_name = truncate(market.raw_name, 256) or market.raw_name
    base_id = truncate(market.external_id, 64) or ""
    if len(distinct) <= 1:
        return [(raw_name, base_id, list(market.selections))]
    out: list[tuple[str, str, list[CollectedSelection]]] = []
    for sig, sels in groups.items():
        ext = base_id if sig is None else f"{base_id[:52]}{SPLIT_SEPARATOR}{_short_hash(sig)}"
        out.append((raw_name, ext, sels))
    return out


def _snapshot(run: _Run, row: BookmakerSelection, odds: Decimal, available: bool) -> None:
    run.session.add(OddsSnapshot(selection_id=row.id, odds=odds, is_available=available, captured_at=run.now))
    run.report.snapshots_written += 1


def _observe(run: _Run, row: BookmakerSelection, odds: Decimal, available: bool) -> None:
    price = quantize_odds(odds)
    last = quantize_odds(Decimal(row.last_odds)) if row.last_odds is not None else None
    if last is None or last != price or bool(row.is_available) != available:
        _snapshot(run, row, price, available)
    row.last_odds = price
    row.last_odds_at = run.now
    row.is_available = available


def _mark_unavailable(run: _Run, row: BookmakerSelection) -> None:
    if not row.is_available:
        return
    if row.last_odds is not None:
        _snapshot(run, row, quantize_odds(Decimal(row.last_odds)), False)
    row.is_available = False
    run.report.selections_marked_unavailable += 1


def _apply_mapping(
    market_row: BookmakerMarket, mapped: Sequence[MappedSelection | None]
) -> tuple[str | None, list[bool]]:
    """Set the market's spec to the most common mapped spec; returns (spec key, per-selection ok)."""
    counts = Counter(m.spec.key() for m in mapped if m is not None)
    if not counts:
        market_row.spec = None
        market_row.spec_key = None
        market_row.market_type = None
        market_row.is_mapped = False
        return None, [False] * len(mapped)
    top_key = counts.most_common(1)[0][0]
    top = next(m for m in mapped if m is not None and m.spec.key() == top_key)
    market_row.spec = top.spec.model_dump(mode="json")
    market_row.spec_key = truncate(top_key, 256)
    market_row.market_type = top.spec.market_type.value
    market_row.is_mapped = True
    return top_key, [m is not None and m.spec.key() == top_key for m in mapped]


# --------------------------------------------------------------------------- per event


def _upsert_event(run: _Run, ce: CollectedEvent, partial: bool) -> tuple[BookmakerEvent, bool]:
    session = run.session
    external_id = truncate(ce.external_id, 64) or ce.external_id
    row = session.scalar(
        select(BookmakerEvent)
        .options(selectinload(BookmakerEvent.markets).selectinload(BookmakerMarket.selections))
        .where(BookmakerEvent.bookmaker == Bookmaker.BETCLIC, BookmakerEvent.external_id == external_id)
    )
    is_new = row is None
    if row is None:
        row = BookmakerEvent(
            bookmaker=Bookmaker.BETCLIC,
            external_id=external_id,
            name=truncate(ce.name, 256) or f"{ce.team1_raw} - {ce.team2_raw}",
            team1_raw=truncate(ce.team1_raw, 128) or ce.team1_raw,
            team2_raw=truncate(ce.team2_raw, 128) or ce.team2_raw,
            first_seen_at=run.now,
            last_seen_at=run.now,
            link_status=LinkStatus.UNLINKED,
            teams_swapped=False,
            is_live=ce.is_live,
            extra={},
        )
        session.add(row)
    row.name = truncate(ce.name, 256) or row.name
    row.team1_raw = truncate(ce.team1_raw, 128) or row.team1_raw
    row.team2_raw = truncate(ce.team2_raw, 128) or row.team2_raw
    if ce.competition_raw:
        row.competition_raw = truncate(ce.competition_raw, 256)
    if ce.starts_at is not None:
        row.starts_at = ce.starts_at
    if ce.url:
        row.url = truncate(ce.url, 1024)
    row.is_live = ce.is_live
    row.last_seen_at = run.now
    extra = dict(row.extra or {})
    extra["last_collected_at"] = run.now.isoformat()
    extra["partial"] = partial
    paths = run.info.raw_paths_by_event.get(ce.external_id, []) if run.info else []
    if paths:
        extra["raw_payloads"] = paths[-run.config.max_raw_paths_per_event :]
        extra["raw_payloads_at"] = run.now.isoformat()
    row.extra = extra
    session.flush()
    return row, is_new


def _sync_market(
    run: _Run,
    ce: CollectedEvent,
    market_row: BookmakerMarket,
    selections: list[CollectedSelection],
    swapped: bool,
    resolver: PlayerResolver | None,
    partial: bool,
) -> None:
    session = run.session
    names = [s.raw_name for s in selections]
    mapped = [
        run.mapper.map(
            market_row.raw_name,
            s.raw_name,
            event=ce,
            swapped=swapped,
            player_resolver=resolver,
            sibling_selections=names,
        )
        for s in selections
    ]
    _, ok_flags = _apply_mapping(market_row, mapped)
    if market_row.is_mapped:
        run.report.markets_mapped += 1
    else:
        run.unmapped.setdefault(market_row.raw_name, None)
    existing = {(s.raw_name, s.external_id): s for s in market_row.selections}
    pending: list[tuple[BookmakerSelection, CollectedSelection]] = []
    created = False
    for sel, mp, ok in zip(selections, mapped, ok_flags, strict=True):
        key = (truncate(sel.raw_name, 256) or sel.raw_name, truncate(sel.external_id, 64) or "")
        row = existing.get(key)
        if row is None:
            row = BookmakerSelection(raw_name=key[0], external_id=key[1], is_available=sel.is_available)
            market_row.selections.append(row)
            existing[key] = row
            created = True
            run.report.selections_new += 1
        row.selection_key = mp.selection_key if (ok and mp is not None) else None
        row.is_mapped = bool(ok and mp is not None)
        if row.is_mapped:
            run.report.selections_mapped += 1
        pending.append((row, sel))
        run.report.selections_seen += 1
    if created:
        session.flush()
    seen_rows = set()
    for row, sel in pending:
        if id(row) in seen_rows:
            continue  # duplicate selection in the payload: first observation wins
        seen_rows.add(id(row))
        _observe(run, row, sel.odds, sel.is_available)
    if not partial:
        for row in market_row.selections:
            if id(row) not in seen_rows:
                _mark_unavailable(run, row)


def _process_event(run: _Run, ce: CollectedEvent) -> None:
    session = run.session
    run.now = run.event_time(ce)
    partial = bool(run.info and ce.external_id in run.info.partial_event_ids)
    row, is_new = _upsert_event(run, ce, partial)
    if is_new:
        run.report.events_new += 1
    if partial:
        run.report.events_partial += 1
    link = link_event(session, row, settings=run.settings, now=run.now, config=run.config.link)
    status_key = row.link_status.value
    run.report.link_status_counts[status_key] = run.report.link_status_counts.get(status_key, 0) + 1
    if link.changed and not is_new:
        run.report.events_relinked += 1
    if ce.is_live and not run.config.store_live_odds:
        run.report.events_live += 1
        return
    swapped = bool(row.teams_swapped) if row.match_id is not None else False
    resolver = run.resolver_for(row.match_id)

    # group the incoming markets by natural key (duplicates are merged, later selections win)
    incoming: dict[tuple[str, str], dict[tuple[str, str], CollectedSelection]] = {}
    for market in ce.markets:
        for raw_name, ext_id, sels in split_market(market, ce, run.mapper):
            bucket = incoming.setdefault((raw_name, ext_id), {})
            for sel in sels:
                bucket[(sel.raw_name, sel.external_id)] = sel

    existing = {(m.raw_name, m.external_id): m for m in row.markets}
    seen_markets: set[int] = set()
    for (raw_name, ext_id), sel_map in incoming.items():
        market_row = existing.get((raw_name, ext_id))
        if market_row is None:
            market_row = BookmakerMarket(
                raw_name=raw_name,
                external_id=ext_id,
                first_seen_at=run.now,
                last_seen_at=run.now,
                is_mapped=False,
            )
            row.markets.append(market_row)
            existing[(raw_name, ext_id)] = market_row
            session.flush()
            run.report.markets_new += 1
        market_row.last_seen_at = run.now
        seen_markets.add(id(market_row))
        run.report.markets_seen += 1
        _sync_market(run, ce, market_row, list(sel_map.values()), swapped, resolver, partial)

    if not partial:
        for market_row in row.markets:
            if id(market_row) in seen_markets:
                continue
            if any(s.is_available for s in market_row.selections):
                run.report.markets_disappeared += 1
            for sel_row in market_row.selections:
                _mark_unavailable(run, sel_row)
    session.flush()


# --------------------------------------------------------------------------- public API


def collect_once(
    session: Session,
    source: OddsSource,
    mapper: MarketMapper,
    *,
    settings: Settings,
    now: datetime | None = None,
    player_resolver_factory: PlayerResolverFactory | None = None,
    config: CollectorConfig | None = None,
) -> CollectReport:
    """One collection pass (see module docstring). The caller owns the transaction (commit).

    Leave ``now`` as ``None`` in production: prices are then stamped with the time the source
    captured them (``FetchInfo.captured_at_by_event``), falling back to the clock right after the
    crawl. An explicit ``now`` stamps everything with that instant (tests, replays).
    """
    report = CollectReport(started_at=now or utcnow())
    events = source.fetch_cs2_events()
    info = source.last_fetch_info() if isinstance(source, SupportsFetchInfo) else None
    after_fetch = now or utcnow()
    run = _Run(
        session=session,
        mapper=mapper,
        settings=settings,
        now=after_fetch,
        config=config or CollectorConfig(),
        report=report,
        info=info,
        factory=player_resolver_factory or lineup_player_resolver,
        explicit_now=now,
        fallback_now=after_fetch,
    )
    if info is not None:
        report.source = info.summary()
    for ce in _merge_duplicate_events(events):
        report.events_seen += 1
        try:
            with session.begin_nested():
                _process_event(run, ce)
        except Exception as exc:
            report.events_failed += 1
            report.errors.append(f"event {ce.external_id}: {type(exc).__name__}: {exc}")
            logger.warning("failed to store Betclic event %s", ce.external_id, exc_info=True)
    session.flush()
    report.unmapped_markets = list(run.unmapped)[: run.config.max_unmapped_in_report]
    report.finished_at = utcnow()
    logger.info(
        "Betclic collection: %d events (%d new, %d live, %d failed), %d markets (%d mapped), "
        "%d snapshots, %d marked unavailable",
        report.events_seen,
        report.events_new,
        report.events_live,
        report.events_failed,
        report.markets_seen,
        report.markets_mapped,
        report.snapshots_written,
        report.selections_marked_unavailable,
    )
    return report


def remap_event(
    session: Session,
    event_row: BookmakerEvent,
    mapper: MarketMapper,
    *,
    player_resolver: PlayerResolver | None = None,
) -> int:
    """Re-map an event's stored markets from their raw names (e.g. after a manual link change).

    No prices are touched. Returns the number of selections whose mapping changed. When
    ``player_resolver`` is omitted the match lineups are used.
    """
    event = CollectedEvent(
        external_id=event_row.external_id,
        name=event_row.name,
        team1_raw=event_row.team1_raw,
        team2_raw=event_row.team2_raw,
    )
    swapped = bool(event_row.teams_swapped) if event_row.match_id is not None else False
    resolver = player_resolver
    if resolver is None and event_row.match_id is not None:
        resolver = lineup_player_resolver(session, event_row.match_id)
    changed = 0
    for market_row in event_row.markets:
        sels = list(market_row.selections)
        names = [s.raw_name for s in sels]
        mapped = [
            mapper.map(
                market_row.raw_name,
                s.raw_name,
                event=event,
                swapped=swapped,
                player_resolver=resolver,
                sibling_selections=names,
            )
            for s in sels
        ]
        _, ok_flags = _apply_mapping(market_row, mapped)
        for sel_row, mp, ok in zip(sels, mapped, ok_flags, strict=True):
            new_key = mp.selection_key if (ok and mp is not None) else None
            if new_key != sel_row.selection_key or bool(ok and mp is not None) != sel_row.is_mapped:
                changed += 1
            sel_row.selection_key = new_key
            sel_row.is_mapped = new_key is not None
    session.flush()
    return changed
