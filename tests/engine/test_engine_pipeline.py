"""End-to-end through the whole engine: price -> EV -> decide -> size -> ledger -> settle -> state."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import Bet
from cs2bot.domain.enums import BetStatus, LinkStatus, MatchFormat
from cs2bot.domain.markets import SELECTION_OVER, SELECTION_TEAM1, SELECTION_TEAM2, MarketSpec, MarketType
from cs2bot.engine import bankroll, decision, ev, ledger, pricing
from cs2bot.testing.synthetic import synthetic_two_way_odds
from tests.engine.engine_factories import T0, add_event, add_match, make_outcome, make_prediction, make_quote

NOW = T0 + timedelta(minutes=1)


def test_full_engine_pipeline(session: Session, settings: Settings) -> None:
    match = add_match(session, scheduled_at=T0 + timedelta(hours=4))
    match.data_completeness = 1.0
    event = add_event(session, match.id, confidence=0.97, status=LinkStatus.AUTO)
    session.flush()
    # strong favourite: maps q = .75 .70 .65 -> P(team1) = 0.525 + 0.4 * 0.65 = 0.785, sigma ~ 0.025
    prediction = make_prediction(MatchFormat.BO3, [0.75, 0.70, 0.65], shift=0.02, match_id=match.id)
    winner = MarketSpec(market_type=MarketType.MATCH_WINNER)
    quotes = [
        make_quote(winner, SELECTION_TEAM1, odds="1.55", captured_at=T0),
        make_quote(winner, SELECTION_TEAM2, odds="2.60", captured_at=T0),
        make_quote(
            MarketSpec(market_type=MarketType.MAP_WINNER, map_number=3), SELECTION_TEAM1, captured_at=T0
        ),
        make_quote(
            MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=999, map_number=1, line=15.5),
            SELECTION_OVER,
            captured_at=T0,
        ),
    ]

    pairs = pricing.price_quotes(prediction, quotes, sigma_floor=settings.models.sigma_floor)
    assert len(pairs) == 3  # the unknown player is skipped
    priced = ev.price_all(pairs, settings)
    fav = priced[0]
    assert fav.outcome.p_win == pytest.approx(0.785)
    assert fav.ev == pytest.approx(0.785 * (0.88 * 1.55 - 1) - 0.215)

    info = decision.load_match_decision_info(
        session, match_id=match.id, bookmaker_event_id=event.id, prediction=prediction, now=NOW
    )
    decisions = decision.evaluate_all(
        priced,
        match=info,
        settings=db_state.effective_decision_settings(session, settings),
        betclic=settings.betclic,
        now=NOW,
    )
    assert [d.accepted for d in decisions] == [True, False, False]
    assert {decision.reason_code(r) for r in decisions[1].reasons} >= {"low_probability", "low_ev"}

    state = bankroll.compute_state(session, settings, now=NOW)
    bankroll_settings = db_state.effective_bankroll_settings(session, settings)
    sized = bankroll.size_decision(decisions[0], state, bankroll_settings, settings.betclic)
    assert sized.accepted and sized.stake == Decimal("20.00")  # 2% cap of the 1000 PLN bankroll

    # the integration layer creates the bet row, places it, then records the stake
    quote = sized.priced.quote
    bet = Bet(
        match_id=match.id,
        selection_id=None,
        external_event_id=quote.external_event_id,
        market_raw=quote.market_raw_name,
        selection_raw=quote.selection_raw_name,
        spec_key=quote.spec.key(),
        selection_key=quote.selection_key,
        requested_odds=quote.odds,
        min_acceptable_odds=quote.odds,
        placed_odds=quote.odds,
        stake=sized.stake,
        status=BetStatus.PLACED,
        dry_run=False,
        idempotency_key=f"{match.id}|{quote.spec.key()}#{quote.selection_key}",
        placed_at=NOW,
        **ledger.bet_money_fields(sized.stake, quote.odds, settings),  # type: ignore[arg-type]
    )
    session.add(bet)
    session.flush()
    ledger.record_stake(session, bet)
    assert bet.tax == Decimal("2.40") and bet.potential_payout == Decimal("27.28")

    state = bankroll.compute_state(session, settings, now=NOW + timedelta(minutes=1))
    assert state.exposure_by_match == {match.id: Decimal("20.00")}
    assert state.balance == Decimal("980.00") and state.bankroll_reference == Decimal("1000.00")
    again = decision.evaluate(
        fav,
        match=decision.load_match_decision_info(
            session, match_id=match.id, bookmaker_event_id=event.id, prediction=prediction, now=NOW
        ),
        settings=settings.decision,
        betclic=settings.betclic,
        now=NOW,
    )
    assert [decision.reason_code(r) for r in again.reasons] == ["duplicate"]

    # settlement after the match
    settled_at = T0 + timedelta(hours=7)
    payout, pnl = ledger.settle_amounts(bet, "won", settings)
    bet.status, bet.payout, bet.pnl, bet.settled_at = BetStatus.WON, payout, pnl, settled_at
    ledger.record_payout(session, bet, payout)
    session.flush()
    assert (payout, pnl) == (Decimal("27.28"), Decimal("7.28"))
    final = bankroll.compute_state(session, settings, now=settled_at + timedelta(minutes=5))
    assert final.balance == final.bankroll_reference == Decimal("1007.28")
    assert final.realized_pnl_today == Decimal("7.28") and final.exposure_by_match == {}
    assert bankroll.check_loss_limits(session, final, settings, now=settled_at) is False
    reconciled = ledger.reconcile(
        session, Decimal("1007.28"), tolerance=settings.executor.reconcile_tolerance
    )
    assert not reconciled.flagged and reconciled.entry_id is None


@pytest.mark.parametrize("p_true", [0.55, 0.65, 0.75, 0.82, 0.9])
def test_margin_and_tax_kill_the_edge_when_we_agree_with_the_bookmaker(
    p_true: float, settings: Settings
) -> None:
    """A 7% margin bookmaker whose belief equals our probability never offers +EV after the 12% tax."""
    o1, o2 = synthetic_two_way_odds(p_true, margin=0.07, noise_sd=0.0, rng=np.random.default_rng(0))
    for p, odds in ((p_true, o1), (1.0 - p_true, o2)):
        outcome = make_outcome(p_win=p, sigma=0.01)
        assert ev.expected_value(outcome, Decimal(str(odds)), settings) < 0
        assert ev.kelly_fraction(outcome, Decimal(str(odds)), settings) <= 0
