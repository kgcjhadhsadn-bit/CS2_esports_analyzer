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
     by (market_id, raw_name, external_id), re-mapping every market seen on every pass (so edited
     YAML rules apply immediately);
   * when the link or the swap flag changed, or the dashboard's link review left
     ``event.extra["remap_required"]`` (docs/dev/reports/api.md), re-map **every stored market**
     of the event - also the ones this pass did not see (a listing-only sighting shows just the
     match winner): a selection keyed for the old orientation must never survive a relink. Flagged
     events that are no longer listed are re-mapped at the end of the pass;
   * write an ``odds_snapshots`` row only when the odds or the availability differ from
     ``selection.last_odds`` / ``is_available``. ``last_seen_at`` is refreshed on every sighting;
     ``last_odds_at`` ("price last confirmed at", what the quote freshness check uses) on every
     sighting of the selection as *available*. Timestamps are the source's capture times -
     per market when the source reports them (``FetchInfo.captured_at_by_market``), else per
     event (``CollectedEvent.captured_at`` / ``FetchInfo.captured_at_by_event``) - because a
     browser crawl takes minutes: stamping with the run start would make fresh prices look stale
     and could put a pre-kick-off price on the wrong side of the closing-line cut-off, and
     stamping a market with a later payload that did not contain it would make a stale price look
     fresh. An observation older than what is already stored for a selection (a replayed
     archive) is ignored, so the price history only moves forward;
   * selections that vanished from a refreshed event (whole market gone, or a single selection
     gone) are marked unavailable with a snapshot - unless the source reported the event as
     partial (e.g. only seen on the listing page).

The mapper is re-bound to the live settlement assumptions on every pass
(``settings.betclic.totals_include_overtime`` / ``h2h_tie_rule``, see
:meth:`MarketMapper.with_settlement`), so the mapping can never disagree with the pricing engine.

``bookmaker_markets.extra`` records ``betclic_market_id`` (Betclic's own id), ``rule`` (the id of
the YAML rule that mapped the market, or - while it is unmapped - of the first rule whose patterns
fit its name; for ``cs2bot odds unmapped`` diagnostics) and, for split rows, ``split_signature``;
when the parser built the name from a map group and the market's own title (``"Mapa 1 - Liczba
rund"``) also ``page_name`` / ``group`` - the text the page really shows.
``bookmaker_events.extra`` records ``sport_raw`` (the payload's own sport label, which
:func:`cs2bot.odds.event_matching.link_event` uses to keep other e-sports from being linked to a
CS2 match), ``last_collected_at``, ``partial`` and the archived ``raw_payloads``.

Series format: a match-winner market that lists a draw is only mapped when the linked match is a
BO2 (``draw_possible``, see :mod:`cs2bot.odds.market_mapping`); the format comes from
``matches.format`` and is unknown (no check) while the event is unlinked.

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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from cs2bot.config import Settings
from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, Match, OddsSnapshot
from cs2bot.domain.enums import Bookmaker, LinkStatus, MatchFormat
from cs2bot.domain.odds import CollectedEvent, CollectedMarket, CollectedSelection, MappedSelection
from cs2bot.odds.event_matching import MANUAL_STATUSES, LinkConfig, link_event
from cs2bot.odds.market_mapping import MarketMapper, PlayerResolver, lineup_player_resolver
from cs2bot.odds.sources import FetchInfo, OddsSource, SupportsFetchInfo
from cs2bot.odds.text import MAX_ODDS, MIN_ODDS_EXCLUSIVE, quantize_odds, truncate
from cs2bot.utils.time import ensure_utc, utcnow

logger = logging.getLogger(__name__)

SPLIT_SEPARATOR = "~"
REMAP_REQUIRED_KEY = "remap_required"  # set by the API's link review (cs2bot.api.routes.review)
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
    # how far back unlisted events flagged ``remap_required`` are still looked for
    remap_lookback_days: int = 14


class CollectReport(BaseModel):
    started_at: datetime
    finished_at: datetime | None = None
    events_seen: int = 0
    events_new: int = 0
    events_failed: int = 0
    events_live: int = 0
    events_partial: int = 0
    events_relinked: int = 0
    events_remapped: int = 0  # every stored market re-mapped (link / swap change or manual review)
    link_status_counts: dict[str, int] = Field(default_factory=dict)
    markets_seen: int = 0
    markets_new: int = 0
    markets_mapped: int = 0
    markets_disappeared: int = 0
    selections_seen: int = 0
    selections_new: int = 0
    selections_mapped: int = 0
    selections_marked_unavailable: int = 0
    selections_stale_ignored: int = 0  # observations older than the stored one (replayed archives)
    selections_invalid: int = 0  # dropped: no name, or odds that are not a price in (1, 1000]
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
    draw_flags: dict[int, bool | None] = field(default_factory=dict)
    unmapped: dict[str, None] = field(default_factory=dict)
    fallback_now: datetime | None = None
    processed_event_ids: set[int] = field(default_factory=set)

    def event_time(self, ce: CollectedEvent) -> datetime:
        """Observation time of one event: explicit ``now`` > the source's capture time > post-fetch clock.

        Capture times are clamped to the post-fetch clock: a price can never be stamped in the future.
        """
        if self.explicit_now is not None:
            return self.explicit_now
        fallback = self.fallback_now or self.now
        captured = ce.captured_at
        if captured is None and self.info is not None:
            captured = self.info.captured_at_by_event.get(ce.external_id)
        return min(ensure_utc(captured), fallback) if captured is not None else fallback

    def market_time(self, ce: CollectedEvent, market: CollectedMarket) -> datetime:
        """Observation time of one market: its own freshest payload when the source reports it."""
        if self.explicit_now is not None:
            return self.explicit_now
        captured = None
        if self.info is not None:
            captured = self.info.captured_at_by_market.get(ce.external_id, {}).get(
                (market.raw_name, market.external_id)
            )
        return min(ensure_utc(captured), self.now) if captured is not None else self.now

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

    def draw_possible_for(self, match_id: int | None) -> bool | None:
        if match_id is None:
            return None
        if match_id not in self.draw_flags:
            self.draw_flags[match_id] = series_draw_possible(self.session, match_id)
        return self.draw_flags[match_id]


# --------------------------------------------------------------------------- helpers


def series_draw_possible(session: Session, match_id: int | None) -> bool | None:
    """Can the linked series end level? ``True`` for a BO2, ``False`` for BO1/BO3/BO5, ``None`` when
    the event is unlinked or the format is unknown (the mapper then skips the check)."""
    if match_id is None:
        return None
    fmt = session.scalar(select(Match.format).where(Match.id == match_id))
    if fmt is None:
        return None
    return MatchFormat(fmt) is MatchFormat.BO2


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
            base.sport_raw = base.sport_raw or event.sport_raw
            if event.captured_at is not None and (
                base.captured_at is None or event.captured_at > base.captured_at
            ):
                base.captured_at = event.captured_at
        else:
            merged[event.external_id] = event.model_copy(deep=True)
    return list(merged.values())


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _valid_selection(sel: CollectedSelection) -> bool:
    """A named selection with a real decimal price (the DB column is ``Numeric(8, 3)``)."""
    odds = sel.odds
    return bool(sel.raw_name.strip()) and odds.is_finite() and MIN_ODDS_EXCLUSIVE < odds <= MAX_ODDS


def split_market(
    market: CollectedMarket, event: CollectedEvent, mapper: MarketMapper
) -> list[tuple[str, str, list[CollectedSelection]]]:
    """``[(raw_name, external_id, selections)]`` - one entry per distinct spec signature.

    The selections of every entry are mapped with the names of ALL selections of the original
    market as siblings (three-way detection must see the "Remis" row even when it lands in
    another entry).
    """
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


def _snapshot(run: _Run, row: BookmakerSelection, odds: Decimal, available: bool, at: datetime) -> None:
    run.session.add(OddsSnapshot(selection_id=row.id, odds=odds, is_available=available, captured_at=at))
    run.report.snapshots_written += 1


def _last_observed_at(row: BookmakerSelection) -> datetime | None:
    """Latest time anything was recorded for a selection (rows written before ``last_seen_at``
    existed only carry ``last_odds_at``)."""
    known = [t for t in (row.last_seen_at, row.last_odds_at) if t is not None]
    return max(known) if known else None


def _observe(run: _Run, row: BookmakerSelection, odds: Decimal, available: bool, at: datetime) -> None:
    """Record one sighting of a selection at ``at`` (snapshot only when price/availability changed)."""
    previous = _last_observed_at(row)
    if previous is not None and at < previous:
        run.report.selections_stale_ignored += 1  # older than what we have: history only moves forward
        return
    price = quantize_odds(odds)
    last = quantize_odds(Decimal(row.last_odds)) if row.last_odds is not None else None
    if last is None or last != price or bool(row.is_available) != available:
        _snapshot(run, row, price, available, at)
    row.last_odds = price
    row.last_seen_at = at
    if available:
        row.last_odds_at = at  # "price last confirmed at": only an available sighting confirms it
    row.is_available = available


def _mark_unavailable(run: _Run, row: BookmakerSelection, at: datetime) -> None:
    if not row.is_available:
        return
    previous = _last_observed_at(row)
    if previous is not None and at < previous:
        return  # the sighting that misses this selection is older than the one that saw it
    if row.last_odds is not None:
        _snapshot(run, row, quantize_odds(Decimal(row.last_odds)), False, at)
    row.is_available = False
    run.report.selections_marked_unavailable += 1


def _set_market_extra(
    market_row: BookmakerMarket,
    mapper: MarketMapper,
    signature: str | None,
    mapped_rule: str | None,
    meta: dict[str, str] | None = None,
) -> None:
    """Keep ``bookmaker_markets.extra`` in sync (written only when something changed).

    ``mapped_rule`` is the rule that mapped the market's selections; for an unmapped market the
    first rule whose patterns fit the name is recorded instead (the candidate to tune).
    """
    wanted: dict[str, Any] = dict(market_row.extra or {})
    wanted["betclic_market_id"] = original_market_external_id(market_row.external_id)
    rule = mapped_rule if mapped_rule is not None else mapper.match_rule(market_row.raw_name)
    if rule is not None:
        wanted["rule"] = rule
    else:
        wanted.pop("rule", None)
    if signature is not None and SPLIT_SEPARATOR in market_row.external_id:
        wanted["split_signature"] = signature
    for key in ("page_name", "group"):
        if meta and meta.get(key):
            wanted[key] = truncate(meta[key], 256)
    if wanted != (market_row.extra or {}):
        market_row.extra = wanted


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
    # Row lock + fresh read: the dashboard's link review (another transaction) may confirm / reject
    # this very event while the pass runs. Without the lock a stale copy read here would be written
    # back at commit and silently undo the manual decision (PostgreSQL; SQLite ignores FOR UPDATE).
    row = session.scalar(
        select(BookmakerEvent)
        .options(selectinload(BookmakerEvent.markets).selectinload(BookmakerMarket.selections))
        .where(BookmakerEvent.bookmaker == Bookmaker.BETCLIC, BookmakerEvent.external_id == external_id)
        .with_for_update(of=BookmakerEvent)
        .execution_options(populate_existing=True)
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
    if row.last_seen_at is None or run.now > row.last_seen_at:
        row.last_seen_at = run.now  # never moves backwards (replayed archives)
    extra = dict(row.extra or {})
    extra["last_collected_at"] = run.now.isoformat()
    extra["partial"] = partial
    if ce.sport_raw:
        extra["sport_raw"] = truncate(ce.sport_raw, 128)
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
    at: datetime,
    siblings: Sequence[str] | None = None,
    signature: str | None = None,
    draw_possible: bool | None = None,
) -> None:
    session = run.session
    names = list(siblings) if siblings is not None else [s.raw_name for s in selections]
    mapped = [
        run.mapper.map(
            market_row.raw_name,
            s.raw_name,
            event=ce,
            swapped=swapped,
            player_resolver=resolver,
            sibling_selections=names,
            draw_possible=draw_possible,
        )
        for s in selections
    ]
    spec_before = market_row.spec_key
    _, ok_flags = _apply_mapping(market_row, mapped)
    spec_changed = market_row.spec_key != spec_before
    mapped_rule: str | None = None
    if market_row.is_mapped:
        run.report.markets_mapped += 1
        first_ok = next(s for s, ok in zip(selections, ok_flags, strict=True) if ok)
        mapped_rule = run.mapper.mapping_rule(
            market_row.raw_name,
            first_ok.raw_name,
            event=ce,
            sibling_selections=names,
            draw_possible=draw_possible,
        )
    else:
        run.unmapped.setdefault(market_row.raw_name, None)
    meta = None
    if run.info is not None:
        meta = run.info.market_meta_by_event.get(ce.external_id, {}).get(
            (market_row.raw_name, original_market_external_id(market_row.external_id))
        )
    _set_market_extra(market_row, run.mapper, signature, mapped_rule, meta)
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
        _observe(run, row, sel.odds, sel.is_available, at)
    for row in market_row.selections:
        if id(row) in seen_rows:
            continue
        if spec_changed and row.is_mapped:
            # the market now maps to another spec (edited rules, new lineups, format known): a
            # selection this pass did not see must not keep a key that belonged to the old spec
            row.selection_key = None
            row.is_mapped = False
        if not partial:
            _mark_unavailable(run, row, at)


def _process_event(run: _Run, ce: CollectedEvent) -> None:
    session = run.session
    run.now = run.event_time(ce)
    partial = bool(run.info and ce.external_id in run.info.partial_event_ids)
    row, is_new = _upsert_event(run, ce, partial)
    if is_new:
        run.report.events_new += 1
    if partial:
        run.report.events_partial += 1
    run.processed_event_ids.add(row.id)
    link = link_event(session, row, settings=run.settings, now=run.now, config=run.config.link)
    status_key = row.link_status.value
    run.report.link_status_counts[status_key] = run.report.link_status_counts.get(status_key, 0) + 1
    if link.changed and not is_new:
        run.report.events_relinked += 1
    swapped = bool(row.teams_swapped) if row.match_id is not None else False
    resolver = run.resolver_for(row.match_id)
    draw_possible = run.draw_possible_for(row.match_id)
    if not is_new and (link.changed or (row.extra or {}).get(REMAP_REQUIRED_KEY)):
        # The match / orientation changed (or a manual review cleared the mappings): every STORED
        # market is re-keyed now, including those this pass does not see.
        _remap_rows(session, row, run.mapper, resolver)
        _clear_remap_flag(row)
        run.report.events_remapped += 1
    if ce.is_live and not run.config.store_live_odds:
        run.report.events_live += 1
        return

    # group the incoming markets by natural key (duplicates are merged, later selections win)
    incoming: dict[tuple[str, str], dict[tuple[str, str], CollectedSelection]] = {}
    observed_at: dict[tuple[str, str], datetime] = {}
    signatures: dict[tuple[str, str], str | None] = {}
    sibling_names: dict[tuple[str, str], dict[str, None]] = {}
    for market in ce.markets:
        valid = [s for s in market.selections if _valid_selection(s)]
        run.report.selections_invalid += len(market.selections) - len(valid)
        if not market.raw_name.strip() or not valid:
            continue  # nothing storable: one malformed market must not fail the whole event
        if len(valid) != len(market.selections):
            market = market.model_copy(update={"selections": valid})
        at = run.market_time(ce, market)
        all_names = [s.raw_name for s in market.selections]
        for raw_name, ext_id, sels in split_market(market, ce, run.mapper):
            key = (raw_name, ext_id)
            bucket = incoming.setdefault(key, {})
            for sel in sels:
                bucket[(sel.raw_name, sel.external_id)] = sel
            observed_at[key] = max(at, observed_at[key]) if key in observed_at else at
            sibling_names.setdefault(key, {}).update(dict.fromkeys(all_names))
            if key not in signatures and sels:
                signatures[key] = run.mapper.group_key(
                    market.raw_name, sels[0].raw_name, event=ce, sibling_selections=all_names
                )

    existing = {(m.raw_name, m.external_id): m for m in row.markets}
    seen_markets: set[int] = set()
    for (raw_name, ext_id), sel_map in incoming.items():
        at = observed_at[(raw_name, ext_id)]
        market_row = existing.get((raw_name, ext_id))
        if market_row is None:
            market_row = BookmakerMarket(
                raw_name=raw_name,
                external_id=ext_id,
                first_seen_at=at,
                last_seen_at=at,
                is_mapped=False,
                extra={},
            )
            row.markets.append(market_row)
            existing[(raw_name, ext_id)] = market_row
            session.flush()
            run.report.markets_new += 1
        if market_row.last_seen_at is None or at > market_row.last_seen_at:
            market_row.last_seen_at = at
        seen_markets.add(id(market_row))
        run.report.markets_seen += 1
        _sync_market(
            run,
            ce,
            market_row,
            list(sel_map.values()),
            swapped,
            resolver,
            partial,
            at,
            siblings=list(sibling_names[(raw_name, ext_id)]),
            signature=signatures.get((raw_name, ext_id)),
            draw_possible=draw_possible,
        )

    if not partial:
        for market_row in row.markets:
            if id(market_row) in seen_markets:
                continue
            if any(s.is_available for s in market_row.selections):
                run.report.markets_disappeared += 1
            for sel_row in market_row.selections:
                _mark_unavailable(run, sel_row, run.now)
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
    mapper = _bind_settlement(mapper, settings)
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
    _remap_flagged_events(run, after_fetch)
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


def _bind_settlement(mapper: MarketMapper, settings: Settings) -> MarketMapper:
    """The mapper bound to the settlement assumptions the pricing engine reads from ``settings``."""
    return mapper.with_settlement(
        totals_include_overtime=settings.betclic.totals_include_overtime,
        h2h_tie_rule=settings.betclic.h2h_tie_rule,
    )


def _clear_remap_flag(event_row: BookmakerEvent) -> None:
    extra = dict(event_row.extra or {})
    if extra.pop(REMAP_REQUIRED_KEY, None) is not None:
        event_row.extra = extra  # reassigned so the JSON change is persisted


def _remap_rows(
    session: Session, event_row: BookmakerEvent, mapper: MarketMapper, resolver: PlayerResolver | None
) -> int:
    """Re-map every stored market of an event from its raw names; returns changed selections."""
    event = CollectedEvent(
        external_id=event_row.external_id,
        name=event_row.name,
        team1_raw=event_row.team1_raw,
        team2_raw=event_row.team2_raw,
    )
    swapped = bool(event_row.teams_swapped) if event_row.match_id is not None else False
    draw_possible = series_draw_possible(session, event_row.match_id)
    # rows split off one bookmaker market share their siblings (three-way detection)
    siblings: dict[tuple[str, str], list[str]] = {}
    for market_row in event_row.markets:
        key = (market_row.raw_name, original_market_external_id(market_row.external_id))
        siblings.setdefault(key, []).extend(s.raw_name for s in market_row.selections)
    changed = 0
    for market_row in event_row.markets:
        sels = list(market_row.selections)
        names = siblings[(market_row.raw_name, original_market_external_id(market_row.external_id))]
        mapped = [
            mapper.map(
                market_row.raw_name,
                s.raw_name,
                event=event,
                swapped=swapped,
                player_resolver=resolver,
                sibling_selections=names,
                draw_possible=draw_possible,
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


def _remap_flagged_events(run: _Run, now: datetime) -> None:
    """Re-map events the link review flagged (``remap_required``) that this pass did not see."""
    cutoff = now - timedelta(days=run.config.remap_lookback_days)
    query = (
        select(BookmakerEvent)
        .options(selectinload(BookmakerEvent.markets).selectinload(BookmakerMarket.selections))
        .where(
            BookmakerEvent.bookmaker == Bookmaker.BETCLIC,
            BookmakerEvent.link_status.in_(tuple(MANUAL_STATUSES)),
            BookmakerEvent.last_seen_at >= cutoff,
        )
        .with_for_update(of=BookmakerEvent)
        .execution_options(populate_existing=True)
    )
    if run.processed_event_ids:
        query = query.where(BookmakerEvent.id.not_in(run.processed_event_ids))
    for event_row in run.session.scalars(query):
        if not (event_row.extra or {}).get(REMAP_REQUIRED_KEY):
            continue
        try:
            with run.session.begin_nested():
                _remap_rows(run.session, event_row, run.mapper, run.resolver_for(event_row.match_id))
                _clear_remap_flag(event_row)
                run.report.events_remapped += 1
        except Exception as exc:
            run.report.errors.append(f"remap of event {event_row.external_id}: {type(exc).__name__}: {exc}")
            logger.warning("failed to re-map Betclic event %s", event_row.external_id, exc_info=True)


def remap_event(
    session: Session,
    event_row: BookmakerEvent,
    mapper: MarketMapper,
    *,
    player_resolver: PlayerResolver | None = None,
    settings: Settings | None = None,
) -> int:
    """Re-map an event's stored markets from their raw names (e.g. after a manual link change).

    No prices are touched and ``extra["remap_required"]`` is cleared. Returns the number of
    selections whose mapping changed. When ``player_resolver`` is omitted the match lineups are
    used. Pass ``settings`` so the mapper is bound to the live settlement assumptions
    (``betclic.totals_include_overtime`` / ``h2h_tie_rule``); without it the mapper's own apply.
    """
    if settings is not None:
        mapper = _bind_settlement(mapper, settings)
    resolver = player_resolver
    if resolver is None and event_row.match_id is not None:
        resolver = lineup_player_resolver(session, event_row.match_id)
    changed = _remap_rows(session, event_row, mapper, resolver)
    _clear_remap_flag(event_row)
    session.flush()
    return changed
