"""Polish betting tax model, expected value and Kelly staking with refunds.

How a Betclic.pl single bet is paid out
---------------------------------------
1. **Stake tax** (podatek od gier, 12 % of the stakes of mutual bets, ustawa o grach hazardowych).
   The operator owes it on every accepted stake and passes it on by deducting it from the stake:
   of a 100 PLN stake 12 PLN is tax and 88 PLN is "in play", so a winning bet pays
   ``(stake - stake tax) x odds``. ``bets.stake`` is the GROSS stake (what leaves the balance) and
   ``bets.tax = stake x tax rate``. Betclic's "bez podatku" promos absorb the tax (factor 1.00
   instead of 0.88); :attr:`cs2bot.config.Settings.effective_tax_rate` is then 0.
2. **Winnings tax** (PIT, art. 30 ust. 1 pkt 2 ustawy o PIT, 10 %). Winnings from operators licensed
   in Poland are exempt only if a *single* winning does not exceed 2,280 PLN (art. 21 ust. 1 pkt 6a).
   Above the threshold the exemption is lost for the whole amount and the bookmaker, as withholding
   agent (płatnik, art. 41 ust. 4), withholds 10 % of the **whole winning** - the full amount paid out
   on the coupon, ``(stake - stake tax) x odds``, returned stake included - not 10 % of the net profit
   and not 10 % of the excess over 2,280 PLN. The flat-rate PIT is levied on revenue "without
   deducting the costs of obtaining it", so the stake is not deductible. This is how Polish
   bookmakers apply it: stake 1,000 PLN at odds 3.00 -> 880 x 3.00 = 2,640 PLN > 2,280 PLN ->
   264 PLN withheld -> 2,376 PLN credited. The threshold is compared with the unrounded winning
   (2,280.00 is still exempt, 2,280.01 is taxed); base and tax are rounded to full złoty, half up
   (art. 63 § 1 Ordynacji podatkowej - the grosz rule of § 1a covers only art. 30a capital income).
3. **Refunds**: a voided or pushed single returns the full GROSS stake, stake tax included.
   ASSUMPTION: there is no published Betclic rule for this case; it follows the usual Polish
   practice of returning the whole amount paid for a cancelled coupon. If Betclic ever refunds only
   the net stake, ``engine.ledger.reconcile`` will flag the difference after the first void bet.

Expected value and Kelly
------------------------
Per unit of gross stake, with effective odds ``o_eff = (1 - t) x odds`` and net odds ``b = o_eff - 1``,
a bet returns ``+b`` with probability ``p_w``, ``-1`` with ``p_l`` and ``0`` with ``p_r = p_push +
p_void`` (refund), so ``EV = p_w b - p_l``. The Kelly stake maximises the expected log-growth

    G(f) = p_w ln(1 + b f) + p_l ln(1 - f) + p_r ln(1)

    G'(f) = p_w b / (1 + b f) - p_l / (1 - f) = 0
      =>   p_w b (1 - f) = p_l (1 + b f)
      =>   f* = (b p_w - p_l) / (b (p_w + p_l))

``G''(f) < 0``, so ``f*`` is the maximum. ``f* > 0`` exactly when ``EV > 0``, it increases with the
odds, and it depends on the probabilities only through ``p_w / (p_w + p_l)`` (the probability of
winning given the bet settles): moving probability mass into refunds proportionally leaves ``f*``
unchanged while scaling the EV. With no refunds it reduces to the classic ``(b p - (1 - p)) / b``.

The 10 % winnings tax depends on the stake (it only bites when the winning exceeds 2,280 PLN), so
:func:`expected_value` takes an optional stake and :func:`big_win_stake_limit` gives the largest
stake that keeps the winning tax-free; ``engine.bankroll.size_stake`` uses it to decide whether
crossing the threshold is worth it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from cs2bot.config import BetclicSettings, Settings
from cs2bot.domain.betting import PricedOutcome
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.utils.money import CENT, D, quantize_money

ZLOTY = Decimal("1")
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class TaxModel:
    """The three numbers that define how a winning single is paid out."""

    stake_tax_rate: Decimal  # deducted from every stake (0 during "bez podatku" promos)
    big_win_threshold: Decimal  # PIT exemption limit for a single winning (2,280 PLN)
    big_win_rate: Decimal  # PIT withheld from the whole winning above the threshold (10 %)

    def __post_init__(self) -> None:
        for name in ("stake_tax_rate", "big_win_rate"):
            value = getattr(self, name)
            if not Decimal(0) <= value < Decimal(1):
                raise ValueError(f"{name} must be in [0, 1), got {value}")
        if self.big_win_threshold < 0:
            raise ValueError("big_win_threshold must be >= 0")

    @classmethod
    def from_settings(cls, settings: Settings | BetclicSettings | TaxModel) -> TaxModel:
        if isinstance(settings, TaxModel):
            return settings
        if isinstance(settings, Settings):
            return cls(
                stake_tax_rate=D(settings.effective_tax_rate),
                big_win_threshold=D(settings.betclic.big_win_tax_threshold),
                big_win_rate=D(settings.betclic.big_win_tax_rate),
            )
        return cls(
            stake_tax_rate=Decimal(0) if settings.tax_free_promo else D(settings.tax_rate),
            big_win_threshold=D(settings.big_win_tax_threshold),
            big_win_rate=D(settings.big_win_tax_rate),
        )


TaxSource = Settings | BetclicSettings | TaxModel


@dataclass(frozen=True)
class PayoutBreakdown:
    """Money flow of one single bet if it wins (all amounts in PLN, rounded to grosze)."""

    stake: Decimal  # gross stake, leaves the balance when the bet is placed
    stake_tax: Decimal  # stake tax deducted by the bookmaker (0 in promos)
    stake_tax_rate: Decimal
    net_stake: Decimal  # stake - stake_tax, the amount actually in play
    odds: Decimal
    gross_win: Decimal  # net_stake x odds ("możliwa wygrana" on the coupon)
    win_tax: Decimal  # 10 % PIT withheld when gross_win > 2,280 PLN, else 0
    net_payout: Decimal  # credited to the balance when the bet wins

    @property
    def profit(self) -> Decimal:
        """Net profit of a win (``net_payout - stake``)."""
        return self.net_payout - self.stake


def effective_odds(odds: Decimal, *, tax_rate: Decimal) -> Decimal:
    """Odds after the stake tax: ``(1 - tax_rate) x odds`` (``0.88 x odds`` by default), unrounded."""
    rate = D(tax_rate)
    if not Decimal(0) <= rate < Decimal(1):
        raise ValueError(f"tax_rate must be in [0, 1), got {rate}")
    return D(odds) * (Decimal(1) - rate)


def stake_tax(stake: Decimal, settings: TaxSource) -> Decimal:
    """Stake tax charged on a gross stake, rounded to grosze (``bets.tax``)."""
    model = TaxModel.from_settings(settings)
    return quantize_money(quantize_money(D(stake)) * model.stake_tax_rate)


def winnings_tax(gross_win: Decimal, settings: TaxSource) -> Decimal:
    """PIT withheld from a single winning: 10 % of the whole winning if it exceeds the threshold."""
    model = TaxModel.from_settings(settings)
    win = D(gross_win)
    if model.big_win_rate <= 0 or win <= model.big_win_threshold:
        return _ZERO
    base = win.quantize(ZLOTY, rounding=ROUND_HALF_UP)
    return (base * model.big_win_rate).quantize(ZLOTY, rounding=ROUND_HALF_UP).quantize(CENT)


def payout_breakdown(
    stake: Decimal, odds: Decimal, settings: TaxSource, *, tax: Decimal | None = None
) -> PayoutBreakdown:
    """Full money flow of a winning single. ``tax`` overrides the stake tax (e.g. ``bets.tax``)."""
    model = TaxModel.from_settings(settings)
    gross_stake = quantize_money(D(stake))
    price_ = D(odds)
    if gross_stake < 0:
        raise ValueError("stake must be >= 0")
    if price_ < 1:
        raise ValueError(f"decimal odds must be >= 1, got {price_}")
    tax_amount = stake_tax(gross_stake, model) if tax is None else quantize_money(D(tax))
    if not _ZERO <= tax_amount <= gross_stake:
        raise ValueError(f"stake tax {tax_amount} is outside [0, stake]")
    net_stake = gross_stake - tax_amount
    gross_win = quantize_money(net_stake * price_)
    win_tax = winnings_tax(gross_win, model)
    rate = tax_amount / gross_stake if gross_stake > 0 else model.stake_tax_rate
    return PayoutBreakdown(
        stake=gross_stake,
        stake_tax=tax_amount,
        stake_tax_rate=rate,
        net_stake=net_stake,
        odds=price_,
        gross_win=gross_win,
        win_tax=win_tax,
        net_payout=gross_win - win_tax,
    )


def net_payout(stake: Decimal, odds: Decimal, settings: TaxSource) -> Decimal:
    """Amount credited to the balance when a single with this GROSS stake wins (after both taxes)."""
    return payout_breakdown(stake, odds, settings).net_payout


def big_win_stake_limit(odds: Decimal, settings: TaxSource) -> Decimal | None:
    """Largest gross stake (in grosze) whose winning stays at or below the PIT threshold.

    ``None`` when there is no winnings tax (rate 0).
    """
    model = TaxModel.from_settings(settings)
    if model.big_win_rate <= 0:
        return None
    price_ = D(odds)
    factor = (Decimal(1) - model.stake_tax_rate) * price_
    if factor <= 0:
        return None

    def gross(s: Decimal) -> Decimal:
        return quantize_money((s - quantize_money(s * model.stake_tax_rate)) * price_)

    limit = (model.big_win_threshold / factor).quantize(CENT, rounding=ROUND_DOWN)
    while gross(limit + CENT) <= model.big_win_threshold:  # gross() is non-decreasing in the stake
        limit += CENT
    while limit > 0 and gross(limit) > model.big_win_threshold:
        limit -= CENT
    return max(limit, _ZERO)


def expected_value(
    outcome: OutcomeProbability, odds: Decimal, settings: TaxSource, stake: Decimal | None = None
) -> float:
    """Expected profit per 1 unit of gross stake, after tax, refunds (push/void) worth 0.

    Without ``stake``: ``p_win (o_eff - 1) - p_lose`` (exact for small stakes, no winnings tax).
    With ``stake``: the exact payout of that stake (grosz rounding and the 10 % winnings tax above
    2,280 PLN) is used, so the EV can drop for large stakes.
    """
    model = TaxModel.from_settings(settings)
    p_win, p_lose = outcome.p_win, outcome.p_lose
    if stake is None:
        b = float(effective_odds(odds, tax_rate=model.stake_tax_rate)) - 1.0
        return p_win * b - p_lose
    if D(stake) <= 0:
        raise ValueError("stake must be positive")
    breakdown = payout_breakdown(stake, odds, model)
    if breakdown.stake <= 0:
        raise ValueError("stake rounds to 0.00")
    win_return = float(breakdown.net_payout / breakdown.stake) - 1.0
    return p_win * win_return - p_lose


def kelly_for_net_odds(p_win: float, p_lose: float, b: float) -> float:
    """Full-Kelly fraction ``(b p_w - p_l) / (b (p_w + p_l))`` for net odds ``b``, clipped to [-1, 1].

    A negative value only means "no edge" (a single bet cannot be shorted); it is clipped at -1 so
    the function stays non-decreasing in the odds, and ``b <= 0`` (a win cannot even return the
    stake) gives -1. A bet that can only be refunded (``p_w + p_l = 0``) gives 0. The result is
    positive exactly when the per-unit EV ``b p_w - p_l`` is positive.
    """
    settled = p_win + p_lose
    if settled <= 0:
        return 0.0
    if b <= 0:
        return -1.0
    return max(-1.0, min(1.0, (b * p_win - p_lose) / (b * settled)))


def kelly_fraction(outcome: OutcomeProbability, odds: Decimal, settings: TaxSource) -> float:
    """Full-Kelly fraction of bankroll after the stake tax, with refunds leaving wealth unchanged.

    Can be <= 0 (no edge). Ignores the stake-dependent winnings tax (see :func:`big_win_stake_limit`).
    """
    model = TaxModel.from_settings(settings)
    b = float(effective_odds(odds, tax_rate=model.stake_tax_rate)) - 1.0
    return kelly_for_net_odds(outcome.p_win, outcome.p_lose, b)


def expected_log_growth(p_win: float, p_lose: float, b: float, fraction: float) -> float:
    """``G(f) = p_w ln(1 + b f) + p_l ln(1 - f)`` - expected log-growth of staking ``f`` of bankroll."""
    if fraction <= 0:
        return 0.0
    if fraction >= 1.0:
        return -math.inf if p_lose > 0 else p_win * math.log1p(b * fraction)
    if 1.0 + b * fraction <= 0:
        return -math.inf
    return p_win * math.log1p(b * fraction) + p_lose * math.log1p(-fraction)


def price(outcome: OutcomeProbability, quote: OddsQuote, settings: TaxSource) -> PricedOutcome:
    """Join a model probability with a bookmaker price: effective odds, per-unit EV, full Kelly."""
    model = TaxModel.from_settings(settings)
    return PricedOutcome(
        outcome=outcome,
        quote=quote,
        effective_odds=effective_odds(quote.odds, tax_rate=model.stake_tax_rate),
        ev=expected_value(outcome, quote.odds, model),
        kelly_full=kelly_fraction(outcome, quote.odds, model),
    )


def price_all(
    pairs: Iterable[tuple[OutcomeProbability, OddsQuote]], settings: TaxSource
) -> list[PricedOutcome]:
    """:func:`price` for every ``(outcome, quote)`` pair returned by ``pricing.price_quotes``."""
    model = TaxModel.from_settings(settings)
    return [price(outcome, quote, model) for outcome, quote in pairs]
