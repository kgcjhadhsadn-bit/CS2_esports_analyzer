"""Backtest results: :class:`BacktestReport` (pydantic, JSON-serialisable) and its Markdown view.

Definitions used throughout:

* **ROI / yield** = profit / total staked (money-weighted, the bankroll-sized stakes);
  ``bankroll_growth`` = final / initial bankroll - 1.
* **Flat** metrics bet one unit on every outcome the decision step accepted (before staking caps),
  so they measure the edge independently of Kelly sizing and loss guards.
* **Hit rate** = won / (won + lost); pushes and voids are excluded.
* **CLV** = bet odds / closing odds - 1 (positive = the bet beat the closing line).
* **Max drawdown** = the largest fall of the bankroll from a previous peak, in PLN and as a
  fraction of that peak.
* Brier / log-loss use ``p(win | settled)`` against won (1) / lost (0) of every priced outcome
  (``*_all``) or of the bets placed (``*_bets``); void / push / unknown outcomes are left out.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "BacktestReport",
    "BetRecord",
    "BreakdownRow",
    "CurvePoint",
    "PredictionRecord",
    "breakdown",
    "max_drawdown",
]


class CurvePoint(BaseModel):
    time: datetime
    bankroll: Decimal
    profit: Decimal


class BetRecord(BaseModel):
    """One accepted decision (``stake > 0``: a bet the bankroll rules let through)."""

    match_id: int
    decided_at: datetime
    settled_at: datetime | None = None
    market_type: str
    spec_key: str
    selection: str
    odds: Decimal
    closing_odds: Decimal | None = None
    clv: float | None = None
    p_win: float
    p_settled: float
    sigma: float
    ev: float
    stake: Decimal
    outcome: str = "open"  # won / lost / void / push / unknown
    payout: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")
    flat_pnl: float = 0.0  # profit of a 1-unit stake on the same bet
    model_version: str = ""

    @property
    def placed(self) -> bool:
        return self.stake > 0


class PredictionRecord(BaseModel):
    """One priced outcome that settled won / lost (for the probability metrics)."""

    match_id: int
    market_type: str
    month: str
    p: float  # p(win | settled)
    y: int  # 1 won, 0 lost
    bet: bool = False


class BreakdownRow(BaseModel):
    key: str
    n_bets: int = 0
    n_won: int = 0
    n_lost: int = 0
    n_refunded: int = 0
    staked: Decimal = Decimal("0")
    profit: Decimal = Decimal("0")
    roi: float | None = None
    hit_rate: float | None = None
    avg_odds: float | None = None
    avg_clv: float | None = None
    flat_n: int = 0
    flat_profit: float = 0.0
    flat_roi: float | None = None
    n_predictions: int = 0
    brier: float | None = None


def max_drawdown(values: Sequence[Decimal]) -> tuple[Decimal, float]:
    """``(largest peak-to-trough fall, that fall / the peak)`` of a bankroll series."""
    peak: Decimal | None = None
    worst = Decimal("0")
    worst_pct = 0.0
    for v in values:
        if peak is None or v > peak:
            peak = v
        fall = peak - v
        if fall > worst:
            worst = fall
        if peak > 0:
            worst_pct = max(worst_pct, float(fall / peak))
    return worst, worst_pct


def _mean(xs: Iterable[float]) -> float | None:
    vals = [x for x in xs if x is not None and math.isfinite(x)]
    return sum(vals) / len(vals) if vals else None


def breakdown(
    bets: Sequence[BetRecord],
    predictions: Sequence[PredictionRecord],
    key_of_bet: Callable[[BetRecord], str],
    key_of_prediction: Callable[[PredictionRecord], str],
) -> list[BreakdownRow]:
    """Group bets (money and flat metrics) and predictions (Brier) by a key."""
    rows: dict[str, BreakdownRow] = {}

    def row(key: str) -> BreakdownRow:
        if key not in rows:
            rows[key] = BreakdownRow(key=key)
        return rows[key]

    odds: dict[str, list[float]] = {}
    clvs: dict[str, list[float]] = {}
    for b in bets:
        r = row(key_of_bet(b))
        r.flat_n += 1
        r.flat_profit += b.flat_pnl
        if not b.placed:
            continue
        r.n_bets += 1
        r.staked += b.stake
        r.profit += b.pnl
        if b.outcome == "won":
            r.n_won += 1
        elif b.outcome == "lost":
            r.n_lost += 1
        else:
            r.n_refunded += 1
        odds.setdefault(r.key, []).append(float(b.odds))
        if b.clv is not None:
            clvs.setdefault(r.key, []).append(b.clv)
    sq: dict[str, list[float]] = {}
    for p in predictions:
        r = row(key_of_prediction(p))
        r.n_predictions += 1
        sq.setdefault(r.key, []).append((p.p - p.y) ** 2)
    for key, r in rows.items():
        r.roi = float(r.profit / r.staked) if r.staked > 0 else None
        decided = r.n_won + r.n_lost
        r.hit_rate = r.n_won / decided if decided else None
        r.avg_odds = _mean(odds.get(key, []))
        r.avg_clv = _mean(clvs.get(key, []))
        r.flat_roi = r.flat_profit / r.flat_n if r.flat_n else None
        r.brier = _mean(sq.get(key, []))
    return [rows[k] for k in sorted(rows)]


class BacktestReport(BaseModel):
    """Everything a walk-forward backtest measured (see the module docstring for definitions)."""

    created_at: datetime
    start: datetime | None = None
    end: datetime | None = None
    model_versions: list[str] = Field(default_factory=list)
    settings_summary: dict[str, Any] = Field(default_factory=dict)

    n_matches: int = 0
    n_matches_predicted: int = 0
    n_matches_skipped: int = 0
    n_retrains: int = 0
    n_prediction_errors: int = 0
    n_quotes: int = 0
    n_priced: int = 0
    n_unpriceable: int = 0
    n_accepted: int = 0  # decisions accepted by the decision step (flat metrics)
    n_bets: int = 0  # accepted with a positive stake
    n_won: int = 0
    n_lost: int = 0
    n_void: int = 0
    n_push: int = 0
    n_unknown: int = 0
    hit_rate: float | None = None

    initial_bankroll: Decimal = Decimal("0")
    final_bankroll: Decimal = Decimal("0")
    total_staked: Decimal = Decimal("0")
    total_returned: Decimal = Decimal("0")
    profit: Decimal = Decimal("0")
    roi: float | None = None
    bankroll_growth: float | None = None
    max_drawdown: Decimal = Decimal("0")
    max_drawdown_pct: float = 0.0

    flat_n: int = 0
    flat_profit: float = 0.0
    flat_roi: float | None = None

    n_clv: int = 0
    avg_clv: float | None = None
    positive_clv_share: float | None = None
    avg_clv_flat: float | None = None  # over every accepted decision with a closing price

    n_predictions: int = 0
    brier_all: float | None = None
    log_loss_all: float | None = None
    n_bet_predictions: int = 0
    brier_bets: float | None = None
    log_loss_bets: float | None = None
    calibration: dict[str, Any] = Field(default_factory=dict)

    profit_curve: list[CurvePoint] = Field(default_factory=list)
    by_market: list[BreakdownRow] = Field(default_factory=list)
    by_month: list[BreakdownRow] = Field(default_factory=list)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    bets: list[BetRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------ rendering
    def to_markdown(self, *, max_bets: int = 20) -> str:
        """A human-readable summary (Markdown tables) for the owner / the dashboard."""

        def pct(x: float | None, digits: int = 1) -> str:
            return "-" if x is None or not math.isfinite(x) else f"{x * 100:.{digits}f}%"

        def num(x: float | None, digits: int = 4) -> str:
            return "-" if x is None or not math.isfinite(x) else f"{x:.{digits}f}"

        def money(x: Decimal) -> str:
            return f"{x:,.2f} PLN"

        period = (
            f"{self.start:%Y-%m-%d} to {self.end:%Y-%m-%d}"
            if self.start and self.end
            else "the whole history"
        )
        lines = [
            "# Backtest report",
            "",
            f"Walk-forward backtest over {period}: {self.n_matches_predicted} of {self.n_matches} matches "
            f"predicted, {self.n_retrains} retrains, models {', '.join(self.model_versions) or '-'}.",
            "",
            "## Betting",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Bets placed | {self.n_bets} (accepted decisions: {self.n_accepted}) |",
            f"| Won / lost / void / push / unknown | {self.n_won} / {self.n_lost} / {self.n_void} / "
            f"{self.n_push} / {self.n_unknown} |",
            f"| Hit rate | {pct(self.hit_rate)} |",
            f"| Staked | {money(self.total_staked)} |",
            f"| Profit | {money(self.profit)} |",
            f"| ROI (yield) | {pct(self.roi, 2)} |",
            f"| Bankroll | {money(self.initial_bankroll)} -> {money(self.final_bankroll)} "
            f"({pct(self.bankroll_growth, 2)}) |",
            f"| Max drawdown | {money(self.max_drawdown)} ({pct(self.max_drawdown_pct, 2)}) |",
            f"| Flat 1-unit bets | {self.flat_n}, profit {self.flat_profit:+.2f} units, "
            f"ROI {pct(self.flat_roi, 2)} |",
            f"| Average CLV | {pct(self.avg_clv, 2)} over {self.n_clv} bets "
            f"({pct(self.positive_clv_share)} beat the close) |",
            "",
            "## Probabilities",
            "",
            "| Set | n | Brier | Log-loss |",
            "|---|---|---|---|",
            f"| All priced outcomes | {self.n_predictions} | {num(self.brier_all)} | "
            f"{num(self.log_loss_all)} |",
            f"| Bets placed | {self.n_bet_predictions} | {num(self.brier_bets)} | "
            f"{num(self.log_loss_bets)} |",
            "",
        ]
        cal = self.calibration or {}
        if cal.get("n"):
            lines += [
                f"Calibration: ECE {num(cal.get('ece'))}, sharpness {num(cal.get('sharpness'))}, "
                f"Brier skill {num(cal.get('brier_skill'))}.",
                "",
                "| Predicted | Observed | n | 95% CI |",
                "|---|---|---|---|",
            ]
            for b in cal.get("bins", []):
                lines.append(
                    f"| {b['mean_predicted']:.3f} | {b['observed_rate']:.3f} | {b['count']} | "
                    f"{b['ci_low']:.3f}-{b['ci_high']:.3f} |"
                )
            lines.append("")

        def table(title: str, rows: list[BreakdownRow]) -> None:
            if not rows:
                return
            lines.extend(
                [
                    f"## {title}",
                    "",
                    "| Key | Bets | Hit rate | Staked | Profit | ROI | Avg CLV | Flat ROI | Brier |",
                    "|---|---|---|---|---|---|---|---|---|",
                ]
            )
            for r in rows:
                lines.append(
                    f"| {r.key} | {r.n_bets} | {pct(r.hit_rate)} | {r.staked:.2f} | {r.profit:+.2f} | "
                    f"{pct(r.roi, 2)} | {pct(r.avg_clv, 2)} | {pct(r.flat_roi, 2)} | {num(r.brier)} |"
                )
            lines.append("")

        table("By market", self.by_market)
        table("By month", self.by_month)
        if self.rejection_reasons:
            lines += ["## Why outcomes were not bet", "", "| Reason | Count |", "|---|---|"]
            for reason, count in sorted(self.rejection_reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"| {reason} | {count} |")
            lines.append("")
        placed = [b for b in self.bets if b.placed]
        if placed and max_bets > 0:
            lines += [
                f"## Bets (last {min(max_bets, len(placed))} of {len(placed)})",
                "",
                "| Decided | Match | Market | Selection | Odds | Close | p | Stake | Result | P&L |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for b in placed[-max_bets:]:
                close = f"{b.closing_odds:.2f}" if b.closing_odds is not None else "-"
                lines.append(
                    f"| {b.decided_at:%Y-%m-%d %H:%M} | {b.match_id} | {b.spec_key} | {b.selection} | "
                    f"{b.odds:.2f} | {close} | {b.p_settled:.3f} | {b.stake:.2f} | {b.outcome} | "
                    f"{b.pnl:+.2f} |"
                )
            lines.append("")
        if self.warnings:
            lines += ["## Warnings", ""] + [f"- {w}" for w in self.warnings] + [""]
        return "\n".join(lines)
