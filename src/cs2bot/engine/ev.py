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
   If Betclic turns out to withhold to the grosz instead, set
   ``betclic.winnings_tax_rounding = "grosz"`` (10 % of the unrounded winning, half up to the grosz).
3. **Refunds**: a voided or pushed single returns the full GROSS stake, stake tax included
   (``betclic.void_refunds_stake_tax = True``). ASSUMPTION: there is no published Betclic rule for
   this case; it follows the usual Polish practice of returning the whole amount paid for a
   cancelled coupon (the stake tax is owed on accepted stakes, and a cancelled single is not one).
   If Betclic ever refunds only the net stake, ``engine.ledger.reconcile`` flags the difference
   after the first void bet and the owner sets ``void_refunds_stake_tax = False``: a refund then
   returns ``stake - stake tax`` and every refunded bet costs the stake tax (see below).

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

When refunds do NOT return the stake tax (``void_refunds_stake_tax = False``) a refund costs
``c = t`` per unit staked: ``EV = p_w b - p_l - p_r c`` and the growth rate gains the term
``p_r ln(1 - c f)``. ``G'(f) = p_w b / (1 + b f) - p_l / (1 - f) - p_r c / (1 - c f)`` is strictly
decreasing, so the optimum is the unique root (found by bisection); it is still positive exactly
when ``EV > 0`` and non-decreasing in the odds.

The 10 % winnings tax depends on the stake (it only bites when the winning exceeds 2,280 PLN), so
:func:`expected_value` takes an optional stake and :func:`big_win_stake_limit` gives the largest
stake that keeps the winning tax-free; ``engine.bankroll.size_stake`` uses it to decide whether
crossing the threshold is worth it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Literal

from cs2bot.config import BetclicSettings, Settings
from cs2bot.domain.betting import PricedOutcome
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.utils.money import CENT, D, quantize_money

ZLOTY = Decimal("1")
_ZERO = Decimal("0.00")


WinTaxRounding = Literal["zloty", "grosz"]


@dataclass(frozen=True)
class TaxModel:
    """The numbers and settlement rules that define how a single bet is paid out."""

    stake_tax_rate: Decimal  # deducted from every stake (0 during "bez podatku" promos)
    big_win_threshold: Decimal  # PIT exemption limit for a single winning (2,280 PLN)
    big_win_rate: Decimal  # PIT withheld from the whole winning above the threshold (10 %)
    # ``BetclicSettings.void_refunds_stake_tax``: a void / pushed bet returns the full gross stake
    # (True) or only ``stake - stake tax`` (False).
    refund_stake_tax: bool = True
    # ``BetclicSettings.winnings_tax_rounding``: PIT base and tax rounded to full zloty (art. 63 § 1
    # Ordynacji podatkowej) or computed to the grosz.
    win_tax_rounding: WinTaxRounding = "zloty"

    def __post_init__(self) -> None:
        for name in ("stake_tax_rate", "big_win_rate"):
            value = getattr(self, name)
            if not Decimal(0) <= value < Decimal(1):
                raise ValueError(f"{name} must be in [0, 1), got {value}")
        if not self.big_win_threshold >= 0:
            raise ValueError("big_win_threshold must be >= 0")
        if self.win_tax_rounding not in ("zloty", "grosz"):
            raise ValueError(f"win_tax_rounding must be 'zloty' or 'grosz', got {self.win_tax_rounding!r}")

    @property
    def refund_loss_rate(self) -> Decimal:
        """Share of the gross stake that is lost when a bet is refunded (0 with full refunds)."""
        return Decimal(0) if self.refund_stake_tax else self.stake_tax_rate

    @classmethod
    def from_settings(cls, settings: Settings | BetclicSettings | TaxModel) -> TaxModel:
        if isinstance(settings, TaxModel):
            return settings
        betclic = settings.betclic if isinstance(settings, Settings) else settings
        return cls(
            stake_tax_rate=Decimal(0) if betclic.tax_free_promo else D(betclic.tax_rate),
            big_win_threshold=D(betclic.big_win_tax_threshold),
            big_win_rate=D(betclic.big_win_tax_rate),
            refund_stake_tax=bool(betclic.void_refunds_stake_tax),
            win_tax_rounding=betclic.winnings_tax_rounding,
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
    """PIT withheld from a single winning: 10 % of the whole winning if it exceeds the threshold.

    The threshold is compared with the unrounded winning. ``win_tax_rounding = "zloty"`` rounds the
    base and the tax to full zloty (half up); ``"grosz"`` takes 10 % of the winning to the grosz.
    """
    model = TaxModel.from_settings(settings)
    win = D(gross_win)
    if model.big_win_rate <= 0 or win <= model.big_win_threshold:
        return _ZERO
    if model.win_tax_rounding == "grosz":
        return quantize_money(win * model.big_win_rate)
    base = win.quantize(ZLOTY, rounding=ROUND_HALF_UP)
    return (base * model.big_win_rate).quantize(ZLOTY, rounding=ROUND_HALF_UP).quantize(CENT)


def refund_amount(stake: Decimal, settings: TaxSource, *, tax: Decimal | None = None) -> Decimal:
    """Amount returned to the balance when a bet with this GROSS stake is voided or pushed.

    The full stake by default; ``stake - stake tax`` when ``void_refunds_stake_tax`` is off (``tax``
    overrides the stake tax, e.g. the tax actually charged on the bet at placement).
    """
    model = TaxModel.from_settings(settings)
    gross_stake = quantize_money(D(stake))
    if gross_stake < 0:
        raise ValueError("stake must be >= 0")
    if model.refund_stake_tax:
        return gross_stake
    tax_amount = stake_tax(gross_stake, model) if tax is None else quantize_money(D(tax))
    if not _ZERO <= tax_amount <= gross_stake:
        raise ValueError(f"stake tax {tax_amount} is outside [0, stake]")
    return gross_stake - tax_amount


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


def _probabilities(outcome: OutcomeProbability) -> tuple[float, float, float]:
    """``(p_win, p_lose, p_refund)`` of an outcome (refund = push + void)."""
    return outcome.p_win, outcome.p_lose, max(0.0, outcome.p_push) + max(0.0, outcome.p_void)


def expected_value(
    outcome: OutcomeProbability, odds: Decimal, settings: TaxSource, stake: Decimal | None = None
) -> float:
    """Expected profit per 1 unit of gross stake, after tax.

    Without ``stake``: ``p_win (o_eff - 1) - p_lose - p_refund c`` where ``c`` is the share of the
    stake a refund costs (0 with full refunds, the stake tax rate otherwise); exact for small stakes
    (no winnings tax). With ``stake``: the exact payout and refund of that stake (grosz rounding and
    the 10 % winnings tax above 2,280 PLN) are used, so the EV can drop for large stakes.
    """
    model = TaxModel.from_settings(settings)
    p_win, p_lose, p_refund = _probabilities(outcome)
    if stake is None:
        b = float(effective_odds(odds, tax_rate=model.stake_tax_rate)) - 1.0
        return p_win * b - p_lose - p_refund * float(model.refund_loss_rate)
    if D(stake) <= 0:
        raise ValueError("stake must be positive")
    breakdown = payout_breakdown(stake, odds, model)
    if breakdown.stake <= 0:
        raise ValueError("stake rounds to 0.00")
    win_return = float(breakdown.net_payout / breakdown.stake) - 1.0
    refund_return = float(refund_amount(breakdown.stake, model) / breakdown.stake) - 1.0
    return p_win * win_return - p_lose + p_refund * refund_return


#: Odds are quoted (and stored) with at most three decimals.
ODDS_STEP = Decimal("0.001")
_MAX_MEANINGFUL_ODDS = 1e6


def min_odds_for_ev(
    outcome: OutcomeProbability, settings: TaxSource, *, min_ev: float = 0.0
) -> Decimal | None:
    """Lowest decimal odds at which the per-unit EV (no stake, see :func:`expected_value`) is still
    ``>= min_ev``, rounded UP to three decimals. ``None`` when the outcome cannot win (or would need
    odds above one million).

    From ``EV = p_w ((1 - t) o - 1) - p_l - p_r c >= m``:  ``o >= (m + p_w + p_l + p_r c) / (p_w (1 - t))``.
    Used for ``BetOrder.min_acceptable_odds``: if the price drops below it between the decision and the
    placement, the edge the decision relied on is gone and the bet must not be placed.
    """
    model = TaxModel.from_settings(settings)
    p_win, p_lose, p_refund = _probabilities(outcome)
    if not (math.isfinite(min_ev) and math.isfinite(p_win) and math.isfinite(p_lose)) or p_win <= 0:
        return None
    keep = 1.0 - float(model.stake_tax_rate)
    exact = (min_ev + p_win + p_lose + p_refund * float(model.refund_loss_rate)) / (p_win * keep)
    if not math.isfinite(exact) or exact > _MAX_MEANINGFUL_ODDS:
        return None  # a win so unlikely that no real price can pay for it
    odds = max(D(exact).quantize(ODDS_STEP, rounding=ROUND_UP), Decimal("1.000"))
    for _ in range(3):  # float noise in ``exact``: make sure the rounded odds really clear the bar
        if expected_value(outcome, odds, model) >= min_ev:
            break
        odds += ODDS_STEP
    return odds


def kelly_for_net_odds(
    p_win: float, p_lose: float, b: float, *, p_refund: float = 0.0, refund_loss: float = 0.0
) -> float:
    """Full-Kelly fraction for net odds ``b``, clipped to [-1, 1].

    With full refunds (``refund_loss = 0``, the default) this is the closed form
    ``(b p_w - p_l) / (b (p_w + p_l))``. When a refund costs ``refund_loss`` per unit staked (see the
    module docstring) the optimum is the root of ``G'(f)``, found by bisection.

    A negative value only means "no edge" (a single bet cannot be shorted); it is clipped at -1 so
    the function stays non-decreasing in the odds, and ``b <= 0`` (a win cannot even return the
    stake) gives -1. A bet that can only be refunded at no cost gives 0. The result is positive
    exactly when the per-unit EV ``b p_w - p_l - p_r c`` is positive.
    """
    cost = p_refund * refund_loss if refund_loss > 0 and p_refund > 0 else 0.0
    settled = p_win + p_lose
    if settled <= 0 and cost <= 0:
        return 0.0
    if b <= 0:
        return -1.0
    edge = b * p_win - p_lose - cost
    if cost <= 0:
        # edge / (b * settled), written so that a tiny (subnormal) settled probability cannot underflow
        # the denominator to zero
        return max(-1.0, min(1.0, (p_win - p_lose / b) / settled))
    if edge <= 0:  # no edge: a negative number that still increases with the odds
        return max(-1.0, edge / (b * (settled + cost)))

    def slope(f: float) -> float:
        return p_win * b / (1.0 + b * f) - p_lose / (1.0 - f) - cost / (1.0 - refund_loss * f)

    lo, hi = 0.0, 1.0 - 1e-12
    if slope(hi) >= 0:
        return 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if slope(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def kelly_fraction(outcome: OutcomeProbability, odds: Decimal, settings: TaxSource) -> float:
    """Full-Kelly fraction of bankroll after the stake tax, with refunds leaving wealth unchanged
    (or costing the stake tax when ``void_refunds_stake_tax`` is off).

    Can be <= 0 (no edge). Ignores the stake-dependent winnings tax (see :func:`big_win_stake_limit`).
    """
    model = TaxModel.from_settings(settings)
    b = float(effective_odds(odds, tax_rate=model.stake_tax_rate)) - 1.0
    p_win, p_lose, p_refund = _probabilities(outcome)
    return kelly_for_net_odds(p_win, p_lose, b, p_refund=p_refund, refund_loss=float(model.refund_loss_rate))


def expected_log_growth(
    p_win: float,
    p_lose: float,
    b: float,
    fraction: float,
    *,
    p_refund: float = 0.0,
    refund_loss: float = 0.0,
) -> float:
    """``G(f) = p_w ln(1 + b f) + p_l ln(1 - f) + p_r ln(1 - c f)`` - expected log-growth of staking
    ``f`` of the bankroll (``c`` = ``refund_loss``, 0 with full refunds)."""
    if fraction <= 0:
        return 0.0
    refund_term = 0.0
    if p_refund > 0 and refund_loss > 0:
        if refund_loss * fraction >= 1.0:
            return -math.inf
        refund_term = p_refund * math.log1p(-refund_loss * fraction)
    if fraction >= 1.0:
        return -math.inf if p_lose > 0 else p_win * math.log1p(b * fraction) + refund_term
    if 1.0 + b * fraction <= 0:
        return -math.inf
    return p_win * math.log1p(b * fraction) + p_lose * math.log1p(-fraction) + refund_term


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
