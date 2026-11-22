"""Decimal money helpers. Never use float for stakes, balances or payouts."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def D(value: object) -> Decimal:
    """Convert int/str/float/Decimal to Decimal (floats go through str to avoid binary noise)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def quantize_money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def round_down_to_step(amount: Decimal, step: Decimal) -> Decimal:
    """Round a stake DOWN to the bookmaker's stake increment (never exceed the sized stake)."""
    if step <= 0:
        raise ValueError("step must be positive")
    units = (amount / step).to_integral_value(rounding=ROUND_DOWN)
    return quantize_money(units * step)
