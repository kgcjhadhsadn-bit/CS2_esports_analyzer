"""Small, display-only expected-value helpers (the API must not import ``cs2bot.engine``).

Polish stake tax: the bookmaker keeps ``tax_rate`` of every stake, so a gross stake ``S`` is
effectively ``(1 - tax_rate) * S`` and a winning bet pays ``(1 - tax_rate) * S * odds``. Voided and
pushed bets refund the full gross stake. Per 1 unit of gross stake:

    EV = p_win * (1 - tax_rate) * odds + p_push + p_void - 1

The 10% tax on single wins above 2280 PLN depends on the stake size and is ignored here (the
decision engine accounts for it when sizing real bets).
"""

from __future__ import annotations

from decimal import Decimal

from cs2bot.utils.money import D


def effective_odds(odds: Decimal, tax_rate: Decimal) -> Decimal:
    """Odds after the stake tax (``0.88 * odds`` with the default 12% tax)."""
    return D(odds) * (Decimal(1) - D(tax_rate))


def ev_after_tax(
    p_win: float, odds: Decimal, tax_rate: Decimal, *, p_push: float = 0.0, p_void: float = 0.0
) -> float:
    """Expected profit per 1 unit of gross stake, after the stake tax, refunds included."""
    return p_win * float(effective_odds(odds, tax_rate)) + p_push + p_void - 1.0


def fair_odds(p_win: float, *, p_push: float = 0.0, p_void: float = 0.0) -> Decimal | None:
    """Odds at which the bet breaks even without tax and margin (``None`` if ``p_win`` is ~0)."""
    if p_win <= 1e-9:
        return None
    return D((1.0 - p_push - p_void) / p_win)


def breakeven_odds(
    p_win: float, tax_rate: Decimal, *, p_push: float = 0.0, p_void: float = 0.0
) -> Decimal | None:
    """Smallest bookmaker odds with ``EV >= 0`` after the stake tax."""
    keep = 1.0 - float(tax_rate)
    if p_win <= 1e-9 or keep <= 0:
        return None
    return D((1.0 - p_push - p_void) / (p_win * keep))
