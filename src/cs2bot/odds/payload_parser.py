"""Generic, tolerant extraction of bookmaker events from arbitrary JSON payloads.

Betclic's exact payloads cannot be observed from outside Poland, so instead of a parser written
for one schema this module *recognises* events, markets and selections by shape, with
configurable key-name candidates (:class:`PayloadKeys`). It understands, among others:

* the historic begmedia "offering" REST API (``offer.cdn.begmedia.com/api/pub/v4|v5/events``,
  ``.../v2/competitions/{id}``): ``{"unifiedEvents": [{"id", "name", "date", "isLive",
  "contestants": [{"name"}], "competition": {"name"}, "markets": [{"id", "name", "mtc",
  "selections": [{"id", "name", "odds"}]}]}]}``;
* the Angular transfer state embedded in Betclic pages (``<script id="ng-state"
  type="application/json">``): gRPC responses decoded to JSON (``"grpc:N": {"requestHeaders",
  "response": {"payload": {"match": {"matchId", "matchDateUtc", "name", "competition",
  "categories": [{"id": "ca_..."}], ...}}}}``) where selections are ``{"id", "name", "odds",
  "betslipMarketId"}`` nested in ``mainSelections`` / ``selectionMatrix`` containers. A page also
  carries dozens of *other* matches (recommendations), so events are scoped to their own node;
* JSON produced by :func:`cs2bot.odds.grpc_web.decode_betclic_grpc_body` from gRPC-web bodies;
* flat tables with one row per selection (``eventId, home, away, market, selection, price``).

Recognition rules (all defensive - one malformed record never aborts the whole payload):

* **event**: a dict with an id key, two contestants (a ``contestants``-like list of exactly two,
  ``home``/``away``-like keys, or a ``"A - B"`` name) and a start time and/or priced selections.
  Name-split teams additionally need a start time *and* markets (avoids treating
  ``"Counter-Strike - ESL Pro League"`` competitions as matches).
* **selection**: a dict with a parseable decimal odds value (numbers or Polish strings such as
  ``"1,85"``) and a name.
* **market**: selections are grouped by the bookmaker's own per-line market id
  (``betslipMarketId``) when present, else by their market card. The market name is the name of
  the node whose id equals the betslip market id, else the name of the *outermost* named node
  holding a selection container (the card; named rows such as ``"Powyżej"`` inside it are
  ignored), else the nearest named ancestor. When a market sits inside a group named after a map
  (``"Mapa 1"``) and its own name has no map number, the group name is prefixed.

Duplicates of the same event found in several payloads are merged (markets and selections are
unioned; prices from later payloads win because they were captured later).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

from cs2bot.domain.odds import CollectedEvent, CollectedMarket, CollectedSelection
from cs2bot.odds.text import fold_text, parse_odds, tidy
from cs2bot.utils.time import parse_datetime

logger = logging.getLogger(__name__)

MAX_DEPTH = 80
DEFAULT_MAX_NODES = 5_000_000
_TEAM_SPLIT_RE = re.compile(r"\s+(?:[-\u2010-\u2015\u2212]|vs\.?|v\.?)\s+", re.IGNORECASE)
_MAP_REF_RE = re.compile(r"\bmap[a-z]*\s*(?:nr\.?\s*)?\d|\b\d\s*\.?\s*map[a-z]*\b")
_CATEGORY_RE = re.compile(r"^ca_[a-z0-9_]+$")
_ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js)(?:$|[?#])", re.IGNORECASE)
_DEFAULT_URL_ID_PATTERN = r"-m(\d+)(?=$|[/?#])"


@dataclass(frozen=True)
class PayloadKeys:
    """Key-name candidates, in priority order. Extend them when a new payload shape shows up."""

    event_ids: tuple[str, ...] = (
        "matchId",
        "eventId",
        "event_id",
        "match_id",
        "fixtureId",
        "gameId",
        "id",
    )
    event_names: tuple[str, ...] = ("name", "label", "title", "eventName", "matchName", "description")
    contestants: tuple[str, ...] = (
        "contestants",
        "competitors",
        "teams",
        "opponents",
        "participants",
        "actors",
        "competitor",
    )
    contestant_names: tuple[str, ...] = ("name", "label", "title", "teamName", "shortName", "displayName")
    home_away: tuple[tuple[str, str], ...] = (
        ("homeTeam", "awayTeam"),
        ("home", "away"),
        ("team1", "team2"),
        ("homeName", "awayName"),
        ("home_team", "away_team"),
        ("competitor1", "competitor2"),
        ("participant1", "participant2"),
        ("teamA", "teamB"),
    )
    start_times: tuple[str, ...] = (
        "matchDateUtc",
        "startDateUtc",
        "startDate",
        "startTime",
        "start_time",
        "startsAt",
        "starts_at",
        "start",
        "date",
        "dateUtc",
        "matchDate",
        "eventDate",
        "kickoff",
        "kickOff",
        "beginDate",
        "scheduledAt",
        "scheduled_at",
    )
    competitions: tuple[str, ...] = (
        "competition",
        "league",
        "tournament",
        "competitionName",
        "leagueName",
        "tournamentName",
        "competition_name",
    )
    sports: tuple[str, ...] = (
        "sport",
        "sportName",
        "sportCode",
        "sportSlug",
        "discipline",
        "videogame",
        "game",
    )
    live_flags: tuple[str, ...] = ("isLive", "live", "inPlay", "isInPlay", "is_live", "in_play")
    urls: tuple[str, ...] = (
        "url",
        "relativeDesktopUrl",
        "relativeUrl",
        "desktopUrl",
        "link",
        "href",
        "path",
        "seoUrl",
        "uri",
    )
    market_names: tuple[str, ...] = (
        "name",
        "label",
        "title",
        "marketName",
        "betName",
        "displayName",
        "market_name",
    )
    market_ids: tuple[str, ...] = ("id", "marketId", "market_id", "uid", "code")
    selection_containers: tuple[str, ...] = (
        "selections",
        "outcomes",
        "choices",
        "mainSelections",
        "selectionMatrix",
        "runners",
        "options",
        "odds",
    )
    selection_names: tuple[str, ...] = (
        "name",
        "label",
        "title",
        "betslipName",
        "outcomeName",
        "selectionName",
        "displayName",
        "shortName",
        "text",
    )
    selection_ids: tuple[str, ...] = (
        "id",
        "selectionId",
        "outcomeId",
        "selection_id",
        "outcome_id",
        "uid",
        "code",
    )
    selection_market_ids: tuple[str, ...] = ("betslipMarketId", "marketId", "market_id")
    odds: tuple[str, ...] = (
        "odds",
        "price",
        "odd",
        "oddValue",
        "oddsValue",
        "decimalOdds",
        "decimal",
        "coefficient",
        "coef",
        "rate",
    )
    available_flags: tuple[str, ...] = (
        "isAvailable",
        "available",
        "isActive",
        "active",
        "isOpen",
        "open",
        "isEnabled",
        "enabled",
        "isBettable",
        "bettable",
    )
    unavailable_flags: tuple[str, ...] = (
        "isSuspended",
        "suspended",
        "isLocked",
        "locked",
        "isClosed",
        "closed",
        "isDisabled",
        "disabled",
        "isBlocked",
    )
    status_keys: tuple[str, ...] = ("status", "state", "marketStatus", "selectionStatus")
    unavailable_statuses: tuple[str, ...] = (
        "suspended",
        "closed",
        "locked",
        "inactive",
        "settled",
        "cancelled",
        "canceled",
        "void",
        "unavailable",
        "disabled",
        "blocked",
        "removed",
        "deactivated",
    )
    flat_market_names: tuple[str, ...] = (
        "market",
        "marketName",
        "market_name",
        "betName",
        "betType",
        "bet_type",
    )
    flat_selection_names: tuple[str, ...] = (
        "selection",
        "selectionName",
        "selection_name",
        "outcome",
        "outcomeName",
        "outcome_name",
        "pick",
        "choice",
    )
    # Keys never searched for markets inside an event node.
    skip_keys: tuple[str, ...] = (
        "contestants",
        "competitors",
        "competition",
        "league",
        "tournament",
        "sport",
        "seo",
        "breadcrumbs",
        "translations",
        "requestHeaders",
    )
    event_url_id_pattern: str = _DEFAULT_URL_ID_PATTERN


DEFAULT_KEYS = PayloadKeys()


# --------------------------------------------------------------------------- parsed records


@dataclass
class ParsedSelection:
    external_id: str
    raw_name: str
    odds: Decimal
    is_available: bool = True

    def merge_key(self) -> str:
        return self.external_id or "name:" + fold_text(self.raw_name)


@dataclass
class ParsedMarket:
    external_id: str
    raw_name: str
    selections: dict[str, ParsedSelection] = field(default_factory=dict)

    def add(self, sel: ParsedSelection) -> None:
        self.selections[sel.merge_key()] = sel  # later observations win (fresher)


@dataclass
class ParsedEvent:
    """One event as found in one payload (before cross-payload merging)."""

    external_id: str
    name: str
    team1_raw: str
    team2_raw: str
    competition_raw: str | None = None
    starts_at: datetime | None = None
    url: str | None = None
    is_live: bool = False
    markets: dict[tuple[str, str], ParsedMarket] = field(default_factory=dict)
    sport_hint: str = ""
    category_ids: list[str] = field(default_factory=list)

    def market(self, raw_name: str, external_id: str) -> ParsedMarket:
        key = (raw_name, external_id)
        if key not in self.markets:
            self.markets[key] = ParsedMarket(external_id=external_id, raw_name=raw_name)
        return self.markets[key]

    def merge(self, other: ParsedEvent) -> None:
        """Fold a later observation of the same event into this one."""
        if not self.name and other.name:
            self.name = other.name
        if not self.competition_raw and other.competition_raw:
            self.competition_raw = other.competition_raw
        if self.starts_at is None and other.starts_at is not None:
            self.starts_at = other.starts_at
        if (not self.url or "-m" not in self.url) and other.url:
            self.url = other.url
        self.is_live = self.is_live or other.is_live
        if other.sport_hint and other.sport_hint not in self.sport_hint:
            self.sport_hint = f"{self.sport_hint} | {other.sport_hint}".strip(" |")
        for cat in other.category_ids:
            if cat not in self.category_ids:
                self.category_ids.append(cat)
        for key, mkt in other.markets.items():
            target = self.market(*key)
            for sel in mkt.selections.values():
                target.add(sel)

    def to_collected(self) -> CollectedEvent:
        markets: list[CollectedMarket] = []
        seen_sets: dict[frozenset[str], CollectedMarket] = {}
        for mkt in self.markets.values():
            sels = [
                CollectedSelection(
                    external_id=s.external_id, raw_name=s.raw_name, odds=s.odds, is_available=s.is_available
                )
                for s in mkt.selections.values()
            ]
            if not sels:
                continue
            ids = frozenset(s.external_id for s in sels if s.external_id)
            if ids and len(ids) == len(sels) and ids in seen_sets:
                # same bookmaker market shown twice (e.g. "Top" tab and its own category)
                existing = seen_sets[ids]
                by_id = {s.external_id: s for s in sels}
                existing.selections = [by_id.get(s.external_id, s) for s in existing.selections]
                continue
            cm = CollectedMarket(external_id=mkt.external_id, raw_name=mkt.raw_name, selections=sels)
            if ids and len(ids) == len(sels):
                seen_sets[ids] = cm
            markets.append(cm)
        return CollectedEvent(
            external_id=self.external_id,
            name=self.name or f"{self.team1_raw} - {self.team2_raw}",
            team1_raw=self.team1_raw,
            team2_raw=self.team2_raw,
            competition_raw=self.competition_raw,
            starts_at=self.starts_at,
            url=self.url,
            is_live=self.is_live,
            markets=markets,
        )


# --------------------------------------------------------------------------- small helpers


class _BudgetExceeded(Exception):
    pass


def _scalar_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return None


def _looks_like_json(text: str) -> bool:
    if len(text) < 20 or len(text) > 30_000_000:
        return False
    head = text.lstrip()[:1]
    return head in ("{", "[")


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, Mapping):
        for key in ("utc", "date", "value", "dateTime", "datetime", "iso"):
            if key in value:
                return _parse_dt(value[key])
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float) and abs(value) < 1e8:
        return None  # not an epoch timestamp
    if isinstance(value, str) and not re.search(r"\d{4}|\d{1,2}[./]\d{1,2}[./]\d{2,4}", value):
        return None
    try:
        dt = parse_datetime(value) if isinstance(value, str | int | float | datetime) else None
    except (ValueError, OverflowError, TypeError, OSError):
        return None
    if dt is None or not (2000 <= dt.year <= 2100):
        return None
    return dt.astimezone(UTC)


def event_id_from_url(url: str | None, pattern: str = _DEFAULT_URL_ID_PATTERN) -> str | None:
    """Betclic event pages end in ``...-m<id>`` (``/e-sport-s102/iem-c1/navi-faze-m123``)."""
    if not url:
        return None
    path = url.split("?", 1)[0].split("#", 1)[0]
    m = re.search(pattern, path)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- the walker


@dataclass
class _SelectionHit:
    node: Mapping[str, Any]
    odds: Decimal
    market_name: str | None
    any_name: str | None
    market_node_id: str | None
    unavailable: bool
    map_prefix: str | None


class _Walker:
    def __init__(self, keys: PayloadKeys, source_url: str | None, max_nodes: int) -> None:
        self.keys = keys
        self.source_url = source_url
        self.max_nodes = max_nodes
        self.nodes = 0
        self.events: list[ParsedEvent] = []
        self.flat_events: dict[str, ParsedEvent] = {}
        # market nodes carrying a match id but found outside their match node
        self.orphans: list[tuple[str, Mapping[str, Any]]] = []
        self._specific_event_ids = tuple(k for k in keys.event_ids if k != "id")
        self._skip = set(keys.skip_keys)
        self._containers = set(keys.selection_containers)
        self._unavailable_statuses = {s.lower() for s in keys.unavailable_statuses}

    # ---------------------------------------------------------- generic accessors
    def _tick(self) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise _BudgetExceeded

    @staticmethod
    def _first_str(node: Mapping[str, Any], keys: Sequence[str]) -> str | None:
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return tidy(value)
        return None

    @staticmethod
    def _first_id(node: Mapping[str, Any], keys: Sequence[str]) -> str | None:
        for key in keys:
            if key in node:
                found = _scalar_id(node[key])
                if found is not None:
                    return found
        return None

    def _named(self, value: Any) -> str | None:
        if isinstance(value, str):
            return tidy(value) or None
        if isinstance(value, Mapping):
            name = self._first_str(value, self.keys.contestant_names)
            if name:
                return name
            for key in ("team", "contestant", "competitor", "participant"):
                inner = value.get(key)
                if isinstance(inner, Mapping):
                    return self._first_str(inner, self.keys.contestant_names)
        return None

    def _odds(self, node: Mapping[str, Any]) -> Decimal | None:
        for key in self.keys.odds:
            if key not in node:
                continue
            value = node[key]
            if isinstance(value, Mapping):
                for inner in ("decimal", "value", "odds", "price"):
                    if inner in value:
                        parsed = parse_odds(value[inner])
                        if parsed is not None:
                            return parsed
                continue
            parsed = parse_odds(value)
            if parsed is not None:
                return parsed
        return None

    def _unavailable(self, node: Mapping[str, Any]) -> bool:
        for key in self.keys.available_flags:
            if key in node and _flag(node[key]) is False:
                return True
        for key in self.keys.unavailable_flags:
            if key in node and _flag(node[key]) is True:
                return True
        for key in self.keys.status_keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip().lower() in self._unavailable_statuses:
                return True
        return False

    def _start(self, node: Mapping[str, Any]) -> datetime | None:
        for key in self.keys.start_times:
            if key in node:
                dt = _parse_dt(node[key])
                if dt is not None:
                    return dt
        return None

    def _teams(self, node: Mapping[str, Any]) -> tuple[tuple[str, str] | None, bool]:
        """((team1, team2) | None, came_from_name_split)."""
        for key in self.keys.contestants:
            value = node.get(key)
            if isinstance(value, list) and value:
                names = [n for n in (self._named(c) for c in value) if n]
                if len(names) == 2:
                    return (names[0], names[1]), False
                if len(names) > 2:
                    return None, False  # outright / multi-participant market
        for home_key, away_key in self.keys.home_away:
            if home_key in node and away_key in node:
                home, away = self._named(node[home_key]), self._named(node[away_key])
                if home and away:
                    return (home, away), False
        name = self._first_str(node, self.keys.event_names)
        if name:
            parts = [p.strip() for p in _TEAM_SPLIT_RE.split(name)]
            if len(parts) == 2 and all(0 < len(p) <= 80 for p in parts):
                return (parts[0], parts[1]), True
        return None, False

    def _is_nested_event(self, node: Mapping[str, Any]) -> bool:
        """Another event inside an event (recommendations...). Explicit contestants required: a
        market node named "Mapa 1 - liczba rund" that happens to carry a date is NOT an event."""
        if self._first_id(node, self.keys.event_ids) is None:
            return False
        teams, from_name = self._teams(node)
        return teams is not None and not from_name and self._start(node) is not None

    def _text_of(self, value: Any) -> str | None:
        if isinstance(value, str):
            return tidy(value) or None
        if isinstance(value, Mapping):
            return self._first_str(value, ("name", "label", "title", "shortName", "code", "slug"))
        return None

    def _url(self, node: Mapping[str, Any], event_id: str) -> str | None:
        for key in self.keys.urls:
            value = node.get(key)
            if isinstance(value, str) and value.startswith(("/", "http")) and not _ASSET_RE.search(value):
                return urljoin(self.source_url, value) if self.source_url else value
        if self.source_url and event_id_from_url(self.source_url, self.keys.event_url_id_pattern) == event_id:
            return self.source_url
        return None

    # ---------------------------------------------------------- walking
    def walk(self, node: Any, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            return
        self._tick()
        if isinstance(node, Mapping):
            if self._try_flat_row(node):
                return
            event = self._try_event(node)
            if event is not None:
                self.events.append(event)
                return
            orphan_id = self._first_id(node, self._specific_event_ids)
            if (
                orphan_id is not None
                and self._first_str(node, self.keys.market_names)
                and any(k in node for k in self._containers)
            ):
                self.orphans.append((orphan_id, node))
                return
            for value in node.values():
                self.walk(value, depth + 1)
        elif isinstance(node, list | tuple):
            for item in node:
                self.walk(item, depth + 1)
        elif isinstance(node, str) and _looks_like_json(node):
            parsed = _loads(node)
            if parsed is not None:
                self.walk(parsed, depth + 1)

    def _try_event(self, node: Mapping[str, Any]) -> ParsedEvent | None:
        event_id = self._first_id(node, self.keys.event_ids)
        if event_id is None:
            return None
        teams, from_name = self._teams(node)
        if teams is None:
            return None
        start = self._start(node)
        if from_name and start is None:
            return None
        try:
            hits, nested, names_by_id, categories = self._collect(node)
        except _BudgetExceeded:
            raise
        except Exception:  # defensive: a weird node must not abort the payload
            logger.debug("failed to collect markets of event %s", event_id, exc_info=True)
            return None
        event = ParsedEvent(
            external_id=event_id,
            name=self._first_str(node, self.keys.event_names) or f"{teams[0]} - {teams[1]}",
            team1_raw=teams[0],
            team2_raw=teams[1],
            starts_at=start,
            category_ids=categories,
        )
        self._group(event, hits, names_by_id)
        if not event.markets and (start is None or from_name):
            return None
        comp_value = next((node[k] for k in self.keys.competitions if k in node), None)
        event.competition_raw = self._text_of(comp_value)
        live = next((_flag(node[k]) for k in self.keys.live_flags if k in node), None)
        event.is_live = bool(live)
        event.url = self._url(node, event_id)
        hints = [self._text_of(node[k]) for k in self.keys.sports if k in node]
        hints += [event.competition_raw, event.url, event.name]
        event.sport_hint = " | ".join(fold_text(h) for h in hints if h)
        for inner in nested:
            self.walk(inner, 1)
        return event

    def _collect(
        self, root: Mapping[str, Any]
    ) -> tuple[list[_SelectionHit], list[Mapping[str, Any]], dict[str, str], list[str]]:
        hits: list[_SelectionHit] = []
        nested: list[Mapping[str, Any]] = []
        names_by_id: dict[str, str] = {}
        categories: list[str] = []

        def visit(
            node: Any,
            market_name: str | None,
            any_name: str | None,
            market_id: str | None,
            unavailable: bool,
            map_prefix: str | None,
            depth: int,
        ) -> None:
            if depth > MAX_DEPTH:
                return
            self._tick()
            if isinstance(node, list | tuple):
                for item in node:
                    visit(item, market_name, any_name, market_id, unavailable, map_prefix, depth + 1)
                return
            if isinstance(node, str):
                if _CATEGORY_RE.match(node) and node not in categories:
                    categories.append(node)
                return
            if not isinstance(node, Mapping):
                return
            is_root = node is root
            if not is_root and self._is_nested_event(node):
                nested.append(node)
                return
            odds = self._odds(node)
            if odds is not None and not is_root:
                hits.append(
                    _SelectionHit(
                        node=node,
                        odds=odds,
                        market_name=market_name,
                        any_name=any_name,
                        market_node_id=market_id,
                        unavailable=unavailable or self._unavailable(node),
                        map_prefix=map_prefix,
                    )
                )
                return
            child_market, child_any, child_id, child_prefix = market_name, any_name, market_id, map_prefix
            child_unavailable = unavailable
            if not is_root:
                name = self._first_str(node, self.keys.market_names)
                node_id = self._first_id(node, self.keys.market_ids)
                if name and node_id is not None:
                    names_by_id.setdefault(node_id, name)
                if name:
                    child_any = name
                    if any(k in node for k in self._containers):
                        # The OUTERMOST node holding a selection container is the market card;
                        # named rows/columns inside it ("Powyżej", "Mapa 1" headers) are not.
                        if market_name is None:
                            child_market, child_id = name, node_id
                    elif _MAP_REF_RE.search(fold_text(name)):
                        # a group such as "Mapa 1": prefixed to map-less market names below it
                        child_prefix = name
                child_unavailable = unavailable or self._unavailable(node)
            for key, value in node.items():
                if key in self._skip:
                    continue
                visit(value, child_market, child_any, child_id, child_unavailable, child_prefix, depth + 1)

        visit(root, None, None, None, False, None, 0)
        return hits, nested, names_by_id, categories

    def _group(self, event: ParsedEvent, hits: list[_SelectionHit], names_by_id: dict[str, str]) -> None:
        for hit in hits:
            sel_name = self._first_str(hit.node, self.keys.selection_names)
            if not sel_name:
                actor = next((hit.node[k] for k in ("actor", "contestant", "team") if k in hit.node), None)
                sel_name = self._named(actor) if actor is not None else None
            if not sel_name:
                continue
            betslip_id = self._first_id(hit.node, self.keys.selection_market_ids)
            raw_name = (
                (names_by_id.get(betslip_id) if betslip_id else None) or hit.market_name or hit.any_name
            )
            if not raw_name or raw_name == sel_name:
                raw_name = hit.market_name or hit.any_name
            if not raw_name:
                continue
            if hit.map_prefix and hit.map_prefix != raw_name and not _MAP_REF_RE.search(fold_text(raw_name)):
                raw_name = f"{hit.map_prefix} - {raw_name}"
            external_id = betslip_id or hit.market_node_id or ""
            selection = ParsedSelection(
                external_id=self._first_id(hit.node, self.keys.selection_ids) or "",
                raw_name=sel_name,
                odds=hit.odds,
                is_available=not hit.unavailable,
            )
            event.market(raw_name, external_id).add(selection)

    def attach_orphans(self) -> None:
        """Attach market nodes that carry a match id but sit outside their match node."""
        by_id = {ev.external_id: ev for ev in self.events}
        for orphan_id, node in self.orphans:
            event = by_id.get(orphan_id)
            if event is None:
                continue
            try:
                hits, _, names_by_id, categories = self._collect({"orphan": node})
            except _BudgetExceeded:
                raise
            except Exception:
                logger.debug("failed to attach orphan market of event %s", orphan_id, exc_info=True)
                continue
            self._group(event, hits, names_by_id)
            for cat in categories:
                if cat not in event.category_ids:
                    event.category_ids.append(cat)

    def _try_flat_row(self, node: Mapping[str, Any]) -> bool:
        market = self._first_str(node, self.keys.flat_market_names)
        selection = self._first_str(node, self.keys.flat_selection_names)
        if not market or not selection:
            return False
        odds = self._odds(node)
        event_id = self._first_id(node, self.keys.event_ids)
        if odds is None or event_id is None:
            return False
        teams, _ = self._teams(node)
        if teams is None:
            return False
        event = self.flat_events.get(event_id)
        if event is None:
            event = ParsedEvent(
                external_id=event_id,
                name=self._first_str(node, self.keys.event_names) or f"{teams[0]} - {teams[1]}",
                team1_raw=teams[0],
                team2_raw=teams[1],
                starts_at=self._start(node),
            )
            comp_value = next((node[k] for k in self.keys.competitions if k in node), None)
            event.competition_raw = self._text_of(comp_value)
            event.is_live = bool(next((_flag(node[k]) for k in self.keys.live_flags if k in node), None))
            event.url = self._url(node, event_id)
            hints = [self._text_of(node[k]) for k in self.keys.sports if k in node]
            hints += [event.competition_raw, event.url, event.name]
            event.sport_hint = " | ".join(fold_text(h) for h in hints if h)
            self.flat_events[event_id] = event
        market_id = self._first_id(node, ("marketId", "market_id", "betslipMarketId")) or ""
        event.market(market, market_id).add(
            ParsedSelection(
                external_id=self._first_id(node, ("selectionId", "selection_id", "outcomeId", "outcome_id"))
                or "",
                raw_name=selection,
                odds=odds,
                is_available=not self._unavailable(node),
            )
        )
        return True


# --------------------------------------------------------------------------- public API


def extract_event_records(
    payloads: Iterable[Any],
    *,
    source_url: str | None = None,
    keys: PayloadKeys | None = None,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> list[ParsedEvent]:
    """Recognise events in each payload; same-id events inside this call are merged.

    Payloads are parsed JSON values (dict/list), JSON text, or ``bytes`` holding JSON.
    """
    walker_keys = keys or DEFAULT_KEYS
    merged: dict[str, ParsedEvent] = {}
    order: list[str] = []
    for payload in payloads:
        data: Any = payload
        if isinstance(payload, bytes | bytearray):
            data = _loads(bytes(payload).decode("utf-8", "replace"))
        elif isinstance(payload, str):
            data = _loads(payload)
        if data is None:
            continue
        walker = _Walker(walker_keys, source_url, max_nodes)
        try:
            walker.walk(data)
            walker.attach_orphans()
        except _BudgetExceeded:
            logger.warning("payload too large to walk completely (>%d nodes); partial result kept", max_nodes)
        except RecursionError:
            logger.warning("payload nesting too deep; partial result kept")
        for event in [*walker.events, *walker.flat_events.values()]:
            if event.external_id in merged:
                merged[event.external_id].merge(event)
            else:
                merged[event.external_id] = event
                order.append(event.external_id)
    return [merged[eid] for eid in order]


def merge_parsed_events(records: Iterable[ParsedEvent]) -> list[ParsedEvent]:
    """Merge records of the same event (in capture order: later prices win)."""
    merged: dict[str, ParsedEvent] = {}
    order: list[str] = []
    for rec in records:
        if rec.external_id in merged:
            merged[rec.external_id].merge(rec)
        else:
            copy = ParsedEvent(
                external_id=rec.external_id,
                name=rec.name,
                team1_raw=rec.team1_raw,
                team2_raw=rec.team2_raw,
                competition_raw=rec.competition_raw,
                starts_at=rec.starts_at,
                url=rec.url,
                is_live=rec.is_live,
                sport_hint=rec.sport_hint,
                category_ids=list(rec.category_ids),
            )
            copy.merge(rec)
            merged[rec.external_id] = copy
            order.append(rec.external_id)
    return [merged[eid] for eid in order]


def sport_matches(record: ParsedEvent, pattern: str | re.Pattern[str]) -> bool | None:
    """True/False if the record's sport/competition/url/name hints match ``pattern``; None if no hints."""
    if not record.sport_hint:
        return None
    regex = re.compile(pattern) if isinstance(pattern, str) else pattern
    return bool(regex.search(record.sport_hint))


def extract_events(
    payloads: Iterable[Any],
    *,
    source_url: str | None = None,
    keys: PayloadKeys | None = None,
    sport_pattern: str | re.Pattern[str] | None = None,
    keep_unknown_sport: bool = True,
) -> list[CollectedEvent]:
    """Find every event in ``payloads`` and return them merged as :class:`CollectedEvent`.

    ``sport_pattern`` (optional) keeps only events whose sport/competition/url/name hints match
    it (events without any hint are kept when ``keep_unknown_sport``).
    """
    records = extract_event_records(payloads, source_url=source_url, keys=keys)
    out: list[CollectedEvent] = []
    for rec in records:
        if sport_pattern is not None:
            verdict = sport_matches(rec, sport_pattern)
            if verdict is False or (verdict is None and not keep_unknown_sport):
                continue
        out.append(rec.to_collected())
    return out


# --------------------------------------------------------------------------- HTML embedded state

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_TYPE_JSON_RE = re.compile(r"""\btype\s*=\s*["']?application/(?:ld\+)?json""", re.IGNORECASE)
_STATE_ID_RE = re.compile(
    r"""\bid\s*=\s*["']?(?:ng-state|serverApp-state|[\w-]*-state|__NEXT_DATA__)""", re.IGNORECASE
)
_GLOBAL_STATE_RE = re.compile(r"(?:window\.)?__[A-Z0-9_]+__\s*=\s*")
_ANGULAR_ESCAPES = (("&q;", '"'), ("&s;", "'"), ("&l;", "<"), ("&g;", ">"), ("&a;", "&"))


def _decode_state_text(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    parsed = _loads(text)
    if parsed is None and "&q;" in text:  # Angular < 16 TransferState escaping
        for src, dst in _ANGULAR_ESCAPES:
            text = text.replace(src, dst)
        parsed = _loads(text)
    return parsed


def extract_json_documents(html: str) -> list[Any]:
    """JSON documents embedded in an HTML page.

    Covers ``<script type="application/json">`` (Angular ``ng-state`` / legacy
    ``serverApp-state`` incl. the old ``&q;`` escaping, ``__NEXT_DATA__``), JSON-LD and
    ``window.__SOME_STATE__ = {...}`` assignments.
    """
    docs: list[Any] = []
    decoder = json.JSONDecoder()
    for attrs, body in _SCRIPT_RE.findall(html):
        if _TYPE_JSON_RE.search(attrs) or _STATE_ID_RE.search(attrs):
            parsed = _decode_state_text(body)
            if parsed is not None:
                docs.append(parsed)
            continue
        for match in _GLOBAL_STATE_RE.finditer(body):
            start = match.end()
            while start < len(body) and body[start].isspace():
                start += 1
            if start < len(body) and body[start] in "{[":
                try:
                    value, _ = decoder.raw_decode(body, start)
                except ValueError:
                    continue
                docs.append(value)
    return docs


def find_match_categories(documents: Iterable[Any], match_id: str) -> list[str]:
    """Market category ids (``ca_*``) of one match from ng-state / decoded gRPC JSON documents.

    Looks for ``{"matchId": <id>, "categories": [{"id": "ca_..."}]}`` (Angular state) and for
    ``{"id": <id>, "categoryIds": [...]}`` (output of the gRPC decoder).
    """
    target = str(match_id)
    found: list[str] = []

    def visit(node: Any, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        if isinstance(node, list | tuple):
            for item in node:
                visit(item, depth + 1)
            return
        if not isinstance(node, Mapping):
            return
        node_id = _scalar_id(node.get("matchId", node.get("id")))
        if node_id == target:
            cats = node.get("categories")
            if isinstance(cats, list):
                for cat in cats:
                    cid = cat.get("id") if isinstance(cat, Mapping) else cat
                    if isinstance(cid, str) and cid not in found:
                        found.append(cid)
            for cid in node.get("categoryIds") or []:
                if isinstance(cid, str) and cid not in found:
                    found.append(cid)
        for value in node.values():
            visit(value, depth + 1)

    for doc in documents:
        visit(doc, 0)
    return found


def find_request_headers(documents: Iterable[Any]) -> dict[str, str]:
    """String headers of the gRPC requests the page itself made (``requestHeaders`` in ng-state)."""

    def visit(node: Any, depth: int) -> dict[str, str] | None:
        if depth > 8:
            return None
        if isinstance(node, list | tuple):
            for item in node:
                got = visit(item, depth + 1)
                if got:
                    return got
            return None
        if not isinstance(node, Mapping):
            return None
        headers = node.get("requestHeaders")
        if isinstance(headers, Mapping):
            clean = {str(k): v for k, v in headers.items() if isinstance(v, str)}
            if clean:
                return clean
        for value in node.values():
            got = visit(value, depth + 1)
            if got:
                return got
        return None

    for doc in documents:
        got = visit(doc, 0)
        if got:
            return got
    return {}
