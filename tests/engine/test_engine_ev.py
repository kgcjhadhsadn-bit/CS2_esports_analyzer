"""Tax model, EV and Kelly: hand-computed examples, the PIT threshold and hypothesis properties."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from scipy.optimize import minimize_scalar

from cs2bot.config import BetclicSettings, Settings
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.engine.ev import (
    TaxModel,
    big_win_stake_limit,
    effective_odds,
    expected_log_growth,
    expected_value,
    kelly_for_net_odds,
    kelly_fraction,
    net_payout,
    payout_breakdown,
    price,
    price_all,
    stake_tax,
    winnings_tax,
)
from tests.engine.engine_factories import make_outcome, make_quote

TAXED = TaxModel(Decimal("0.12"), Decimal("2280"), Decimal("0.10"))
PROMO = TaxModel(Decimal("0"), Decimal("2280"), Decimal("0.10"))
NO_PIT = TaxModel(Decimal("0.12"), Decimal("2280"), Decimal("0"))


def outcome(p_win: float, p_void: float = 0.0, p_push: float = 0.0) -> OutcomeProbability:
    return OutcomeProbability(
        spec_key="x", selection="team1", p_win=p_win, p_void=p_void, p_push=p_push, sigma=0.01
    )


# ------------------------------------------------------------------ tax model


def test_tax_model_from_settings() -> None:
    s = Settings()
    assert TaxModel.from_settings(s) == TAXED
    promo = Settings(betclic=BetclicSettings(tax_free_promo=True))
    assert TaxModel.from_settings(promo).stake_tax_rate == 0
    assert TaxModel.from_settings(BetclicSettings(tax_free_promo=True)) == PROMO
    assert TaxModel.from_settings(BetclicSettings()) == TAXED
    assert TaxModel.from_settings(TAXED) is TAXED
    with pytest.raises(ValueError, match="stake_tax_rate"):
        TaxModel(Decimal("1.2"), Decimal("2280"), Decimal("0.1"))
    with pytest.raises(ValueError, match="big_win_threshold"):
        TaxModel(Decimal("0.12"), Decimal("-1"), Decimal("0.1"))


def test_effective_odds() -> None:
    assert effective_odds(Decimal("2.00"), tax_rate=Decimal("0.12")) == Decimal("1.76")
    assert effective_odds(Decimal("1.85"), tax_rate=Decimal("0")) == Decimal("1.85")
    with pytest.raises(ValueError):
        effective_odds(Decimal("2.0"), tax_rate=Decimal("1"))


@pytest.mark.parametrize(
    ("stake", "odds", "model", "expected"),
    [
        ("100", "2.00", TAXED, "176.00"),  # 88 * 2
        ("100", "2.00", PROMO, "200.00"),  # "bez podatku" promo: factor 1.00
        ("7.30", "1.85", TAXED, "11.88"),  # tax 0.88 -> 6.42 * 1.85 = 11.877
        ("10", "1.01", TAXED, "8.89"),  # odds this low lose money even when they win
        ("1000", "3.00", TAXED, "2376.00"),  # 880 * 3 = 2640 > 2280 -> 264 PIT withheld
        ("1000", "3.00", NO_PIT, "2640.00"),
        ("1140.00", "2.00", PROMO, "2280.00"),  # exactly at the threshold: exempt
        ("1140.01", "2.00", PROMO, "2052.02"),  # 2280.02 -> 10% of 2280 (whole winning) = 228
    ],
)
def test_net_payout(stake: str, odds: str, model: TaxModel, expected: str) -> None:
    assert net_payout(Decimal(stake), Decimal(odds), model) == Decimal(expected)


def test_net_payout_accepts_full_settings() -> None:
    assert net_payout(Decimal("100"), Decimal("2.00"), Settings()) == Decimal("176.00")


def test_payout_breakdown_big_win() -> None:
    b = payout_breakdown(Decimal("1000"), Decimal("3.00"), TAXED)
    assert b.stake_tax == Decimal("120.00") and b.net_stake == Decimal("880.00")
    assert b.gross_win == Decimal("2640.00") and b.win_tax == Decimal("264.00")
    assert b.net_payout == Decimal("2376.00") and b.profit == Decimal("1376.00")
    assert b.stake_tax_rate == Decimal("0.12")
    # the stake tax recorded on a bet overrides the settings (e.g. a bet placed during a promo)
    promo_bet = payout_breakdown(Decimal("100"), Decimal("2.00"), TAXED, tax=Decimal("0"))
    assert promo_bet.net_payout == Decimal("200.00")
    with pytest.raises(ValueError, match="odds"):
        payout_breakdown(Decimal("10"), Decimal("0.9"), TAXED)
    with pytest.raises(ValueError, match="stake tax"):
        payout_breakdown(Decimal("10"), Decimal("2"), TAXED, tax=Decimal("11"))
    with pytest.raises(ValueError, match="stake"):
        payout_breakdown(Decimal("-1"), Decimal("2"), TAXED)


def test_winnings_tax_rounds_to_full_zloty() -> None:
    assert winnings_tax(Decimal("2280.00"), TAXED) == Decimal("0.00")
    assert winnings_tax(Decimal("2280.01"), TAXED) == Decimal("228.00")
    assert winnings_tax(Decimal("2345.67"), TAXED) == Decimal("235.00")  # base 2346 -> 234.6 -> 235
    assert winnings_tax(Decimal("2344.49"), TAXED) == Decimal("234.00")  # base 2344 -> 234.4 -> 234
    assert winnings_tax(Decimal("9999.99"), NO_PIT) == Decimal("0.00")


def test_stake_tax() -> None:
    assert stake_tax(Decimal("10"), TAXED) == Decimal("1.20")
    assert stake_tax(Decimal("7.30"), TAXED) == Decimal("0.88")
    assert stake_tax(Decimal("7.30"), PROMO) == Decimal("0.00")


def test_big_win_stake_limit() -> None:
    limit = big_win_stake_limit(Decimal("3.00"), TAXED)
    assert limit == Decimal("863.64")  # 863.64 - 103.64 = 760.00 * 3 = 2280.00
    assert payout_breakdown(limit, Decimal("3.00"), TAXED).win_tax == 0
    assert payout_breakdown(limit + Decimal("0.01"), Decimal("3.00"), TAXED).win_tax > 0
    assert big_win_stake_limit(Decimal("2.00"), PROMO) == Decimal("1140.00")
    assert big_win_stake_limit(Decimal("2.00"), NO_PIT) is None
    for odds in ("1.01", "1.37", "2.5", "4.444", "14.99"):
        lim = big_win_stake_limit(Decimal(odds), TAXED)
        assert lim is not None
        assert payout_breakdown(lim, Decimal(odds), TAXED).gross_win <= Decimal("2280")
        assert payout_breakdown(lim + Decimal("0.01"), Decimal(odds), TAXED).gross_win > Decimal("2280")


# ------------------------------------------------------------------ expected value


def test_expected_value_examples() -> None:
    # p = 0.8 at 1.60: o_eff = 1.408 -> EV = 0.8 * 0.408 - 0.2 = 0.1264
    assert expected_value(outcome(0.8), Decimal("1.60"), TAXED) == pytest.approx(0.1264)
    # a quarter of the mass refunded: 0.6 * 0.408 - 0.15 = 0.0948 (refunds are worth 0)
    assert expected_value(outcome(0.6, p_void=0.25), Decimal("1.60"), TAXED) == pytest.approx(0.0948)
    assert expected_value(outcome(0.6, p_push=0.25), Decimal("1.60"), TAXED) == pytest.approx(0.0948)
    # tax-free promo: 0.8 * 0.6 - 0.2 = 0.28
    assert expected_value(outcome(0.8), Decimal("1.60"), PROMO) == pytest.approx(0.28)
    # break-even under tax needs p * 0.88 * odds = 1
    assert expected_value(outcome(0.5), Decimal(str(1 / 0.44)), TAXED) == pytest.approx(0.0, abs=1e-12)


def test_expected_value_with_stake_applies_the_winnings_tax() -> None:
    o = outcome(0.5)
    # no stake / small stake: 0.5 * (2.64 - 1) - 0.5 = 0.32
    assert expected_value(o, Decimal("3.00"), TAXED) == pytest.approx(0.32)
    assert expected_value(o, Decimal("3.00"), TAXED, stake=Decimal("10")) == pytest.approx(0.32)
    # 1000 PLN: payout 2376 -> 0.5 * 1.376 - 0.5 = 0.188
    assert expected_value(o, Decimal("3.00"), TAXED, stake=Decimal("1000")) == pytest.approx(0.188)
    with pytest.raises(ValueError):
        expected_value(o, Decimal("3.00"), TAXED, stake=Decimal("0"))


# ------------------------------------------------------------------ Kelly


def test_kelly_hand_example() -> None:
    # b = 0.408, f* = (0.408 * 0.8 - 0.2) / 0.408 = 0.309803...
    f = kelly_fraction(outcome(0.8), Decimal("1.60"), TAXED)
    assert f == pytest.approx(0.1264 / 0.408)
    # refunds: (0.408 * 0.6 - 0.15) / (0.408 * 0.75) is the same fraction
    assert kelly_fraction(outcome(0.6, p_void=0.25), Decimal("1.60"), TAXED) == pytest.approx(f)
    # without refunds the formula reduces to PLAN.md's (b p - (1 - p)) / b
    b = 0.408
    assert f == pytest.approx((b * 0.8 - 0.2) / b)


@pytest.mark.parametrize(
    ("p_win", "p_void", "odds", "model"),
    [
        (0.8, 0.0, "1.60", TAXED),
        (0.6, 0.25, "1.60", TAXED),
        (0.55, 0.1, "2.40", PROMO),
        (0.3, 0.5, "4.50", TAXED),
        (0.9, 0.05, "1.25", TAXED),
    ],
)
def test_kelly_maximises_log_growth_numerically(
    p_win: float, p_void: float, odds: str, model: TaxModel
) -> None:
    o = outcome(p_win, p_void=p_void)
    b = float(effective_odds(Decimal(odds), tax_rate=model.stake_tax_rate)) - 1.0
    res = minimize_scalar(
        lambda f: -(o.p_win * math.log1p(b * f) + o.p_lose * math.log1p(-f)),
        bounds=(0.0, 0.999),
        method="bounded",
        options={"xatol": 1e-10},
    )
    f_star = kelly_fraction(o, Decimal(odds), model)
    if f_star > 0:
        assert res.x == pytest.approx(f_star, abs=1e-6)
    else:
        assert res.x == pytest.approx(0.0, abs=1e-5)


def test_kelly_edge_cases() -> None:
    # odds so low that even a win returns less than the stake: b < 0
    assert kelly_fraction(outcome(0.99), Decimal("1.10"), TAXED) == -1.0
    assert expected_value(outcome(0.99), Decimal("1.10"), TAXED) < 0
    # no way to lose: bet everything (capped at 1)
    assert kelly_fraction(outcome(0.7, p_void=0.3), Decimal("1.50"), TAXED) == pytest.approx(1.0)
    assert kelly_for_net_odds(0.7, 0.0, 0.32) == 1.0
    # nothing can settle: no stake
    assert kelly_fraction(outcome(0.0, p_void=1.0), Decimal("2.00"), TAXED) == 0.0
    assert expected_value(outcome(0.0, p_void=1.0), Decimal("2.00"), TAXED) == 0.0
    assert kelly_for_net_odds(0.5, 0.5, 0.0) == -1.0
    assert kelly_for_net_odds(0.1, 0.9, 0.5) == -1.0  # strongly negative edge is clipped


def test_expected_log_growth() -> None:
    assert expected_log_growth(0.8, 0.2, 0.408, 0.0) == 0.0
    assert expected_log_growth(0.8, 0.2, 0.408, 1.0) == -math.inf
    assert expected_log_growth(1.0, 0.0, 0.5, 1.0) == pytest.approx(math.log1p(0.5))
    f = 0.1264 / 0.408
    g = expected_log_growth(0.8, 0.2, 0.408, f)
    assert g > expected_log_growth(0.8, 0.2, 0.408, f - 0.01)
    assert g > expected_log_growth(0.8, 0.2, 0.408, f + 0.01)


def test_price_builds_priced_outcome() -> None:
    o = make_outcome(p_win=0.8)
    q = make_quote(odds="1.60")
    priced = price(o, q, Settings())
    assert priced.effective_odds == Decimal("1.408")
    assert priced.ev == pytest.approx(0.1264)
    assert priced.kelly_full == pytest.approx(0.1264 / 0.408)
    assert priced.outcome is o and priced.quote is q
    assert [p.ev for p in price_all([(o, q)], Settings())] == [priced.ev]


# ------------------------------------------------------------------ properties

probabilities = st.tuples(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
odds_values = st.decimals(min_value=Decimal("1.01"), max_value=Decimal("25"), places=3)
models = st.sampled_from([TAXED, PROMO])


def _outcome_from(pw_frac: tuple[float, float]) -> OutcomeProbability:
    p_win, refund_share = pw_frac
    p_refund = (1.0 - p_win) * refund_share
    return outcome(p_win, p_void=p_refund)


@settings(max_examples=300, deadline=None)
@given(probabilities, odds_values, models)
def test_property_kelly_non_positive_iff_ev_non_positive(
    pw: tuple[float, float], odds: Decimal, model: TaxModel
) -> None:
    o = _outcome_from(pw)
    ev = expected_value(o, odds, model)
    assume(abs(ev) > 1e-12)
    f = kelly_fraction(o, odds, model)
    assert (f <= 0) == (ev <= 0)
    assert -1.0 <= f <= 1.0


@settings(max_examples=300, deadline=None)
@given(probabilities, odds_values, models, st.floats(min_value=0.01, max_value=1.0))
def test_property_refund_invariance(
    pw: tuple[float, float], odds: Decimal, model: TaxModel, lam: float
) -> None:
    """Moving probability mass into refunds proportionally keeps f* and scales the EV."""
    o = _outcome_from(pw)
    assume(o.p_win + o.p_lose > 1e-6)
    scaled = outcome(o.p_win * lam, p_void=1.0 - (o.p_win + o.p_lose) * lam)
    assert kelly_fraction(scaled, odds, model) == pytest.approx(kelly_fraction(o, odds, model), abs=1e-9)
    assert expected_value(scaled, odds, model) == pytest.approx(
        lam * expected_value(o, odds, model), abs=1e-9
    )


@settings(max_examples=300, deadline=None)
@given(probabilities, odds_values, odds_values, models)
def test_property_monotone_in_odds(pw: tuple[float, float], a: Decimal, b: Decimal, model: TaxModel) -> None:
    o = _outcome_from(pw)
    lo, hi = min(a, b), max(a, b)
    assert expected_value(o, lo, model) <= expected_value(o, hi, model) + 1e-12
    assert kelly_fraction(o, lo, model) <= kelly_fraction(o, hi, model) + 1e-12


@settings(max_examples=200, deadline=None)
@given(probabilities, odds_values, models)
def test_property_kelly_is_a_growth_maximum(pw: tuple[float, float], odds: Decimal, model: TaxModel) -> None:
    o = _outcome_from(pw)
    f = kelly_fraction(o, odds, model)
    assume(0.02 < f < 0.98)
    b = float(effective_odds(odds, tax_rate=model.stake_tax_rate)) - 1.0
    g = expected_log_growth(o.p_win, o.p_lose, b, f)
    assert g >= expected_log_growth(o.p_win, o.p_lose, b, f - 0.01) - 1e-12
    assert g >= expected_log_growth(o.p_win, o.p_lose, b, f + 0.01) - 1e-12
    assert g > 0


@settings(max_examples=200, deadline=None)
@given(
    probabilities,
    odds_values,
    st.decimals(min_value=Decimal("10"), max_value=Decimal("20000"), places=2),
)
def test_property_winnings_tax_never_helps(pw: tuple[float, float], odds: Decimal, stake: Decimal) -> None:
    o = _outcome_from(pw)
    with_stake = expected_value(o, odds, TAXED, stake=stake)
    # grosz rounding of the stake tax (<= 0.005 x odds) and of the winning (<= 0.005), per unit staked
    rounding = o.p_win * (0.005 * float(odds) + 0.005) / float(stake)
    assert with_stake <= expected_value(o, odds, TAXED) + rounding + 1e-12
    payout = net_payout(stake, odds, TAXED)
    gross = payout_breakdown(stake, odds, TAXED).gross_win
    assert payout == gross or (gross > Decimal("2280") and payout < gross)
