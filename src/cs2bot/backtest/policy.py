"""Injectable pricing / decision / staking / settlement steps of the backtest.

The defaults wrap the real engine, so a backtest measures exactly what the live bot would do:

* :class:`EnginePricer` - :func:`cs2bot.engine.pricing.price_outcome` for a ``MatchPrediction``
  (every market type, void / push aware), a lookup for a :class:`ProbabilityTable`;
* :class:`EngineDecider` - :func:`cs2bot.engine.decision.evaluate` (every PLAN.md section 6 rule),
  with the match-level facts a backtest knows (decision time, link assumed confirmed, the
  configured data completeness, veto known or not);
* :class:`EngineStaker` - :func:`cs2bot.engine.bankroll.size_stake` (1/4 Kelly, caps, exposure, loss
  guards, bets per day) on the simulated bankroll;
* :class:`EngineResolver` - :func:`cs2bot.engine.settlement.resolve_outcome`.

Simple alternatives for experiments and tests: :class:`ThresholdDecider` (p / EV thresholds only)
and :class:`FlatStaker` (constant stake).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from cs2bot.backtest.types import MatchQuery, ProbabilityTable, outcome_key
from cs2bot.config import BankrollSettings, BetclicSettings, DecisionSettings
from cs2bot.domain.betting import BankrollState, BetDecision, PricedOutcome
from cs2bot.domain.enums import LinkStatus
from cs2bot.domain.markets import SELECTION_TEAM1, SELECTION_TEAM2, MarketSpec, MarketType
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import MapWinEstimate, MatchPrediction, OutcomeProbability
from cs2bot.domain.results import MatchActuals, SettlementOutcome
from cs2bot.engine import bankroll as engine_bankroll
from cs2bot.engine import decision as engine_decision
from cs2bot.engine.pricing import UnpriceableMarket, price_outcome
from cs2bot.engine.settlement import resolve_outcome
from cs2bot.utils.money import D

__all__ = [
    "Decider",
    "DecisionContext",
    "EngineDecider",
    "EnginePricer",
    "EngineResolver",
    "EngineStaker",
    "FlatStaker",
    "Pricer",
    "Resolver",
    "Staker",
    "ThresholdDecider",
    "UnpriceableMarket",
]


@dataclass(frozen=True)
class DecisionContext:
    """Match-level facts the decision step needs (what ``MatchDecisionInfo`` holds live)."""

    match: MatchQuery
    now: datetime
    veto_known: bool
    data_completeness: float
    already_bet_keys: frozenset[str] = field(default_factory=frozenset)


Pricer = Callable[[Any, OddsQuote], OutcomeProbability]
Decider = Callable[[PricedOutcome, DecisionContext], BetDecision]
Staker = Callable[[BetDecision, BankrollState], Decimal]
Resolver = Callable[[MarketSpec, str, MatchActuals], SettlementOutcome]


class EnginePricer:
    """Price a quote from the prediction object the backtest's predictor returned.

    * :class:`MatchPrediction` -> ``engine.pricing.price_outcome`` (sigma from the low/high scenarios);
    * :class:`ProbabilityTable` or a ``Mapping[outcome_key, OutcomeProbability]`` -> lookup;
    * :class:`MapWinEstimate` (a single-map model) -> ``MAP_WINNER`` of map 1 and, read as a BO1,
      ``MATCH_WINNER`` (team1 / team2 only; use ``series_match_prediction`` for a series);
    * anything else, or an outcome the prediction cannot support -> :class:`UnpriceableMarket`.
    """

    def __init__(self, *, sigma_floor: float = 0.01, betclic: BetclicSettings | None = None) -> None:
        self.sigma_floor = sigma_floor
        self.betclic = betclic

    def __call__(self, prediction: Any, quote: OddsQuote) -> OutcomeProbability:
        if isinstance(prediction, MatchPrediction):
            return price_outcome(
                quote.spec,
                quote.selection_key,
                prediction,
                sigma_floor=self.sigma_floor,
                betclic=self.betclic,
            )
        found: OutcomeProbability | None = None
        if isinstance(prediction, MapWinEstimate):
            found = self._from_estimate(prediction, quote)
        elif isinstance(prediction, ProbabilityTable):
            found = prediction.get(quote.spec, quote.selection_key)
        elif isinstance(prediction, Mapping):
            value = prediction.get(outcome_key(quote.spec, quote.selection_key))
            found = value if isinstance(value, OutcomeProbability) else None
        else:
            raise UnpriceableMarket(f"cannot price from a {type(prediction).__name__}")
        if found is None:
            raise UnpriceableMarket(f"no probability for {outcome_key(quote.spec, quote.selection_key)}")
        return found.model_copy(update={"sigma": max(found.sigma, self.sigma_floor)})

    @staticmethod
    def _from_estimate(est: MapWinEstimate, quote: OddsQuote) -> OutcomeProbability | None:
        spec = quote.spec
        single_map = spec.market_type is MarketType.MATCH_WINNER or (
            spec.market_type is MarketType.MAP_WINNER and spec.map_number == 1
        )
        if not single_map or quote.selection_key not in (SELECTION_TEAM1, SELECTION_TEAM2):
            return None
        p = est.p_team1_win if quote.selection_key == SELECTION_TEAM1 else 1.0 - est.p_team1_win
        return OutcomeProbability(
            spec_key=spec.key(), selection=quote.selection_key, p_win=p, sigma=est.sigma
        )


class EngineDecider:
    """``engine.decision.evaluate`` with the facts a backtest knows about the match.

    The event link is assumed ``CONFIRMED`` with confidence 1 and the match ``SCHEDULED`` (a
    backtest only replays linked events), the data completeness is the configured value and the
    quote age limit is ``max_quote_age_s`` (historical quotes are the price on offer at the
    decision time, even when the price was last *changed* long before).
    """

    def __init__(
        self,
        settings: DecisionSettings,
        betclic: BetclicSettings,
        *,
        max_quote_age_s: float | None = None,
    ) -> None:
        self.settings = settings
        self.betclic = (
            betclic
            if max_quote_age_s is None
            else betclic.model_copy(update={"odds_max_age_s": int(max(max_quote_age_s, 1))})
        )

    def __call__(self, priced: PricedOutcome, ctx: DecisionContext) -> BetDecision:
        info = engine_decision.MatchDecisionInfo(
            match_id=ctx.match.match_id,
            scheduled_at=ctx.match.start_time,
            match_status=None,
            data_completeness=ctx.data_completeness,
            link_confidence=1.0,
            link_status=LinkStatus.CONFIRMED,
            veto_known=ctx.veto_known,
            context_veto=False,
            already_bet_keys=set(ctx.already_bet_keys),
        )
        return engine_decision.evaluate(
            priced, match=info, settings=self.settings, betclic=self.betclic, now=ctx.now
        )


class ThresholdDecider:
    """Accept when ``p(win | settled) >= min_probability`` and ``EV >= min_ev`` (nothing else)."""

    def __init__(self, *, min_probability: float = 0.0, min_ev: float = 0.0) -> None:
        self.min_probability = min_probability
        self.min_ev = min_ev

    def __call__(self, priced: PricedOutcome, ctx: DecisionContext) -> BetDecision:
        reasons: list[str] = []
        p = priced.outcome.p_win_given_settled
        if not p >= self.min_probability:
            reasons.append(f"low_probability: p(win | settled) {p:.3f} < {self.min_probability:.3f}")
        if not priced.ev >= self.min_ev:
            reasons.append(f"low_ev: EV {priced.ev:+.4f} < {self.min_ev:.4f}")
        key = engine_decision.bet_key(ctx.match.match_id, priced.quote.spec.key(), priced.quote.selection_key)
        if key in ctx.already_bet_keys:
            reasons.append("duplicate: this outcome was already bet on this match")
        return BetDecision(
            priced=priced,
            match_id=ctx.match.match_id,
            accepted=not reasons,
            reasons=reasons,
            decided_at=ctx.now,
        )


class EngineStaker:
    """``engine.bankroll.size_stake`` on the simulated bankroll state."""

    def __init__(self, settings: BankrollSettings, betclic: BetclicSettings) -> None:
        self.settings = settings
        self.betclic = betclic

    def __call__(self, decision: BetDecision, state: BankrollState) -> Decimal:
        stake, _ = engine_bankroll.size_stake(decision, state, self.settings, self.betclic)
        return stake


class FlatStaker:
    """A constant stake for every accepted bet (capped by the free balance)."""

    def __init__(self, stake: Decimal | float | str = Decimal("10")) -> None:
        self.stake = D(stake)
        if self.stake <= 0:
            raise ValueError("stake must be positive")

    def __call__(self, decision: BetDecision, state: BankrollState) -> Decimal:
        if not decision.accepted:
            return Decimal("0")
        return min(self.stake, max(state.balance, Decimal("0")))


class EngineResolver:
    """``engine.settlement.resolve_outcome`` with the Betclic settlement rules of ``betclic``."""

    def __init__(self, betclic: BetclicSettings | None = None) -> None:
        self.betclic = betclic

    def __call__(self, spec: MarketSpec, selection: str, actuals: MatchActuals) -> SettlementOutcome:
        return resolve_outcome(spec, selection, actuals, betclic=self.betclic)
