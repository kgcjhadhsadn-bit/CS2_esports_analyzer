"""Small, display-only expected-value helpers (the API must not import ``cs2bot.engine``).

Polish stake tax: the bookmaker keeps ``tax_rate`` of every stake, so a gross stake ``S`` is
effectively ``(1 - tax_rate) * S`` and a winning bet pays ``(1 - tax_rate) * S * odds``. Voided and
pushed bets refund the gross stake, minus ``refund_loss_rate * S`` (0 by default; the stake tax
rate when ``betclic.void_refunds_stake_tax`` is off). Per 1 unit of gross stake - the same formula
as ``cs2bot.engine.ev.expected_value`` without a stake:

    EV = p_win * (1 - tax_rate) * odds + (p_push + p_void) * (1 - refund_loss_rate) - 1

The 10% tax on single wins above 2280 PLN depends on the stake size and is ignored here (the
decision engine accounts for it when sizing real bets).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cs2bot.config import Settings
from cs2bot.utils.money import D

_ZERO = Decimal(0)


@dataclass(frozen=True)
class TaxTerms:
    """The two tax numbers the display EV needs (see the module doc)."""

    tax_rate: Decimal
    refund_loss_rate: Decimal = _ZERO

    @classmethod
    def from_settings(cls, settings: Settings) -> TaxTerms:
        rate = D(settings.effective_tax_rate)
        return cls(
            tax_rate=rate,
            refund_loss_rate=_ZERO if settings.betclic.void_refunds_stake_tax else rate,
        )


def _terms(tax: TaxTerms | Decimal) -> TaxTerms:
    return tax if isinstance(tax, TaxTerms) else TaxTerms(D(tax))


def effective_odds(odds: Decimal, tax: TaxTerms | Decimal) -> Decimal:
    """Odds after the stake tax (``0.88 * odds`` with the default 12% tax)."""
    return D(odds) * (Decimal(1) - _terms(tax).tax_rate)


def _refund_value(tax: TaxTerms, p_push: float, p_void: float) -> float:
    """Expected money back from refunds per unit staked (push / void probabilities floored at 0)."""
    return (max(0.0, p_push) + max(0.0, p_void)) * (1.0 - float(tax.refund_loss_rate))


def ev_after_tax(
    p_win: float, odds: Decimal, tax: TaxTerms | Decimal, *, p_push: float = 0.0, p_void: float = 0.0
) -> float:
    """Expected profit per 1 unit of gross stake, after the stake tax, refunds included."""
    terms = _terms(tax)
    return p_win * float(effective_odds(odds, terms)) + _refund_value(terms, p_push, p_void) - 1.0


def p_win_given_settled(p_win: float, *, p_push: float = 0.0, p_void: float = 0.0) -> float | None:
    """P(win | the bet is not refunded) - the number the decision engine compares with
    ``min_probability`` and the right one to calibrate against won / lost results (``None`` when
    the bet is refunded with certainty)."""
    settled = 1.0 - max(0.0, p_push) - max(0.0, p_void)
    if settled <= 1e-9:
        return None
    return min(1.0, max(0.0, p_win / settled))


def fair_odds(p_win: float, *, p_push: float = 0.0, p_void: float = 0.0) -> Decimal | None:
    """Odds at which the bet breaks even without tax and margin (``None`` if ``p_win`` is ~0)."""
    if p_win <= 1e-9:
        return None
    return D((1.0 - max(0.0, p_push) - max(0.0, p_void)) / p_win)


def breakeven_odds(
    p_win: float, tax: TaxTerms | Decimal, *, p_push: float = 0.0, p_void: float = 0.0
) -> Decimal | None:
    """Smallest bookmaker odds with ``EV >= 0`` after the stake tax."""
    terms = _terms(tax)
    keep = 1.0 - float(terms.tax_rate)
    if p_win <= 1e-9 or keep <= 0:
        return None
    return D((1.0 - _refund_value(terms, p_push, p_void)) / (p_win * keep))
