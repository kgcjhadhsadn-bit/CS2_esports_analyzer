"""Bookmaker odds contracts (source-agnostic)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from cs2bot.domain.markets import MarketSpec


class CollectedSelection(BaseModel):
    external_id: str = ""
    raw_name: str
    odds: Decimal
    is_available: bool = True


class CollectedMarket(BaseModel):
    external_id: str = ""
    raw_name: str
    selections: list[CollectedSelection] = Field(default_factory=list)


class CollectedEvent(BaseModel):
    """One bookmaker event as scraped, before linking/mapping."""

    external_id: str
    name: str
    team1_raw: str
    team2_raw: str
    competition_raw: str | None = None
    starts_at: datetime | None = None
    url: str | None = None
    is_live: bool = False
    captured_at: datetime | None = None  # when this event's prices were read (UTC)
    sport_raw: str | None = None
    markets: list[CollectedMarket] = Field(default_factory=list)


class MappedSelection(BaseModel):
    """Result of mapping a bookmaker selection into our taxonomy (team1/team2 orientation)."""

    spec: MarketSpec
    selection_key: str


class OddsQuote(BaseModel):
    """Latest price for one mapped outcome, as handed to the pricing/decision engine."""

    selection_id: int  # bookmaker_selections.id
    bookmaker_event_id: int  # bookmaker_events.id
    external_event_id: str
    market_raw_name: str
    selection_raw_name: str
    market_external_id: str = ""
    selection_external_id: str = ""
    event_url: str | None = None
    spec: MarketSpec
    selection_key: str
    odds: Decimal
    captured_at: datetime
    is_available: bool = True
