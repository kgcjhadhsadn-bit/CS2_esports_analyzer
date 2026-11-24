"""Walk-forward backtest engine.

For every historical match, in kick-off order:

1. the decision time is ``t = kick-off - decision_lead``;
2. bets of earlier matches whose result was known by ``t`` are settled (bankroll updated);
3. if no model exists yet, or ``retrain_every`` has passed since the last training, the trainer is
   called with :meth:`HistoricalData.until(t) <cs2bot.backtest.types.HistoricalData.until>` - only
   records visible strictly before ``t`` (asserted). Otherwise, a predictor with an ``observe``
   method is shown everything visible at ``t`` (online updates between retrains);
4. the predictor predicts the match from a :class:`~cs2bot.backtest.types.MatchQuery` (no result);
   its ``trained_through`` and the prediction's own time stamp must be before ``t``
   (:class:`~cs2bot.backtest.types.LeakageError` otherwise);
5. every quote on offer at ``t`` (``OddsHistory.quotes_before``) is priced, run through the
   decision step and - if accepted - sized against the simulated bankroll;
6. every priced outcome is settled against the actual result (for the probability metrics) and
   the placed bets are paid out when the match result is known.

Money is ``Decimal`` throughout; payouts use the Polish tax rules of ``cs2bot.engine.ev`` (12% stake
tax, 10% tax on single winnings above 2,280 PLN, refunds on void / push).
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cs2bot.backtest.odds_history import OddsHistory
from cs2bot.backtest.policy import (
    Decider,
    DecisionContext,
    EngineDecider,
    EnginePricer,
    EngineResolver,
    EngineStaker,
    Pricer,
    Resolver,
    Staker,
    UnpriceableMarket,
)
from cs2bot.backtest.report import (
    BacktestReport,
    BetRecord,
    CurvePoint,
    PredictionRecord,
    breakdown,
    max_drawdown,
)
from cs2bot.backtest.types import (
    BacktestMatch,
    HistoricalData,
    LeakageError,
    MatchQuery,
    Predictor,
    Trainer,
    outcome_key,
)
from cs2bot.config import Settings, get_settings
from cs2bot.domain.betting import BankrollState
from cs2bot.domain.results import SettlementOutcome
from cs2bot.engine import decision as engine_decision
from cs2bot.engine import ev as engine_ev
from cs2bot.models.calibration import brier, calibration_report, log_loss
from cs2bot.models.confidence import ConfidenceGuard, high_confidence_report
from cs2bot.models.ensemble import LookAheadError
from cs2bot.utils.money import D, quantize_money
from cs2bot.utils.time import ensure_utc, local_day_start, local_week_start, to_local, utcnow

logger = logging.getLogger(__name__)

__all__ = ["BacktestConfig", "SimulatedBankroll", "run_backtest"]

_ZERO = Decimal("0")
#: Look-ahead errors always propagate; every other failure of an injected step is contained.
_LEAKAGE = (LeakageError, LookAheadError)


class BacktestConfig(BaseModel):
    """Knobs of the walk-forward engine (betting rules come from the ``Settings``)."""

    model_config = ConfigDict(frozen=True)

    decision_lead: timedelta = timedelta(hours=1)  # decide this long before kick-off
    retrain_every: timedelta = timedelta(days=7)
    max_quote_age: timedelta = timedelta(hours=48)  # older prices are not considered "on offer"
    veto_known_at_decision: bool = True  # keep the published veto in the context at decision time
    data_completeness: float = 1.0  # passed to the decision rules (the backtest has no live gate)
    start: datetime | None = None  # predict matches kicking off in [start, end)
    end: datetime | None = None
    min_training_maps: int = 0  # skip matches until the training set has this many maps
    initial_bankroll: Decimal | None = None  # default settings.bankroll.starting_bankroll
    n_calibration_bins: int = 10
    keep_unplaced_decisions: bool = True  # list accepted-but-unstaked decisions in report.bets
    max_report_bets: int | None = None  # keep only the latest N bets in the report (None = all)
    extra: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _OpenBet:
    record: BetRecord
    settle_at: datetime
    resolution: SettlementOutcome


@dataclass
class SimulatedBankroll:
    """Cash, open bets and realized P&L, turned into the engine's :class:`BankrollState`."""

    initial: Decimal
    tax: engine_ev.TaxModel
    timezone: str = "Europe/Warsaw"
    cash: Decimal = _ZERO
    open: list[_OpenBet] = field(default_factory=list)
    realized: list[tuple[datetime, Decimal]] = field(default_factory=list)  # (settled at, pnl)
    placed_times: list[datetime] = field(default_factory=list)
    curve: list[CurvePoint] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.initial

    @property
    def open_stakes(self) -> Decimal:
        return sum((b.record.stake for b in self.open), _ZERO)

    @property
    def total(self) -> Decimal:
        return self.cash + self.open_stakes

    def state(self, now: datetime) -> BankrollState:
        day = local_day_start(now, self.timezone)
        week = local_week_start(now, self.timezone)
        pnl_today = sum((p for t, p in self.realized if day <= t <= now), _ZERO)
        pnl_week = sum((p for t, p in self.realized if week <= t <= now), _ZERO)
        reference = self.total
        exposure: dict[int, Decimal] = {}
        for b in self.open:
            exposure[b.record.match_id] = exposure.get(b.record.match_id, _ZERO) + b.record.stake
        return BankrollState(
            balance=self.cash,
            bankroll_reference=reference,
            day_start_bankroll=reference - pnl_today,
            week_start_bankroll=reference - pnl_week,
            realized_pnl_today=pnl_today,
            realized_pnl_week=pnl_week,
            open_stakes_today=sum((b.record.stake for b in self.open if b.record.decided_at >= day), _ZERO),
            open_stakes_week=sum((b.record.stake for b in self.open if b.record.decided_at >= week), _ZERO),
            exposure_by_match=exposure,
            day_start=day,
            week_start=week,
            bets_placed_today=sum(1 for t in self.placed_times if t >= day),
        )

    def place(self, bet: BetRecord, settle_at: datetime, resolution: SettlementOutcome) -> None:
        self.cash -= bet.stake
        self.open.append(_OpenBet(bet, settle_at, resolution))
        self.placed_times.append(bet.decided_at)

    def settle_until(self, t: datetime | None) -> None:
        """Settle every open bet whose result was known at ``t`` (all of them for ``None``)."""
        due = sorted(
            (b for b in self.open if t is None or b.settle_at <= t),
            key=lambda b: (b.settle_at, b.record.match_id),
        )
        for b in due:
            self.open.remove(b)
            payout = self._payout(b.record, b.resolution)
            b.record.outcome = b.resolution.value
            b.record.payout = payout
            b.record.pnl = payout - b.record.stake
            b.record.settled_at = b.settle_at
            self.cash += payout
            self.realized.append((b.settle_at, b.record.pnl))
            self.curve.append(
                CurvePoint(time=b.settle_at, bankroll=self.total, profit=self.total - self.initial)
            )

    def _payout(self, bet: BetRecord, outcome: SettlementOutcome) -> Decimal:
        if outcome is SettlementOutcome.WON:
            return engine_ev.net_payout(bet.stake, bet.odds, self.tax)
        if outcome is SettlementOutcome.LOST:
            return _ZERO
        if outcome.is_refund:
            return engine_ev.refund_amount(bet.stake, self.tax)
        return bet.stake  # UNKNOWN: a backtest cannot ask the bookmaker; treated as a full refund


def _prediction_time(prediction: Any) -> datetime | None:
    for attr in ("as_of", "created_at"):
        value = getattr(prediction, attr, None)
        if isinstance(value, datetime):
            return ensure_utc(value)
    return None


def _flat_pnl(odds: Decimal, outcome: SettlementOutcome, tax: engine_ev.TaxModel) -> float:
    if outcome is SettlementOutcome.WON:
        return (1.0 - float(tax.stake_tax_rate)) * float(odds) - 1.0
    if outcome is SettlementOutcome.LOST:
        return -1.0
    if outcome.is_refund:
        return -float(tax.refund_loss_rate)
    return 0.0


def _check_trained_through(predictor: Predictor, t: datetime) -> None:
    value = getattr(predictor, "trained_through", None)
    if isinstance(value, datetime) and ensure_utc(value) >= t:
        raise LeakageError(
            f"the trainer returned a model trained on data up to {ensure_utc(value).isoformat()}, "
            f"which is not before the decision time {t.isoformat()}"
        )


def run_backtest(
    data: HistoricalData,
    *,
    trainer: Trainer,
    odds: OddsHistory,
    config: BacktestConfig | None = None,
    settings: Settings | None = None,
    pricer: Pricer | None = None,
    decider: Decider | None = None,
    staker: Staker | None = None,
    resolver: Resolver | None = None,
    matches: Sequence[BacktestMatch] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> BacktestReport:
    """Run the walk-forward backtest (module docstring) and return the report.

    ``matches`` defaults to ``data.matches``. The engine-backed defaults use ``settings``
    (default :func:`get_settings`): decision thresholds, bankroll rules, tax and settlement rules,
    ``models.sigma_floor``. :class:`LeakageError` (and the ensemble's ``LookAheadError``) is raised -
    never swallowed - on look-ahead; any other exception of a predictor only skips that match.
    """
    cfg = config or BacktestConfig()
    st = settings or get_settings()
    tax = engine_ev.TaxModel.from_settings(st)
    pricer = pricer or EnginePricer(sigma_floor=st.models.sigma_floor, betclic=st.betclic)
    decider = decider or EngineDecider(
        st.decision, st.betclic, max_quote_age_s=cfg.max_quote_age.total_seconds()
    )
    staker = staker or EngineStaker(st.bankroll, st.betclic)
    resolver = resolver or EngineResolver(st.betclic)
    bank = SimulatedBankroll(
        initial=quantize_money(cfg.initial_bankroll or st.bankroll.starting_bankroll),
        tax=tax,
        timezone=st.timezone,
    )
    bank.curve.append(CurvePoint(time=utcnow(), bankroll=bank.total, profit=_ZERO))

    todo = sorted(matches if matches is not None else data.matches, key=lambda m: (m.start_time, m.match_id))
    if cfg.start is not None:
        todo = [m for m in todo if ensure_utc(m.start_time) >= ensure_utc(cfg.start)]
    if cfg.end is not None:
        todo = [m for m in todo if ensure_utc(m.start_time) < ensure_utc(cfg.end)]

    report = BacktestReport(
        created_at=utcnow(),
        start=ensure_utc(cfg.start) if cfg.start else (todo[0].start_time if todo else None),
        end=ensure_utc(cfg.end) if cfg.end else (todo[-1].start_time if todo else None),
        n_matches=len(todo),
        initial_bankroll=bank.initial,
        settings_summary={
            "decision_lead_h": cfg.decision_lead.total_seconds() / 3600,
            "retrain_every_days": cfg.retrain_every.total_seconds() / 86400,
            "min_probability": st.decision.min_probability,
            "min_ev": st.decision.min_ev,
            "max_sigma": st.decision.max_sigma,
            "kelly_fraction": st.bankroll.kelly_fraction,
            "tax_rate": str(tax.stake_tax_rate),
            "pricer": type(pricer).__name__,
            "decider": type(decider).__name__,
            "staker": type(staker).__name__,
        },
    )
    if todo:
        bank.curve[0] = CurvePoint(
            time=todo[0].start_time - cfg.decision_lead, bankroll=bank.total, profit=_ZERO
        )

    predictor: Predictor | None = None
    last_train: datetime | None = None
    versions: list[str] = []
    rejections: Counter[str] = Counter()
    accepted: list[BetRecord] = []
    predictions: list[PredictionRecord] = []
    clv_flat: list[float] = []
    already: dict[int, set[str]] = {}

    for i, match in enumerate(todo):
        if progress is not None:
            progress(i, len(todo))
        t = ensure_utc(match.start_time) - cfg.decision_lead
        bank.settle_until(t)
        if predictor is None or last_train is None or t - last_train >= cfg.retrain_every:
            training = data.until(t)
            if len(training.map_results) < cfg.min_training_maps:
                report.n_matches_skipped += 1
                continue
            try:
                fresh = trainer(training)
            except _LEAKAGE:
                raise
            except Exception as exc:  # a failed retrain keeps the previous model (or skips the match)
                logger.warning("training at %s failed: %s", t.isoformat(), exc)
                report.n_training_errors += 1
                last_train = t  # retry at the next retrain point, not before every match
                if predictor is None:
                    report.n_matches_skipped += 1
                    continue
            else:
                predictor = fresh
                last_train = t
                report.n_retrains += 1
                version = str(getattr(predictor, "model_version", type(predictor).__name__))
                if version not in versions:
                    versions.append(version)
        else:
            observe = getattr(predictor, "observe", None)
            if callable(observe):
                try:
                    observe(data.until(t))
                except _LEAKAGE:
                    raise
                except Exception as exc:  # keep predicting with the model as it was
                    logger.warning("online update at %s failed: %s", t.isoformat(), exc)
                    report.n_training_errors += 1
        _check_trained_through(predictor, t)

        context = match.context
        if context is not None and not cfg.veto_known_at_decision and context.known_veto:
            context = context.model_copy(update={"known_veto": [], "first_veto_team": None})
        query = MatchQuery(
            match_id=match.match_id,
            start_time=ensure_utc(match.start_time),
            format=match.format,
            team1_id=match.team1_id,
            team2_id=match.team2_id,
            as_of=t,
            context=context,
            tier=match.tier,
            is_lan=match.is_lan,
        )
        try:
            prediction = predictor.predict(query)
        except _LEAKAGE:
            raise
        except Exception as exc:  # one broken match must not abort the backtest
            logger.warning("prediction failed for match %s: %s", match.match_id, exc)
            report.n_prediction_errors += 1
            continue
        when = _prediction_time(prediction)
        if when is not None and when > t:
            raise LeakageError(
                f"the prediction for match {match.match_id} is stamped {when.isoformat()}, "
                f"after the decision time {t.isoformat()}"
            )
        report.n_matches_predicted += 1
        model_version = str(
            getattr(prediction, "model_version", "") or getattr(predictor, "model_version", "")
        )
        veto_known = bool(getattr(prediction, "veto_known", cfg.veto_known_at_decision))
        settle_at = data.match_end(match)

        quotes = [
            q
            for q in odds.quotes_before(match.match_id, t)
            if t - ensure_utc(q.captured_at) <= cfg.max_quote_age
        ]
        report.n_quotes += len(quotes)
        for quote in quotes:
            key = outcome_key(quote.spec, quote.selection_key)
            try:
                outcome = pricer(prediction, quote)
                priced = engine_ev.price(outcome, quote, tax)
            except _LEAKAGE:
                raise
            except (UnpriceableMarket, ValueError, KeyError) as exc:
                report.n_unpriceable += 1
                logger.debug("unpriceable %s: %s", key, exc)
                continue
            except Exception as exc:  # a broken pricer must not abort the backtest
                report.n_unpriceable += 1
                report.n_step_errors += 1
                logger.warning("pricing %s of match %s failed: %s", key, match.match_id, exc)
                continue
            report.n_priced += 1
            resolution = SettlementOutcome.UNKNOWN
            if match.actuals is not None:
                try:
                    resolution = resolver(quote.spec, quote.selection_key, match.actuals)
                except Exception as exc:  # unknown settlement = refund, never a guess
                    report.n_step_errors += 1
                    logger.warning("settling %s of match %s failed: %s", key, match.match_id, exc)
            p_settled = outcome.p_win_given_settled
            month = f"{to_local(t, st.timezone):%Y-%m}"
            pred_rec: PredictionRecord | None = None
            if resolution in (SettlementOutcome.WON, SettlementOutcome.LOST) and math.isfinite(p_settled):
                pred_rec = PredictionRecord(
                    match_id=match.match_id,
                    market_type=quote.spec.market_type.value,
                    month=month,
                    p=min(max(p_settled, 0.0), 1.0),
                    y=1 if resolution is SettlementOutcome.WON else 0,
                )
                predictions.append(pred_rec)
            ctx = DecisionContext(
                match=query,
                now=t,
                veto_known=veto_known,
                data_completeness=cfg.data_completeness,
                already_bet_keys=frozenset(already.get(match.match_id, set())),
            )
            try:
                decision = decider(priced, ctx)
            except _LEAKAGE:
                raise
            except Exception as exc:
                report.n_step_errors += 1
                rejections["decision_error"] += 1
                logger.warning("deciding %s of match %s failed: %s", key, match.match_id, exc)
                continue
            if not decision.accepted:
                for reason in decision.reasons:
                    rejections[engine_decision.reason_code(reason)] += 1
                continue
            report.n_accepted += 1
            try:
                closing = odds.closing_quote(match.match_id, key)
            except Exception as exc:  # CLV is a report metric only
                logger.warning("closing quote of %s for match %s failed: %s", key, match.match_id, exc)
                closing = None
            closing_odds = closing.odds if closing is not None else None
            clv = (
                float(quote.odds / closing_odds) - 1.0
                if closing_odds is not None and closing_odds > 0
                else None
            )
            if clv is not None:
                clv_flat.append(clv)
            state = bank.state(t)
            staking_failed = False
            try:
                raw_stake = D(staker(decision, state))
                stake = quantize_money(raw_stake) if raw_stake.is_finite() else _ZERO
                stake = min(stake, max(bank.cash, _ZERO))  # never more than the free balance
            except Exception as exc:
                report.n_step_errors += 1
                staking_failed = True
                stake = _ZERO
                logger.warning("staking %s of match %s failed: %s", key, match.match_id, exc)
            bet = BetRecord(
                match_id=match.match_id,
                decided_at=t,
                market_type=quote.spec.market_type.value,
                spec_key=quote.spec.key(),
                selection=quote.selection_key,
                odds=quote.odds,
                closing_odds=closing_odds,
                clv=clv,
                p_win=outcome.p_win,
                p_settled=p_settled,
                sigma=outcome.sigma,
                ev=priced.ev,
                stake=stake if stake > 0 else _ZERO,
                outcome=resolution.value,
                flat_pnl=_flat_pnl(quote.odds, resolution, tax),
                model_version=model_version,
            )
            if stake > 0:
                bank.place(bet, settle_at, resolution)
                bet.outcome = "open"
                already.setdefault(match.match_id, set()).add(
                    engine_decision.bet_key(match.match_id, quote.spec.key(), quote.selection_key)
                )
                if pred_rec is not None:
                    pred_rec.bet = True
            else:
                rejections["staking_error" if staking_failed else "staking"] += 1
            accepted.append(bet)
    if progress is not None:
        progress(len(todo), len(todo))
    bank.settle_until(None)
    _fill_report(report, bank, accepted, predictions, clv_flat, rejections, versions, cfg)
    return report


def _fill_report(
    report: BacktestReport,
    bank: SimulatedBankroll,
    accepted: list[BetRecord],
    predictions: list[PredictionRecord],
    clv_flat: list[float],
    rejections: Counter[str],
    versions: list[str],
    cfg: BacktestConfig,
) -> None:
    placed = [b for b in accepted if b.placed]
    counts = Counter(b.outcome for b in placed)
    report.model_versions = versions
    report.n_bets = len(placed)
    report.n_won = counts.get("won", 0)
    report.n_lost = counts.get("lost", 0)
    report.n_void = counts.get("void", 0)
    report.n_push = counts.get("push", 0)
    report.n_unknown = counts.get("unknown", 0)
    decided = report.n_won + report.n_lost
    report.hit_rate = report.n_won / decided if decided else None
    report.total_staked = sum((b.stake for b in placed), _ZERO)
    report.total_returned = sum((b.payout for b in placed), _ZERO)
    report.profit = report.total_returned - report.total_staked
    report.roi = float(report.profit / report.total_staked) if report.total_staked > 0 else None
    report.final_bankroll = bank.total
    report.bankroll_growth = float(bank.total / bank.initial) - 1.0 if bank.initial > 0 else None
    report.max_drawdown, report.max_drawdown_pct = max_drawdown([p.bankroll for p in bank.curve])
    report.profit_curve = bank.curve
    report.flat_n = len(accepted)
    report.flat_profit = float(sum(b.flat_pnl for b in accepted))
    report.flat_roi = report.flat_profit / report.flat_n if report.flat_n else None
    clvs = [b.clv for b in placed if b.clv is not None]
    report.n_clv = len(clvs)
    report.avg_clv = sum(clvs) / len(clvs) if clvs else None
    report.positive_clv_share = sum(1 for c in clvs if c > 0) / len(clvs) if clvs else None
    report.avg_clv_flat = sum(clv_flat) / len(clv_flat) if clv_flat else None
    y_all = [p.y for p in predictions]
    p_all = [p.p for p in predictions]
    report.n_predictions = len(predictions)
    if predictions:
        report.brier_all = brier(y_all, p_all)
        report.log_loss_all = log_loss(y_all, p_all)
    bet_preds = [p for p in predictions if p.bet]
    report.n_bet_predictions = len(bet_preds)
    if bet_preds:
        report.brier_bets = brier([p.y for p in bet_preds], [p.p for p in bet_preds])
        report.log_loss_bets = log_loss([p.y for p in bet_preds], [p.p for p in bet_preds])
    report.calibration = calibration_report(y_all, p_all, cfg.n_calibration_bins)
    # deep_data: the confidence rule needs the [0.75, 1] range in 2.5-pp bins and a per-market verdict
    report.high_confidence = high_confidence_report(p_all, y_all)
    guard = ConfidenceGuard.from_predictions(predictions)
    report.market_trust = {k: v.as_dict() for k, v in guard.trust.items()}
    report.predictions = list(predictions)
    report.by_market = breakdown(accepted, predictions, lambda b: b.market_type, lambda p: p.market_type)
    report.by_month = breakdown(
        accepted,
        predictions,
        lambda b: f"{to_local(b.decided_at, bank.timezone):%Y-%m}",
        lambda p: p.month,
    )
    report.rejection_reasons = dict(rejections)
    bets = accepted if cfg.keep_unplaced_decisions else placed
    if cfg.max_report_bets is not None:
        bets = bets[-cfg.max_report_bets :]
    report.bets = bets
    if report.n_unknown:
        report.warnings.append(
            f"{report.n_unknown} bets could not be settled from the stored results "
            "and were counted as refunds"
        )
    if report.n_prediction_errors:
        report.warnings.append(f"{report.n_prediction_errors} matches could not be predicted (see the log)")
    if report.n_training_errors:
        report.warnings.append(
            f"{report.n_training_errors} trainings / online updates failed; the previous model was kept "
            "(see the log)"
        )
    if report.n_step_errors:
        report.warnings.append(
            f"{report.n_step_errors} pricing / settlement / decision / staking steps failed and were "
            "skipped (see the log)"
        )
