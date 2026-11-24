"""Historical odds for the backtest: the :class:`OddsHistory` protocol and two implementations.

* :class:`InMemoryOddsHistory` - a list of timestamped :class:`OddsQuote` s per match (synthetic
  odds, odds imported from a comparison site, tests).
* :class:`DbOddsHistory` - the Betclic price history the odds collector stored (``odds_snapshots``),
  for events linked ``AUTO`` / ``CONFIRMED`` to our matches.

Conventions shared by both:

* ``quotes_before(match_id, t)`` returns, per outcome (``spec key # selection``), the **latest
  available** price captured strictly before ``t`` - exactly what the bot would have seen at ``t``.
  A price captured at or after ``t`` is never returned (look-ahead).
* ``closing_quote(match_id, key)`` (``key`` = ``outcome_key(spec, selection)``) is the last
  available price strictly before kick-off - the closing line used for CLV. It is only read after
  the bet was decided (for the report), never by a model.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.backtest.types import outcome_key
from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, Match, OddsSnapshot
from cs2bot.domain.enums import LinkStatus
from cs2bot.domain.markets import MarketSpec
from cs2bot.domain.odds import OddsQuote
from cs2bot.utils.time import ensure_utc

logger = logging.getLogger(__name__)

__all__ = ["DbOddsHistory", "InMemoryOddsHistory", "OddsHistory"]

_LINKED = (LinkStatus.AUTO, LinkStatus.CONFIRMED)


@runtime_checkable
class OddsHistory(Protocol):
    def quotes_before(self, match_id: int, t: datetime) -> list[OddsQuote]: ...

    def closing_quote(self, match_id: int, key: str) -> OddsQuote | None: ...


class InMemoryOddsHistory:
    """Odds history held in memory: ``add(match_id, quote)`` with ``quote.captured_at`` as time.

    ``kickoffs`` (match id -> start time) bounds the closing line; without it the closing quote is
    the latest quote of the outcome.
    """

    def __init__(self, kickoffs: dict[int, datetime] | None = None) -> None:
        self._quotes: dict[int, dict[str, list[OddsQuote]]] = defaultdict(lambda: defaultdict(list))
        self._kickoffs = {k: ensure_utc(v) for k, v in (kickoffs or {}).items()}
        self._sorted = True

    def add(self, match_id: int, quote: OddsQuote) -> None:
        if quote.captured_at.tzinfo is None:
            raise ValueError("quote.captured_at must be timezone-aware")
        self._quotes[match_id][outcome_key(quote.spec, quote.selection_key)].append(quote)
        self._sorted = False

    def add_many(self, match_id: int, quotes: Iterable[OddsQuote]) -> None:
        for q in quotes:
            self.add(match_id, q)

    def set_kickoff(self, match_id: int, kickoff: datetime) -> None:
        self._kickoffs[match_id] = ensure_utc(kickoff)

    def _sort(self) -> None:
        if not self._sorted:
            for per_match in self._quotes.values():
                for lst in per_match.values():
                    lst.sort(key=lambda q: ensure_utc(q.captured_at))
            self._sorted = True

    @staticmethod
    def _latest(quotes: list[OddsQuote], t: datetime) -> OddsQuote | None:
        best: OddsQuote | None = None
        for q in quotes:
            if ensure_utc(q.captured_at) >= t:
                break
            best = q if q.is_available else None  # suspended: no price until it is quoted again
        return best

    def quotes_before(self, match_id: int, t: datetime) -> list[OddsQuote]:
        self._sort()
        cutoff = ensure_utc(t)
        out: list[OddsQuote] = []
        for key in sorted(self._quotes.get(match_id, {})):
            q = self._latest(self._quotes[match_id][key], cutoff)
            if q is not None:
                out.append(q)
        return out

    def closing_quote(self, match_id: int, key: str) -> OddsQuote | None:
        self._sort()
        quotes = self._quotes.get(match_id, {}).get(key, [])
        if not quotes:
            return None
        kickoff = self._kickoffs.get(match_id)
        if kickoff is None:
            available = [q for q in quotes if q.is_available]
            return available[-1] if available else None
        best: OddsQuote | None = None
        for q in quotes:
            if ensure_utc(q.captured_at) >= kickoff:
                break
            if q.is_available:
                best = q
        return best

    def match_ids(self) -> list[int]:
        return sorted(self._quotes)


class DbOddsHistory:
    """Betclic price history from the database (read-only).

    Only mapped markets / selections of events linked ``AUTO`` or ``CONFIRMED`` to the match count.
    Prices come from ``odds_snapshots`` (written on change): the price on offer at ``t`` is the
    latest snapshot before ``t``; a snapshot marking the selection unavailable means no price.
    The kick-off for the closing line is the earlier of the event's ``starts_at`` and the match's
    ``scheduled_at``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._cache: dict[int, list[tuple[BookmakerSelection, BookmakerMarket, BookmakerEvent]]] = {}

    def _selections(self, match_id: int) -> list[tuple[BookmakerSelection, BookmakerMarket, BookmakerEvent]]:
        if match_id not in self._cache:
            rows = self._session.execute(
                select(BookmakerSelection, BookmakerMarket, BookmakerEvent)
                .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
                .join(BookmakerEvent, BookmakerMarket.event_id == BookmakerEvent.id)
                .where(
                    BookmakerEvent.match_id == match_id,
                    BookmakerEvent.link_status.in_(_LINKED),
                    BookmakerMarket.is_mapped.is_(True),
                    BookmakerSelection.is_mapped.is_(True),
                    BookmakerSelection.selection_key.is_not(None),
                )
                .order_by(BookmakerMarket.spec_key, BookmakerSelection.selection_key, BookmakerSelection.id)
            ).all()
            self._cache[match_id] = [(s, m, e) for s, m, e in rows]
        return self._cache[match_id]

    def _quote(
        self, sel: BookmakerSelection, market: BookmakerMarket, event: BookmakerEvent, snap: OddsSnapshot
    ) -> OddsQuote | None:
        from cs2bot.odds.collector import original_market_external_id

        if market.spec is None or sel.selection_key is None:
            return None
        try:
            spec = MarketSpec.model_validate(market.spec)
        except ValidationError:
            logger.warning("stored spec of market %s is invalid: %r", market.id, market.spec)
            return None
        return OddsQuote(
            selection_id=sel.id,
            bookmaker_event_id=event.id,
            external_event_id=event.external_id,
            market_raw_name=market.raw_name,
            selection_raw_name=sel.raw_name,
            market_external_id=original_market_external_id(market.external_id),
            selection_external_id=sel.external_id,
            event_url=event.url,
            spec=spec,
            selection_key=sel.selection_key,
            odds=Decimal(snap.odds),
            captured_at=ensure_utc(snap.captured_at),
            is_available=True,
        )

    def _snapshot_before(self, selection_id: int, t: datetime) -> OddsSnapshot | None:
        return self._session.scalar(
            select(OddsSnapshot)
            .where(OddsSnapshot.selection_id == selection_id, OddsSnapshot.captured_at < t)
            .order_by(OddsSnapshot.captured_at.desc(), OddsSnapshot.id.desc())
            .limit(1)
        )

    def quotes_before(self, match_id: int, t: datetime) -> list[OddsQuote]:
        cutoff = ensure_utc(t)
        out: list[OddsQuote] = []
        seen: set[str] = set()
        for sel, market, event in self._selections(match_id):
            snap = self._snapshot_before(sel.id, cutoff)
            if snap is None or not snap.is_available:
                continue
            q = self._quote(sel, market, event, snap)
            if q is None:
                continue
            key = outcome_key(q.spec, q.selection_key)
            if key in seen:  # the same outcome from a second linked event: keep the first
                continue
            seen.add(key)
            out.append(q)
        return out

    def kickoff(self, match_id: int) -> datetime | None:
        match = self._session.get(Match, match_id)
        times = [ensure_utc(match.scheduled_at)] if match is not None and match.scheduled_at else []
        for _, _, event in self._selections(match_id):
            if event.starts_at is not None:
                times.append(ensure_utc(event.starts_at))
        return min(times, default=None)

    def closing_quote(self, match_id: int, key: str) -> OddsQuote | None:
        kickoff = self.kickoff(match_id)
        if kickoff is None:
            return None
        for sel, market, event in self._selections(match_id):
            if market.spec is None or sel.selection_key is None:
                continue
            try:
                spec = MarketSpec.model_validate(market.spec)
            except ValidationError:
                continue
            if outcome_key(spec, sel.selection_key) != key:
                continue
            snap = self._session.scalar(
                select(OddsSnapshot)
                .where(
                    OddsSnapshot.selection_id == sel.id,
                    OddsSnapshot.captured_at < kickoff,
                    OddsSnapshot.is_available.is_(True),
                )
                .order_by(OddsSnapshot.captured_at.desc(), OddsSnapshot.id.desc())
                .limit(1)
            )
            if snap is not None:
                return self._quote(sel, market, event, snap)
        return None
