"""Pure analytics behind the bankroll / performance endpoints (no DB access, easy to test).

Definitions (also in docs/API.md):

* **turnover** - total stake of bets that won or lost (voids are refunded and excluded)
* **realized P&L** - sum of ``bets.pnl`` of settled bets (won / lost / void)
* **yield** - realized P&L / turnover
* **ROI** - realized P&L / capital base (deposits from the ledger, else the starting bankroll)
* **hit rate** - won / (won + lost)
* **max drawdown** - largest fall of cumulative realized P&L from its running peak (settlement order);
  ``max_drawdown_pct`` divides each fall by ``capital base + peak``
* **calibration** - equal-width probability bins; Brier score, log-loss and expected calibration
  error (ECE = sum over bins of ``n_bin / N * |mean predicted - observed|``)
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from cs2bot.api.formatting import round_or_none
from cs2bot.api.schemas import BrierPoint, CalibrationBin, CalibrationSet
from cs2bot.domain.enums import BetStatus
from cs2bot.domain.markets import MarketType
from cs2bot.utils.time import to_local

OPEN_STATUSES = frozenset({BetStatus.PENDING, BetStatus.PLACING, BetStatus.PLACED, BetStatus.DRY_RUN})
SETTLED_STATUSES = frozenset({BetStatus.WON, BetStatus.LOST, BetStatus.VOID})
EXCLUDED_STATUSES = frozenset({BetStatus.FAILED, BetStatus.CANCELLED})
# statuses whose stake actually left the balance (used to rebuild the bankroll at a past instant)
MONEY_OUT_STATUSES = frozenset({BetStatus.PLACED, BetStatus.WON, BetStatus.LOST, BetStatus.VOID})

ZERO = Decimal("0")


@dataclass(frozen=True)
class BetFact:
    """The fields of a bet the analytics need (decoupled from the ORM row)."""

    id: int
    match_id: int | None
    market_type: str
    status: BetStatus
    dry_run: bool
    stake: Decimal
    pnl: Decimal | None
    odds: Decimal | None
    clv: float | None
    ev: float | None
    p_win: float | None
    placed_at: datetime | None
    settled_at: datetime | None
    created_at: datetime | None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_settled(self) -> bool:
        return self.status in SETTLED_STATUSES

    @property
    def opened_at(self) -> datetime | None:
        return self.placed_at or self.created_at

    @property
    def realized_pnl(self) -> Decimal:
        """P&L of a settled bet (0 for open bets). Falls back to -stake for a lost bet without pnl."""
        if not self.is_settled:
            return ZERO
        if self.pnl is not None:
            return self.pnl
        if self.status is BetStatus.LOST:
            return -self.stake
        return ZERO


@dataclass(frozen=True)
class BetStats:
    bets: int
    open_bets: int
    settled_bets: int
    won: int
    lost: int
    void: int
    turnover: Decimal
    realized_pnl: Decimal
    open_stakes: Decimal
    hit_rate: float | None
    yield_rate: float | None
    avg_odds: Decimal | None
    avg_clv: float | None
    clv_bets: int
    avg_ev: float | None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bet_stats(facts: Iterable[BetFact]) -> BetStats:
    """Aggregate statistics; failed / cancelled bets (no money at risk) are ignored."""
    rows = [f for f in facts if f.status not in EXCLUDED_STATUSES]
    won = [f for f in rows if f.status is BetStatus.WON]
    lost = [f for f in rows if f.status is BetStatus.LOST]
    void = [f for f in rows if f.status is BetStatus.VOID]
    open_rows = [f for f in rows if f.is_open]
    decided = won + lost
    turnover = sum((f.stake for f in decided), ZERO)
    realized = sum((f.realized_pnl for f in rows), ZERO)
    odds = [f.odds for f in decided if f.odds is not None]
    clvs = [f.clv for f in rows if f.clv is not None and math.isfinite(f.clv)]
    evs = [f.ev for f in rows if f.ev is not None and math.isfinite(f.ev)]
    return BetStats(
        bets=len(rows),
        open_bets=len(open_rows),
        settled_bets=len(won) + len(lost) + len(void),
        won=len(won),
        lost=len(lost),
        void=len(void),
        turnover=turnover,
        realized_pnl=realized,
        open_stakes=sum((f.stake for f in open_rows), ZERO),
        hit_rate=len(won) / len(decided) if decided else None,
        yield_rate=float(realized / turnover) if turnover > 0 else None,
        avg_odds=(sum(odds, ZERO) / len(odds)) if odds else None,
        avg_clv=_mean(clvs),
        clv_bets=len(clvs),
        avg_ev=_mean(evs),
    )


def settled_in_order(facts: Iterable[BetFact]) -> list[BetFact]:
    """Settled bets ordered by settlement time (bets without a timestamp last, by id)."""
    rows = [f for f in facts if f.is_settled]
    far = datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    return sorted(rows, key=lambda f: (f.settled_at or far, f.id))


def max_drawdown(pnls: Iterable[Decimal], capital: Decimal) -> tuple[Decimal, float | None]:
    """Largest peak-to-trough fall of cumulative P&L, absolute and relative to ``capital + peak``."""
    cumulative = ZERO
    peak = ZERO
    worst = ZERO
    worst_pct: float | None = None
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        fall = peak - cumulative
        worst = max(worst, fall)
        base = capital + peak
        if base > 0:
            pct = float(fall / base)
            worst_pct = pct if worst_pct is None else max(worst_pct, pct)
    return worst, worst_pct


# --------------------------------------------------------------------------------------------
# P&L by period
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodRow:
    period: str
    start: date
    stats: BetStats


Granularity = str  # "day" | "week" | "month"


def _period_of(local_day: date, granularity: Granularity) -> tuple[date, str]:
    if granularity == "day":
        return local_day, local_day.isoformat()
    if granularity == "week":
        start = local_day - timedelta(days=local_day.weekday())
        iso_year, iso_week, _ = start.isocalendar()
        return start, f"{iso_year}-W{iso_week:02d}"
    if granularity == "month":
        start = local_day.replace(day=1)
        return start, f"{start.year}-{start.month:02d}"
    raise ValueError(f"unknown granularity {granularity!r}")


def pnl_by_period(facts: Iterable[BetFact], tz: str | ZoneInfo, granularity: Granularity) -> list[PeriodRow]:
    """Settled bets grouped by the local (Warsaw by default) date of settlement, oldest first."""
    groups: dict[tuple[date, str], list[BetFact]] = defaultdict(list)
    for fact in facts:
        if not fact.is_settled or fact.settled_at is None:
            continue
        local_day = to_local(fact.settled_at, tz).date()
        groups[_period_of(local_day, granularity)].append(fact)
    return [
        PeriodRow(period=label, start=start, stats=bet_stats(rows))
        for (start, label), rows in sorted(groups.items())
    ]


# --------------------------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval of a binomial proportion."""
    if n <= 0:
        return None
    phat = successes / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _clip(p: float, eps: float = 1e-6) -> float:
    return min(1.0 - eps, max(eps, p))


def calibration_set(
    samples: Sequence[tuple[float, int]],
    n_bins: int,
    *,
    by_month: Sequence[BrierPoint] = (),
) -> CalibrationSet:
    """Reliability bins and scores of (predicted probability, outcome 0/1) pairs."""
    clean = [(min(1.0, max(0.0, p)), 1 if y else 0) for p, y in samples if p is not None and math.isfinite(p)]
    width = 1.0 / n_bins
    bucket: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in clean:
        bucket[min(n_bins - 1, int(p / width))].append((p, y))
    bins: list[CalibrationBin] = []
    ece = 0.0
    n = len(clean)
    for i, rows in enumerate(bucket):
        lower, upper = round(i * width, 10), round((i + 1) * width, 10)
        if not rows:
            bins.append(CalibrationBin(lower=lower, upper=upper, count=0))
            continue
        mean_p = sum(p for p, _ in rows) / len(rows)
        hits = sum(y for _, y in rows)
        observed = hits / len(rows)
        ci = wilson_interval(hits, len(rows))
        ece += len(rows) / n * abs(mean_p - observed)
        bins.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(rows),
                mean_predicted=round(mean_p, 6),
                observed_rate=round(observed, 6),
                ci_low=round(ci[0], 6) if ci else None,
                ci_high=round(ci[1], 6) if ci else None,
            )
        )
    if not clean:
        return CalibrationSet(n=0, bins=bins, brier_by_month=list(by_month))
    brier = sum((p - y) ** 2 for p, y in clean) / n
    log_loss = -sum(y * math.log(_clip(p)) + (1 - y) * math.log(1 - _clip(p)) for p, y in clean) / n
    return CalibrationSet(
        n=n,
        base_rate=round_or_none(sum(y for _, y in clean) / n),
        mean_predicted=round_or_none(sum(p for p, _ in clean) / n),
        brier=round_or_none(brier),
        log_loss=round_or_none(log_loss),
        ece=round_or_none(ece),
        bins=bins,
        brier_by_month=list(by_month),
    )


def brier_by_month(
    samples: Iterable[tuple[float, int, datetime | None]], tz: str | ZoneInfo
) -> list[BrierPoint]:
    """Brier score per local calendar month (samples without a timestamp are skipped)."""
    groups: dict[str, list[float]] = defaultdict(list)
    for p, y, ts in samples:
        if ts is None or p is None or not math.isfinite(p):
            continue
        local = to_local(ts, tz)
        groups[f"{local.year}-{local.month:02d}"].append((p - (1 if y else 0)) ** 2)
    return [
        BrierPoint(period=k, n=len(v), brier=round(sum(v) / len(v), 6)) for k, v in sorted(groups.items())
    ]


# --------------------------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------------------------

_MARKET_ORDER = {mt.value: i for i, mt in enumerate(MarketType)}


def group_by_market(facts: Iterable[BetFact]) -> list[tuple[str, list[BetFact]]]:
    groups: dict[str, list[BetFact]] = defaultdict(list)
    for fact in facts:
        groups[fact.market_type].append(fact)
    return sorted(groups.items(), key=lambda kv: (_MARKET_ORDER.get(kv[0], len(_MARKET_ORDER)), kv[0]))


def downsample[P](points: Sequence[P], max_points: int) -> list[P]:
    """Evenly thin a series to at most ``max_points`` (first and last points are always kept)."""
    n = len(points)
    if max_points <= 0 or n <= max_points:
        return list(points)
    if max_points == 1:
        return [points[-1]]
    step = (n - 1) / (max_points - 1)
    indices = sorted({round(i * step) for i in range(max_points)} | {0, n - 1})
    return [points[i] for i in indices]
