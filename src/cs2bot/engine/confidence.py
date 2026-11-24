"""Conservative probability adjustment applied before the decision rules (final review, Lens C).

The owner bets on confidence alone (PLAN.md section 0: ``p(win | settled) >= 0.75``, odds ignored, the
stake chosen by confidence tier). A confident but wrong probability is then the costliest error, and
the calibrated holdout probabilities above 0.75 were measured to be a few points over-confident
(docs/dev/reports/deep_data.md). :mod:`cs2bot.models.confidence` therefore fits a per-market
:class:`~cs2bot.models.confidence.ConfidenceGuard` from resolved backtest predictions whose
``adjust(market_type, p)`` shrinks probabilities above 0.75 where the history is thin or
over-confident, never raising one.

This module applies such an adjuster to the outcomes BEFORE they are priced, so the confidence check,
the stake tier, the EV shown and the written decision all use the same conservative number:

* ``p(win | settled)`` is replaced by ``min(p, adjuster(market_type, p))``; the refund probabilities
  (push / void) are kept, so ``p_win = p_adj x P(settled)`` and the lost mass moves to ``p_lose``;
* an adjuster that fails or returns a non-finite number yields ``p_win = NaN`` - the decision then
  rejects the outcome (``invalid_numbers``); a broken guard can block bets, never enlarge them.

The engine stays free of I/O: the caller (``services.betting``) loads the guard and passes
``guard.adjust``. The backtest does not apply it (the guard is fitted on the backtest's own
predictions, so applying it there would be look-ahead).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable

from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.utils.logging import get_logger

logger = get_logger(__name__)

#: ``(market_type value, p(win | settled)) -> adjusted p(win | settled)``
ProbabilityAdjuster = Callable[[str, float], float]

#: Adjustments smaller than this are treated as "unchanged" (no note, no copy).
_EPS = 1e-12


def guarded_outcome(
    outcome: OutcomeProbability, market_type: str, adjuster: ProbabilityAdjuster
) -> OutcomeProbability:
    """``outcome`` with its ``p(win | settled)`` replaced by the (never larger) adjusted value."""
    settle_mass = 1.0 - outcome.p_void - outcome.p_push
    if not settle_mass > 1e-12:
        return outcome  # never settles: the decision rejects it on its own
    p = outcome.p_win_given_settled
    if not math.isfinite(p):
        return outcome
    try:
        adjusted = float(adjuster(market_type, p))
    except Exception as exc:  # a broken guard must block the bet, never let it through unchecked
        logger.warning(
            "confidence guard failed for %s (%s): outcome blocked", market_type, type(exc).__name__
        )
        adjusted = math.nan
    if not math.isfinite(adjusted):
        return outcome.model_copy(update={"p_win": math.nan})
    adjusted = min(p, max(0.0, adjusted))
    if p - adjusted <= _EPS:
        return outcome
    return outcome.model_copy(update={"p_win": adjusted * settle_mass})


def guard_pairs(
    pairs: Iterable[tuple[OutcomeProbability, OddsQuote]], adjuster: ProbabilityAdjuster
) -> tuple[list[tuple[OutcomeProbability, OddsQuote]], dict[tuple[str, str], tuple[float, float]]]:
    """Apply :func:`guarded_outcome` to every ``(outcome, quote)`` pair of ``pricing.price_quotes``.

    Returns the adjusted pairs and, for every outcome that changed, ``(spec_key, selection) ->
    (model p(win | settled), adjusted p(win | settled))`` for notes and audit.
    """
    out: list[tuple[OutcomeProbability, OddsQuote]] = []
    changed: dict[tuple[str, str], tuple[float, float]] = {}
    for outcome, quote in pairs:
        guarded = guarded_outcome(outcome, quote.spec.market_type.value, adjuster)
        if guarded is not outcome:
            changed[(outcome.spec_key, outcome.selection)] = (
                outcome.p_win_given_settled,
                guarded.p_win_given_settled if math.isfinite(guarded.p_win) else math.nan,
            )
        out.append((guarded, quote))
    return out, changed


def guard_note(model_p: float, adjusted_p: float) -> str:
    """Human-readable note for a decision whose probability the guard lowered."""
    return (
        f"confidence guard: p(win | settled) {model_p:.3f} -> {adjusted_p:.3f} "
        "(conservative high-confidence calibration)"
    )
