"""Optional sharp-bookmaker benchmark (closing-line reference for CLV, market feature).

No paid odds feed is configured, so the default source is :class:`DisabledSharpOddsSource`, which
never answers. A real feed is plugged in by implementing :class:`SharpOddsSource` (one method). Two
adapters ship:

* :class:`DisabledSharpOddsSource` - the default; ``enabled`` is False and every lookup returns None;
* :class:`JsonFileSharpOddsSource` - reads de-vigged prices from a local JSON file the owner (or a
  separate tool) maintains, e.g. exported from an odds-comparison site::

      {"quotes": [{"match_id": 123, "captured_at": "2026-11-24T17:55:00Z",
                   "team1_odds": 1.42, "team2_odds": 2.95, "source": "pinnacle"}]}

  ``team1`` / ``team2`` must already be in our orientation (``matches.team1_id`` / ``team2_id``).

:func:`closing_line_reference` is the CLV reference used for a selection: the sharp closing
probability when a sharp quote exists for the match-winner market, else the Betclic closing price
(:func:`cs2bot.odds.quotes.closing_odds`).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from cs2bot.odds.movement import devig_two_way
from cs2bot.utils.time import ensure_utc, parse_datetime

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from cs2bot.config import Settings

log = logging.getLogger(__name__)

__all__ = [
    "ClosingReference",
    "DisabledSharpOddsSource",
    "JsonFileSharpOddsSource",
    "SharpOddsSource",
    "SharpQuote",
    "closing_line_reference",
    "sharp_source_from_settings",
]


@dataclass(frozen=True, slots=True)
class SharpQuote:
    """A sharp two-way match-winner price, our orientation."""

    match_id: int
    captured_at: datetime
    team1_odds: float
    team2_odds: float
    source: str = "sharp"

    @property
    def p_team1(self) -> float:
        """De-vigged probability of team1 (proportional method)."""
        return devig_two_way(self.team1_odds, self.team2_odds)[0]


@runtime_checkable
class SharpOddsSource(Protocol):
    """A sharp-bookmaker price feed. ``quotes`` returns the match's quotes, oldest first."""

    @property
    def enabled(self) -> bool: ...

    def quotes(self, match_id: int, *, until: datetime | None = None) -> list[SharpQuote]: ...


class DisabledSharpOddsSource:
    """The default: no sharp feed configured (``enabled`` False, no quotes)."""

    @property
    def enabled(self) -> bool:
        return False

    def quotes(self, match_id: int, *, until: datetime | None = None) -> list[SharpQuote]:
        return []


class JsonFileSharpOddsSource:
    """Quotes from a local JSON file (see the module docstring). Malformed entries are skipped.

    The file is re-read when its modification time changes, so another tool can keep it updated.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._by_match: dict[int, list[SharpQuote]] = {}

    @property
    def enabled(self) -> bool:
        return self.path.is_file()

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._by_match, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("sharp odds file %s unreadable: %s", self.path, exc)
            self._by_match, self._mtime = {}, mtime
            return
        items = data.get("quotes", []) if isinstance(data, dict) else data
        by_match: dict[int, list[SharpQuote]] = {}
        for item in items if isinstance(items, list) else []:
            try:
                at = parse_datetime(item["captured_at"])
                o1, o2 = float(item["team1_odds"]), float(item["team2_odds"])
                if at is None or not (o1 > 1.0 and o2 > 1.0) or math.isnan(o1 + o2):
                    continue
                q = SharpQuote(
                    int(item["match_id"]), ensure_utc(at), o1, o2, str(item.get("source", "sharp"))
                )
            except (KeyError, TypeError, ValueError):
                continue
            by_match.setdefault(q.match_id, []).append(q)
        for qs in by_match.values():
            qs.sort(key=lambda q: q.captured_at)
        self._by_match, self._mtime = by_match, mtime

    def quotes(self, match_id: int, *, until: datetime | None = None) -> list[SharpQuote]:
        self._load()
        qs = self._by_match.get(match_id, [])
        if until is None:
            return list(qs)
        t = ensure_utc(until)
        return [q for q in qs if q.captured_at < t]


@dataclass(frozen=True)
class ClosingReference:
    """CLV reference of one selection: implied probability at the close and where it came from."""

    probability: float
    odds: Decimal | None  # Betclic closing odds (None when the reference is the sharp line)
    source: str  # "sharp" | "betclic"


def closing_line_reference(
    session: Session,
    selection_id: int,
    kickoff: datetime,
    *,
    sharp: SharpOddsSource | None = None,
    match_id: int | None = None,
    selection_key: str | None = None,
    spec_key: str | None = None,
) -> ClosingReference | None:
    """Closing-line reference for CLV.

    With an enabled ``sharp`` source, a match-winner selection (``spec_key == "match_winner"``,
    ``selection_key`` team1 / team2) uses the last sharp quote before ``kickoff`` (de-vigged). Every
    other case falls back to Betclic's own closing price (implied probability ``1 / odds``, not
    de-vigged - the same convention as the existing CLV). None when neither exists.
    """
    from cs2bot.odds.quotes import closing_odds

    if (
        sharp is not None
        and sharp.enabled
        and match_id is not None
        and spec_key == "match_winner"
        and selection_key in ("team1", "team2")
    ):
        qs: Sequence[SharpQuote] = sharp.quotes(match_id, until=kickoff)
        if qs:
            p1 = qs[-1].p_team1
            if not math.isnan(p1):
                return ClosingReference(p1 if selection_key == "team1" else 1.0 - p1, None, "sharp")
    odds = closing_odds(session, selection_id, kickoff)
    if odds is None or odds <= 1:
        return None
    return ClosingReference(float(1 / odds), odds, "betclic")


def sharp_source_from_settings(settings: Settings) -> SharpOddsSource:
    """``settings.models.sharp_odds_file`` -> :class:`JsonFileSharpOddsSource`, else the disabled source."""
    from cs2bot.utils.paths import resolve_path

    path = settings.models.sharp_odds_file
    if not path:
        return DisabledSharpOddsSource()
    return JsonFileSharpOddsSource(resolve_path(path))
