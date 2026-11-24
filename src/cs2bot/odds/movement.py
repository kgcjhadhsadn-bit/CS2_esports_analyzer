"""Betclic line movement: per-selection movement summaries and match-level market features.

Read-only on top of ``odds_snapshots`` (written by :mod:`cs2bot.odds.collector` only when a price or
its availability changes). Nothing here changes mapping or linking behaviour.

Definitions (all computed from snapshots captured strictly before ``as_of`` - no look-ahead):

* **opening price** - the first available snapshot of the selection;
* **current price** - the last available snapshot before ``as_of`` (snapshots are written on change,
  so it is the price on offer at ``as_of``);
* **move** - a snapshot whose price differs from the previous available price;
* **steam move** - a shift of the implied probability of at least ``steam_min_prob`` within
  ``steam_window`` (one or several consecutive moves in the same direction). Bookmakers move this
  much this fast when sharp money or team news arrives; the sign says which way;
* **time since last move** - hours between the last move and ``as_of``.

Match-level features use the match-winner market (``spec_key == "match_winner"``, selections
``team1`` / ``team2`` in our orientation) and de-vig the two-way price proportionally:
``p1 = (1/o1) / (1/o1 + 1/o2)``.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from cs2bot.utils.time import ensure_utc

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from cs2bot.datasets.deep import MarketPoint

NAN: Final[float] = math.nan
MATCH_WINNER_SPEC_KEY: Final[str] = "match_winner"

__all__ = [
    "MARKET_FEATURE_NAMES",
    "MATCH_WINNER_SPEC_KEY",
    "MovementConfig",
    "PricePoint",
    "SelectionMovement",
    "devig_two_way",
    "load_match_winner_history",
    "load_selection_history",
    "market_features",
    "selection_movement",
]


@dataclass(frozen=True)
class MovementConfig:
    steam_min_prob: float = 0.04  # implied-probability shift that counts as a steam move
    steam_window: timedelta = timedelta(minutes=30)
    min_move_prob: float = 0.002  # smaller changes are rounding noise


DEFAULT_MOVEMENT = MovementConfig()


@dataclass(frozen=True, slots=True)
class PricePoint:
    captured_at: datetime
    odds: float
    is_available: bool = True


@dataclass(frozen=True)
class SelectionMovement:
    """Movement summary of one selection as of a time (NaN / 0 when never quoted)."""

    n_points: int
    opening_odds: float
    current_odds: float
    opening_at: datetime | None
    last_move_at: datetime | None
    n_moves: int
    steam_up: int  # steam moves that shortened the price (implied probability up)
    steam_down: int
    prob_change: float  # implied probability current - opening
    hours_since_last_move: float
    hours_quoted: float  # hours between the opening price and as_of
    min_odds: float
    max_odds: float

    @property
    def quoted(self) -> bool:
        return self.n_points > 0


def _implied(odds: float) -> float:
    return 1.0 / odds if odds > 1.0 else NAN


def _steam(moves: Sequence[tuple[float, float]], cfg: MovementConfig) -> tuple[int, int]:
    """Count steam moves in a chronological list of (timestamp, implied probability) points.

    A steam move is detected when, within ``steam_window`` of a point, the implied probability moved
    by ``steam_min_prob`` or more; once detected the scan restarts after that window (one burst = one
    steam move).
    """
    up = down = 0
    window = cfg.steam_window.total_seconds()
    i = 0
    n = len(moves)
    while i < n:
        t0, p0 = moves[i]
        j = i + 1
        hit = 0
        while j < n and moves[j][0] - t0 <= window:
            delta = moves[j][1] - p0
            if abs(delta) >= cfg.steam_min_prob:
                hit = 1 if delta > 0 else -1
                break
            j += 1
        if hit:
            up += hit > 0
            down += hit < 0
            i = j + 1
        else:
            i += 1
    return up, down


def selection_movement(
    history: Sequence[PricePoint], as_of: datetime, *, config: MovementConfig = DEFAULT_MOVEMENT
) -> SelectionMovement:
    """Summary of the available snapshots captured strictly before ``as_of`` (any input order)."""
    t = ensure_utc(as_of)
    pts = sorted(
        (p for p in history if p.is_available and p.odds > 1.0 and ensure_utc(p.captured_at) < t),
        key=lambda p: ensure_utc(p.captured_at),
    )
    if not pts:
        return SelectionMovement(0, NAN, NAN, None, None, 0, 0, 0, NAN, NAN, NAN, NAN, NAN)
    probs = [(ensure_utc(p.captured_at).timestamp(), _implied(p.odds)) for p in pts]
    n_moves = 0
    last_move: datetime | None = None
    for prev, cur, p in zip(probs, probs[1:], pts[1:], strict=False):
        if abs(cur[1] - prev[1]) >= config.min_move_prob:
            n_moves += 1
            last_move = ensure_utc(p.captured_at)
    up, down = _steam(probs, config)
    opening_at = ensure_utc(pts[0].captured_at)
    ref = last_move or opening_at
    return SelectionMovement(
        n_points=len(pts),
        opening_odds=pts[0].odds,
        current_odds=pts[-1].odds,
        opening_at=opening_at,
        last_move_at=last_move,
        n_moves=n_moves,
        steam_up=up,
        steam_down=down,
        prob_change=probs[-1][1] - probs[0][1],
        hours_since_last_move=(t - ref).total_seconds() / 3600.0,
        hours_quoted=(t - opening_at).total_seconds() / 3600.0,
        min_odds=min(p.odds for p in pts),
        max_odds=max(p.odds for p in pts),
    )


def devig_two_way(odds1: float | None, odds2: float | None) -> tuple[float, float]:
    """(de-vigged P(selection 1), overround); NaN when either price is missing / invalid."""
    if odds1 is None or odds2 is None or not (odds1 > 1.0 and odds2 > 1.0):
        return NAN, NAN
    i1, i2 = 1.0 / odds1, 1.0 / odds2
    return i1 / (i1 + i2), i1 + i2 - 1.0


#: Match-level market features (team1 perspective), see :func:`market_features`.
MARKET_FEATURE_NAMES: Final[tuple[str, ...]] = (
    "mkt_p_open",
    "mkt_p_last",
    "mkt_logit_last",
    "mkt_logit_move",
    "mkt_n_moves",
    "mkt_steam",
    "mkt_hours_since_move",
    "mkt_hours_quoted",
    "mkt_overround",
    "sharp_p_last",
    "sharp_minus_mkt",
)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def market_features(
    points: Sequence[MarketPoint],
    as_of: datetime,
    *,
    flip: bool = False,
    sharp: Sequence[tuple[datetime, float]] = (),
    config: MovementConfig = DEFAULT_MOVEMENT,
) -> dict[str, float]:
    """Match-winner market features as of ``as_of`` (points captured strictly before it).

    ``points`` are in the match's orientation; ``flip=True`` returns them for the other team (a
    mirrored training row / a caller whose team1 is the match's team2). Every value is NaN when the
    market was never quoted before ``as_of`` (``mkt_n_moves`` / ``mkt_steam`` then 0).
    """
    t = ensure_utc(as_of)
    out = dict.fromkeys(MARKET_FEATURE_NAMES, NAN)
    out["mkt_n_moves"] = 0.0
    out["mkt_steam"] = 0.0
    usable: list[tuple[float, float, float]] = []  # (ts, p_team1, overround)
    for pt in sorted(points, key=lambda p: ensure_utc(p.captured_at)):
        ts = ensure_utc(pt.captured_at)
        if ts >= t:
            break
        p1, over = devig_two_way(pt.team1_odds, pt.team2_odds)
        if math.isnan(p1):
            continue
        usable.append((ts.timestamp(), 1.0 - p1 if flip else p1, over))
    if usable:
        out["mkt_p_open"] = usable[0][1]
        out["mkt_p_last"] = usable[-1][1]
        out["mkt_logit_last"] = _logit(usable[-1][1])
        out["mkt_logit_move"] = _logit(usable[-1][1]) - _logit(usable[0][1])
        out["mkt_overround"] = usable[-1][2]
        moves = [b for a, b in itertools.pairwise(usable) if abs(b[1] - a[1]) >= config.min_move_prob]
        out["mkt_n_moves"] = float(len(moves))
        up, down = _steam([(u[0], u[1]) for u in usable], config)
        out["mkt_steam"] = float(up - down)
        last = moves[-1][0] if moves else usable[0][0]
        out["mkt_hours_since_move"] = (t.timestamp() - last) / 3600.0
        out["mkt_hours_quoted"] = (t.timestamp() - usable[0][0]) / 3600.0
    sharp_pts = sorted((ensure_utc(ts), p) for ts, p in sharp if ensure_utc(ts) < t and 0.0 < p < 1.0)
    if sharp_pts:
        sp = sharp_pts[-1][1]
        out["sharp_p_last"] = 1.0 - sp if flip else sp
        if not math.isnan(out["mkt_p_last"]):
            out["sharp_minus_mkt"] = out["sharp_p_last"] - out["mkt_p_last"]
    return out


# ------------------------------------------------------------------------------------- DB loaders


def load_selection_history(
    session: Session, selection_ids: Sequence[int], *, until: datetime | None = None
) -> dict[int, list[PricePoint]]:
    """selection id -> chronological price points (``captured_at < until``)."""
    from sqlalchemy import select

    from cs2bot.db.models import OddsSnapshot

    out: dict[int, list[PricePoint]] = defaultdict(list)
    ids = sorted(set(selection_ids))
    for i in range(0, len(ids), 500):
        stmt = select(
            OddsSnapshot.selection_id, OddsSnapshot.captured_at, OddsSnapshot.odds, OddsSnapshot.is_available
        ).where(OddsSnapshot.selection_id.in_(ids[i : i + 500]))
        if until is not None:
            stmt = stmt.where(OddsSnapshot.captured_at < ensure_utc(until))
        for sid, at, odds, avail in session.execute(stmt.order_by(OddsSnapshot.captured_at, OddsSnapshot.id)):
            try:
                out[int(sid)].append(PricePoint(ensure_utc(at), float(Decimal(odds)), bool(avail)))
            except (TypeError, ValueError, ArithmeticError):
                continue
    return dict(out)


def load_match_winner_history(
    session: Session, *, until: datetime | None = None
) -> dict[int, list[MarketPoint]]:
    """match id -> chronological two-way match-winner prices (our orientation) from linked events.

    Only events linked ``AUTO`` / ``CONFIRMED`` count (the same rule as the betting quotes). Each
    snapshot of either selection produces a point carrying the latest known price of both sides; a
    suspended selection (``is_available`` false) makes that side ``None`` until it is quoted again.
    Several linked events of one match are merged chronologically.
    """
    from sqlalchemy import select

    from cs2bot.datasets.deep import MarketPoint
    from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, OddsSnapshot
    from cs2bot.domain.enums import LinkStatus

    stmt = (
        select(
            BookmakerEvent.match_id,
            BookmakerEvent.id,
            BookmakerSelection.selection_key,
            OddsSnapshot.captured_at,
            OddsSnapshot.odds,
            OddsSnapshot.is_available,
        )
        .join(BookmakerSelection, OddsSnapshot.selection_id == BookmakerSelection.id)
        .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
        .join(BookmakerEvent, BookmakerMarket.event_id == BookmakerEvent.id)
        .where(
            BookmakerEvent.match_id.is_not(None),
            BookmakerEvent.link_status.in_([LinkStatus.AUTO, LinkStatus.CONFIRMED]),
            BookmakerMarket.spec_key == MATCH_WINNER_SPEC_KEY,
            BookmakerSelection.selection_key.in_(["team1", "team2"]),
        )
        .order_by(OddsSnapshot.captured_at, OddsSnapshot.id)
    )
    if until is not None:
        stmt = stmt.where(OddsSnapshot.captured_at < ensure_utc(until))
    state: dict[tuple[int, int], dict[str, float | None]] = defaultdict(
        lambda: {"team1": None, "team2": None}
    )
    out: dict[int, list[MarketPoint]] = defaultdict(list)
    for match_id, event_id, key, at, odds, avail in session.execute(stmt):
        if match_id is None:
            continue
        try:
            price = float(Decimal(odds)) if avail else None
        except (TypeError, ValueError, ArithmeticError):
            continue
        prices = state[(int(match_id), int(event_id))]
        prices[str(key)] = price
        out[int(match_id)].append(MarketPoint(ensure_utc(at), prices["team1"], prices["team2"]))
    return dict(out)


def history_index(points: Mapping[int, Sequence[MarketPoint]]) -> dict[int, list[float]]:
    """match id -> sorted capture timestamps (helper for fast "quoted before t" checks)."""
    return {mid: sorted(ensure_utc(p.captured_at).timestamp() for p in pts) for mid, pts in points.items()}


def quoted_before(index: Mapping[int, Sequence[float]], match_id: int, as_of: datetime) -> int:
    """Number of recorded points of ``match_id`` captured strictly before ``as_of``."""
    ts = index.get(match_id)
    return bisect.bisect_left(ts, ensure_utc(as_of).timestamp()) if ts else 0
