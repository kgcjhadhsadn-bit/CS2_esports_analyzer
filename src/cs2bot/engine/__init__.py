"""cs2bot.engine - pricing, EV / Kelly with Polish tax, decision rules, bankroll and ledger.

Pipeline for one match (see docs/ARCHITECTURE.md, P8)::

    pairs = pricing.price_quotes(prediction, quotes, sigma_floor=settings.models.sigma_floor)
    priced = ev.price_all(pairs, settings)                       # effective odds, EV, full Kelly
    info = decision.load_match_decision_info(session, match_id=..., bookmaker_event_id=...,
                                             prediction=prediction)
    decisions = decision.evaluate_all(priced, match=info, settings=decision_settings,
                                      betclic=settings.betclic, now=now)
    state = bankroll.compute_state(session, settings, now=now)
    sized = bankroll.size_decision(d, state, bankroll_settings, settings.betclic)  # per accepted d

Modules: :mod:`.pricing`, :mod:`.ev`, :mod:`.decision`, :mod:`.bankroll`, :mod:`.ledger`. Import the
submodules directly (nothing is re-exported here, to keep imports cheap and cycle-free).
"""
