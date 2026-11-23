"""Read side of the odds store: prices handed to the pricing/decision engine, closing lines, history.

Only selections that are safe to bet are quoted (:func:`latest_quotes`):

* the Betclic event is linked ``AUTO`` or ``CONFIRMED`` to the match (``PENDING_REVIEW`` links
  are never bet on - PLAN.md section 4.4), it is not live and its start time (if known) is still
  in the future;
* the market and the selection are mapped and the selection is currently available;
* the price is fresh: ``last_odds_at >= now - max_age_s``. ``last_odds_at`` means "price last
  confirmed at": the collector refreshes it on every pass that saw the selection *available*
  (not only when the price changed), stamped with the time the source captured the payload - not
  the time the pass ran. ``last_seen_at`` (any sighting, also suspended) is not used for freshness.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, OddsSnapshot
from cs2bot.domain.enums import LinkStatus
from cs2bot.domain.markets import MarketSpec
from cs2bot.domain.odds import OddsQuote
from cs2bot.odds.collector import original_market_external_id
from cs2bot.utils.time import utcnow

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_S = 600.0  # BetclicSettings.odds_max_age_s default
_LINKED = (LinkStatus.AUTO, LinkStatus.CONFIRMED)


def _quote(sel: BookmakerSelection, market: BookmakerMarket, event: BookmakerEvent) -> OddsQuote | None:
    if market.spec is None or sel.selection_key is None or sel.last_odds is None or sel.last_odds_at is None:
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
        odds=Decimal(sel.last_odds),
        captured_at=sel.last_odds_at,
        is_available=bool(sel.is_available),
    )


def latest_quotes(
    session: Session,
    match_id: int,
    *,
    max_age_s: float | None = None,
    now: datetime | None = None,
) -> list[OddsQuote]:
    """Fresh, mapped, available quotes of every Betclic event safely linked to ``match_id``."""
    current = now or utcnow()
    cutoff = current - timedelta(seconds=DEFAULT_MAX_AGE_S if max_age_s is None else max_age_s)
    rows = session.execute(
        select(BookmakerSelection, BookmakerMarket, BookmakerEvent)
        .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
        .join(BookmakerEvent, BookmakerMarket.event_id == BookmakerEvent.id)
        .where(
            BookmakerEvent.match_id == match_id,
            BookmakerEvent.link_status.in_(_LINKED),
            BookmakerEvent.is_live.is_(False),
            BookmakerMarket.is_mapped.is_(True),
            BookmakerSelection.is_mapped.is_(True),
            BookmakerSelection.is_available.is_(True),
            BookmakerSelection.last_odds.is_not(None),
            BookmakerSelection.last_odds_at >= cutoff,
        )
        .order_by(BookmakerMarket.spec_key, BookmakerSelection.selection_key, BookmakerSelection.id)
    ).all()
    quotes: list[OddsQuote] = []
    for sel, market, event in rows:
        if event.starts_at is not None and event.starts_at <= current:
            continue  # kick-off passed: no pre-match price any more
        quote = _quote(sel, market, event)
        if quote is not None:
            quotes.append(quote)
    return quotes


def latest_quote(
    session: Session,
    selection_id: int,
    *,
    max_age_s: float | None = None,
    now: datetime | None = None,
) -> OddsQuote | None:
    """The current quote of one selection under the same safety rules as :func:`latest_quotes`."""
    match_id = session.scalar(
        select(BookmakerEvent.match_id)
        .join(BookmakerMarket, BookmakerMarket.event_id == BookmakerEvent.id)
        .join(BookmakerSelection, BookmakerSelection.market_id == BookmakerMarket.id)
        .where(BookmakerSelection.id == selection_id)
    )
    if match_id is None:
        return None
    for quote in latest_quotes(session, match_id, max_age_s=max_age_s, now=now):
        if quote.selection_id == selection_id:
            return quote
    return None


def closing_odds(session: Session, selection_id: int, kickoff: datetime) -> Decimal | None:
    """Closing line: the price of the last *available* snapshot strictly before ``kickoff``.

    Snapshots are written only on change, so the latest one before kick-off is the price that
    was on offer at kick-off. A snapshot recording the market's suspension carries the previous
    price, so it is skipped rather than treated as a new price. ``None`` if never quoted before.
    """
    snap = session.scalar(
        select(OddsSnapshot)
        .where(
            OddsSnapshot.selection_id == selection_id,
            OddsSnapshot.captured_at < kickoff,
            OddsSnapshot.is_available.is_(True),
        )
        .order_by(OddsSnapshot.captured_at.desc(), OddsSnapshot.id.desc())
        .limit(1)
    )
    return Decimal(snap.odds) if snap is not None else None


def odds_history(session: Session, selection_id: int) -> list[tuple[datetime, Decimal, bool]]:
    """Every recorded change ``(captured_at, odds, is_available)``, oldest first."""
    snaps = session.scalars(
        select(OddsSnapshot)
        .where(OddsSnapshot.selection_id == selection_id)
        .order_by(OddsSnapshot.captured_at, OddsSnapshot.id)
    )
    return [(s.captured_at, Decimal(s.odds), bool(s.is_available)) for s in snaps]
