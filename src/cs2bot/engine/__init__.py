"""cs2bot.engine - pricing, EV / Kelly with Polish tax, decision rules, bankroll, ledger, settlement.

Pipeline for one match (see docs/ARCHITECTURE.md, P8)::

    pairs = pricing.price_quotes(prediction, quotes, sigma_floor=settings.models.sigma_floor)
    priced = ev.price_all(pairs, settings)                       # effective odds, EV, full Kelly
    info = decision.load_match_decision_info(session, match_id=..., bookmaker_event_id=...,
                                             prediction=prediction)
    decisions = decision.evaluate_all(priced, match=info, settings=decision_settings,
                                      betclic=settings.betclic, now=now)
    state = bankroll.compute_state(session, settings, now=now)
    sized, _ = bankroll.size_decisions(decisions, state, bankroll_settings, settings.betclic)

    # for every sized decision with a stake: create the PENDING ``bets`` row
    # (min_acceptable_odds = decision.min_acceptable_odds(...)), commit, check
    # db.state.is_trading_halted, call the executor, then
    ledger.apply_placement(session, bet, placement_result, settings)   # status + STAKE entry

    # after the match
    outcome = settlement.resolve_bet(bet, actuals).outcome             # UNKNOWN: ask the bookmaker
    ledger.settle_bet(session, bet, outcome, settings)                 # payout / refund, pnl
    bankroll.check_loss_limits(session, bankroll.compute_state(session, settings, now=now), settings)

Modules: :mod:`.pricing`, :mod:`.ev`, :mod:`.decision`, :mod:`.bankroll`, :mod:`.ledger`,
:mod:`.settlement`. Import the submodules directly (nothing is re-exported here, to keep imports
cheap and cycle-free).
"""
