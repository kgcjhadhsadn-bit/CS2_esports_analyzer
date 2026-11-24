"""Walk-forward engine (cs2bot.backtest.runner) on hand-made data: money, settlement, leakage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cs2bot.backtest import (
    BacktestConfig,
    BacktestMatch,
    BacktestReport,
    EnginePricer,
    FlatStaker,
    HistoricalData,
    InMemoryOddsHistory,
    LeakageError,
    MatchQuery,
    ProbabilityTable,
    SimulatedBankroll,
    ThresholdDecider,
    TrainingSet,
    assert_no_leakage,
    max_drawdown,
    outcome_key,
    run_backtest,
    series_match_prediction,
)
from cs2bot.backtest.policy import DecisionContext, EngineDecider, UnpriceableMarket
from cs2bot.config import Settings
from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import MapSequenceProbability, MapWinEstimate, OutcomeProbability
from cs2bot.domain.records import MapResultRecord
from cs2bot.domain.results import MapActual, MatchActuals
from cs2bot.engine import ev as engine_ev
from cs2bot.models.sim_series import series_outcome

T0 = datetime(2025, 3, 3, 12, 0, tzinfo=UTC)  # a Monday
MW = MarketSpec(market_type=MarketType.MATCH_WINNER)
TOTAL3 = MarketSpec(market_type=MarketType.TOTAL_MAPS, line=3.0)
MAP3 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=3)


def _map(match_id: int, k: int, t1: int, t2: int, start: datetime, t1_wins: bool) -> MapResultRecord:
    return MapResultRecord(
        match_id=match_id,
        map_number=k,
        map_name=["mirage", "inferno", "nuke"][k - 1],
        played_at=start + timedelta(hours=k - 1),
        team1_id=t1,
        team2_id=t2,
        team1_rounds=13 if t1_wins else 7,
        team2_rounds=7 if t1_wins else 13,
    )


def _match(match_id: int, start: datetime, wins: list[bool]) -> tuple[BacktestMatch, list[MapResultRecord]]:
    """A BO3 between teams 1 and 2; ``wins[k]`` = team1 won map k+1."""
    maps = [_map(match_id, k + 1, 1, 2, start, w) for k, w in enumerate(wins)]
    actuals = MatchActuals(
        match_id=match_id,
        format=MatchFormat.BO3,
        team1_id=1,
        team2_id=2,
        maps=tuple(
            MapActual(
                map_number=m.map_number,
                map_name=m.map_name,
                team1_rounds=m.team1_rounds,
                team2_rounds=m.team2_rounds,
            )
            for m in maps
        ),
    )
    return BacktestMatch(match_id, start, MatchFormat.BO3, 1, 2, actuals=actuals), maps


def _quote(
    spec: MarketSpec, selection: str, odds: str, at: datetime, sel_id: int = 1, available: bool = True
) -> OddsQuote:
    return OddsQuote(
        selection_id=sel_id,
        bookmaker_event_id=1,
        external_event_id="e",
        market_raw_name=spec.key(),
        selection_raw_name=selection,
        spec=spec,
        selection_key=selection,
        odds=Decimal(odds),
        captured_at=at,
        is_available=available,
    )


class TablePredictor:
    """Fixed outcome probabilities for every match (no skill needed for money tests)."""

    def __init__(
        self, probs: dict[str, OutcomeProbability], *, stamp_offset: timedelta = timedelta(0)
    ) -> None:
        self.probs = probs
        self.stamp_offset = stamp_offset
        self.model_version = "table-test"
        self.trained_through: datetime | None = None

    def predict(self, query: MatchQuery) -> ProbabilityTable:
        return ProbabilityTable(
            match_id=query.match_id, as_of=query.as_of + self.stamp_offset, outcomes=self.probs
        )


def _op(
    spec: MarketSpec, sel: str, p: float, p_push: float = 0.0, p_void: float = 0.0
) -> tuple[str, OutcomeProbability]:
    return outcome_key(spec, sel), OutcomeProbability(
        spec_key=spec.key(), selection=sel, p_win=p, p_push=p_push, p_void=p_void, sigma=0.01
    )


@pytest.fixture
def world() -> tuple[HistoricalData, list[BacktestMatch], InMemoryOddsHistory]:
    m1, r1 = _match(1, T0, [True, True])  # 2-0 team1: total maps 2, map 3 not played
    m2, r2 = _match(2, T0 + timedelta(days=1), [True, False, False])  # 1-2: total maps 3
    history = InMemoryOddsHistory()
    for m in (m1, m2):
        history.set_kickoff(m.match_id, m.start_time)
        open_at, close_at = m.start_time - timedelta(hours=20), m.start_time - timedelta(minutes=5)
        history.add_many(
            m.match_id,
            [
                _quote(MW, "team1", "2.00", open_at),
                _quote(MW, "team1", "1.60", close_at),
                _quote(TOTAL3, "over", "2.50", open_at),
                _quote(MAP3, "team2", "3.00", open_at),
                _quote(MAP3, "team2", "2.40", m.start_time),  # at kick-off: never the closing line
            ],
        )
    data = HistoricalData(map_results=r1 + r2, matches=[m1, m2])
    return data, [m1, m2], history


def _probs() -> dict[str, OutcomeProbability]:
    return dict(
        [
            _op(MW, "team1", 0.7),
            _op(TOTAL3, "over", 0.45, p_push=0.4),  # BO3 total maps over 3.0: only 3 maps -> push
            _op(MAP3, "team2", 0.3, p_void=0.5),
        ]
    )


def test_settlement_win_push_void_and_lose_with_polish_tax(world, settings: Settings) -> None:
    data, _, history = world
    report = run_backtest(
        data,
        trainer=lambda training: TablePredictor(_probs()),
        odds=history,
        settings=settings,
        decider=ThresholdDecider(min_probability=0.0, min_ev=0.0),
        staker=FlatStaker(Decimal("10")),
    )
    tax = engine_ev.TaxModel.from_settings(settings)
    win_at_2 = engine_ev.net_payout(Decimal("10"), Decimal("2.00"), tax)
    assert win_at_2 == Decimal("17.60")  # (10 - 12%) * 2.00
    by_match = {(b.match_id, b.spec_key, b.selection): b for b in report.bets}
    mw1 = by_match[(1, MW.key(), "team1")]
    assert (mw1.outcome, mw1.payout, mw1.pnl) == ("won", win_at_2, Decimal("7.60"))
    assert by_match[(2, MW.key(), "team1")].outcome == "lost"
    assert by_match[(2, MW.key(), "team1")].pnl == Decimal("-10")
    # total maps over 3.0: match 1 had 2 maps (lost), match 2 had exactly 3 (push, stake refunded)
    assert by_match[(1, TOTAL3.key(), "over")].outcome == "lost"
    push = by_match[(2, TOTAL3.key(), "over")]
    assert (push.outcome, push.payout, push.pnl) == ("push", Decimal("10"), Decimal("0"))
    # map 3 not played in match 1 -> void; played (team2 won) in match 2
    void = by_match[(1, MAP3.key(), "team2")]
    assert (void.outcome, void.pnl) == ("void", Decimal("0"))
    won3 = by_match[(2, MAP3.key(), "team2")]
    assert (
        won3.outcome == "won" and won3.pnl == engine_ev.net_payout(Decimal("10"), Decimal("3.00"), tax) - 10
    )
    assert (report.n_bets, report.n_won, report.n_lost, report.n_push, report.n_void) == (6, 2, 2, 1, 1)
    assert report.hit_rate == pytest.approx(0.5)
    assert report.total_staked == Decimal("60")
    assert report.profit == sum((b.pnl for b in report.bets), Decimal("0"))
    assert report.final_bankroll == report.initial_bankroll + report.profit
    assert report.roi == pytest.approx(float(report.profit / report.total_staked))
    # flat (1-unit) profit uses the same tax rules
    assert report.flat_profit == pytest.approx(
        (0.88 * 2.0 - 1) - 1 + (-1) + 0 + 0 + (0.88 * 3.0 - 1), abs=1e-9
    )


def test_clv_uses_the_last_quote_strictly_before_kickoff(world, settings: Settings) -> None:
    data, _, history = world
    report = run_backtest(
        data,
        trainer=lambda training: TablePredictor(_probs()),
        odds=history,
        settings=settings,
        decider=ThresholdDecider(),
        staker=FlatStaker(Decimal("5")),
    )
    mw = [b for b in report.bets if b.spec_key == MW.key()]
    assert all(b.odds == Decimal("2.00") and b.closing_odds == Decimal("1.60") for b in mw)
    assert all(b.clv == pytest.approx(2.0 / 1.6 - 1) for b in mw)
    map3 = [b for b in report.bets if b.spec_key == MAP3.key()]
    assert all(b.closing_odds == Decimal("3.00") and b.clv == pytest.approx(0.0) for b in map3)
    totals = [b for b in report.bets if b.spec_key == TOTAL3.key()]
    assert all(b.clv == pytest.approx(0.0) for b in totals)
    assert report.avg_clv == pytest.approx((2 * 0.25) / 6)
    assert report.positive_clv_share == pytest.approx(2 / 6)


def test_report_breakdowns_curve_and_rendering(world, settings: Settings) -> None:
    data, _, history = world
    report = run_backtest(
        data,
        trainer=lambda training: TablePredictor(_probs()),
        odds=history,
        settings=settings,
        decider=ThresholdDecider(),
        staker=FlatStaker(Decimal("10")),
    )
    markets = {r.key: r for r in report.by_market}
    assert set(markets) == {"match_winner", "total_maps", "map_winner"}
    assert markets["match_winner"].n_bets == 2 and markets["match_winner"].profit == Decimal("-2.40")
    assert [r.key for r in report.by_month] == ["2025-03"]
    assert report.by_month[0].n_bets == 6
    # bankroll curve: start point + one point per settled bet
    assert len(report.profit_curve) == 1 + 6
    assert report.profit_curve[-1].bankroll == report.final_bankroll
    assert report.max_drawdown >= 0
    # probability metrics only use won / lost outcomes (push and void excluded)
    assert report.n_predictions == 4 and report.calibration["n"] == 4
    md = report.to_markdown()
    for heading in (
        "# Backtest report",
        "## Betting",
        "## Probabilities",
        "## By market",
        "## By month",
        "## Bets",
    ):
        assert heading in md
    assert "match_winner" in md and "PLN" in md
    again = BacktestReport.model_validate_json(report.model_dump_json())
    assert again.profit == report.profit and len(again.bets) == len(report.bets)
    json.loads(report.model_dump_json())  # plain JSON


def test_engine_defaults_apply_the_real_rules(world, settings: Settings) -> None:
    """Real decision rules (p(win | settled) >= 0.75, EV >= 3% ...) and 1/4 Kelly with caps."""
    data, _, history = world
    report = run_backtest(
        data, trainer=lambda training: TablePredictor(_probs()), odds=history, settings=settings
    )
    assert report.n_priced == 6
    # match winner (p 0.70) and map 3 (p | settled 0.60) fail the probability rule; total maps
    # over 3.0 has p(win | settled) = 0.45 / 0.60 = 0.75 and a large EV -> bet, sized by Kelly
    assert report.rejection_reasons == {"low_probability": 4}
    assert report.n_bets == 2 and {b.spec_key for b in report.bets} == {TOTAL3.key()}
    for bet in report.bets:
        assert Decimal("0") < bet.stake <= Decimal("20.00")  # 2% of the 1000 PLN bankroll


def test_engine_decider_and_staker_bet_a_clear_edge(world, settings: Settings) -> None:
    data, _, history = world
    probs = dict([_op(MW, "team1", 0.9)])
    report = run_backtest(
        data, trainer=lambda training: TablePredictor(probs), odds=history, settings=settings
    )
    assert report.n_bets == 2
    first, second = report.bets
    # the live rule (confidence tiers, odds ignored): p 0.90 -> the 85 % tier = 1.5 % of 1000 PLN
    assert first.stake == Decimal("15.00")
    assert second.stake == (Decimal("0.015") * (Decimal("1000") + first.pnl)).quantize(
        Decimal("0.1"), "ROUND_DOWN"
    )
    assert report.settings_summary["staking_mode"] == "confidence_tiers"
    assert all(bet.ev > settings.decision.min_ev for bet in report.bets)


def test_unpriceable_quotes_and_prediction_errors_do_not_abort(world, settings: Settings) -> None:
    data, _, history = world

    class Flaky(TablePredictor):
        def predict(self, query: MatchQuery) -> ProbabilityTable:
            if query.match_id == 1:
                raise RuntimeError("broken features")
            return super().predict(query)

    report = run_backtest(
        data,
        trainer=lambda training: Flaky(dict([_op(MW, "team1", 0.7)])),
        odds=history,
        settings=settings,
        decider=ThresholdDecider(),
        staker=FlatStaker(Decimal("10")),
    )
    assert report.n_prediction_errors == 1 and report.n_matches_predicted == 1
    assert report.n_unpriceable == 2 and report.n_priced == 1
    assert any("could not be predicted" in w for w in report.warnings)


def test_quotes_before_and_closing_quote_semantics() -> None:
    h = InMemoryOddsHistory()
    h.set_kickoff(7, T0)
    h.add_many(
        7,
        [
            _quote(MW, "team1", "1.90", T0 - timedelta(hours=10)),
            _quote(MW, "team1", "1.80", T0 - timedelta(hours=5)),
            _quote(MW, "team1", "1.80", T0 - timedelta(hours=2), available=False),
            _quote(MW, "team1", "1.75", T0 - timedelta(minutes=30)),
            _quote(MW, "team1", "1.10", T0 + timedelta(minutes=10)),  # in-play
            _quote(MW, "team2", "2.10", T0 - timedelta(hours=10), sel_id=2),
        ],
    )
    at = lambda t: {(q.selection_key, q.odds) for q in h.quotes_before(7, t)}  # noqa: E731
    assert at(T0 - timedelta(hours=10)) == set()  # strictly before
    assert at(T0 - timedelta(hours=9)) == {("team1", Decimal("1.90")), ("team2", Decimal("2.10"))}
    assert at(T0 - timedelta(hours=1)) == {("team2", Decimal("2.10"))}  # team1 suspended
    assert at(T0) == {("team1", Decimal("1.75")), ("team2", Decimal("2.10"))}
    closing = h.closing_quote(7, outcome_key(MW, "team1"))
    assert closing is not None and closing.odds == Decimal("1.75")
    assert h.closing_quote(7, outcome_key(MW, "draw")) is None
    assert h.quotes_before(8, T0) == []


def test_old_quotes_are_not_on_offer(world, settings: Settings) -> None:
    data, _, history = world
    report = run_backtest(
        data,
        trainer=lambda training: TablePredictor(_probs()),
        odds=history,
        settings=settings,
        config=BacktestConfig(max_quote_age=timedelta(hours=2)),
        decider=ThresholdDecider(),
        staker=FlatStaker(Decimal("10")),
    )
    assert report.n_quotes == 0 and report.n_bets == 0


# ------------------------------------------------------------------------------ leakage guard


def test_training_set_rejects_future_records() -> None:
    _, maps = _match(1, T0, [True, True])
    with pytest.raises(LeakageError, match="not before the cutoff"):
        TrainingSet(cutoff=T0 + timedelta(minutes=30), map_results=tuple(maps))
    with pytest.raises(LeakageError):
        assert_no_leakage(maps, T0)
    assert_no_leakage(maps, T0 + timedelta(hours=1, seconds=1))
    finished_late = BacktestMatch(1, T0, MatchFormat.BO3, 1, 2, end_time=T0 + timedelta(hours=3))
    with pytest.raises(LeakageError, match="ended"):
        TrainingSet(cutoff=T0 + timedelta(hours=1), matches=(finished_late,))


def test_historical_data_hides_results_until_the_match_is_over() -> None:
    m, maps = _match(1, T0, [True, False, True])
    data = HistoricalData(map_results=maps, matches=[m], result_delay=timedelta(hours=1))
    # map 1 started at T0, but the series ends after map 3 (T0 + 2h) + 1h
    assert data.until(T0 + timedelta(hours=1)).map_results == ()
    assert data.until(T0 + timedelta(hours=2, minutes=59)).map_results == ()
    seen = data.until(T0 + timedelta(hours=3))
    assert len(seen.map_results) == 3 and seen.matches == ()
    assert data.until(T0 + timedelta(hours=3, seconds=1)).matches == (m,)
    assert data.match_end(m) == T0 + timedelta(hours=3)
    with pytest.raises(ValueError, match="result_delay"):
        HistoricalData(result_delay=timedelta(0))


def test_trainer_only_ever_sees_the_past(world, settings: Settings) -> None:
    data, matches, history = world
    seen: list[tuple[datetime, TrainingSet]] = []

    def spy(training: TrainingSet) -> TablePredictor:
        seen.append((training.cutoff, training))
        return TablePredictor(_probs())

    run_backtest(
        data,
        trainer=spy,
        odds=history,
        settings=settings,
        config=BacktestConfig(retrain_every=timedelta(hours=1)),
        decider=ThresholdDecider(),
        staker=FlatStaker(Decimal("1")),
    )
    assert [c for c, _ in seen] == [m.start_time - timedelta(hours=1) for m in matches]
    for cutoff, training in seen:
        assert all(r.played_at < cutoff for r in training.map_results)
    assert seen[0][1].map_results == ()  # nothing finished before the first match
    assert len(seen[1][1].map_results) == 2  # match 1 (2 maps) finished before match 2


def test_cheating_trainer_is_detected(world, settings: Settings) -> None:
    """A trainer that reads the full history behind the engine's back is caught by trained_through."""
    data, _, history = world
    everything = list(data.map_results)

    def cheat(training: TrainingSet) -> TablePredictor:
        model = TablePredictor(_probs())
        model.trained_through = max(r.played_at for r in everything)  # trained on the future
        return model

    with pytest.raises(LeakageError, match="trained on data up to"):
        run_backtest(data, trainer=cheat, odds=history, settings=settings, decider=ThresholdDecider())


def test_prediction_stamped_after_the_decision_time_is_detected(world, settings: Settings) -> None:
    data, _, history = world
    with pytest.raises(LeakageError, match="after the decision time"):
        run_backtest(
            data,
            trainer=lambda training: TablePredictor(_probs(), stamp_offset=timedelta(minutes=1)),
            odds=history,
            settings=settings,
        )


def test_look_ahead_inside_a_model_is_not_swallowed(world, settings: Settings) -> None:
    from cs2bot.models.ensemble import LookAheadError

    data, _, history = world

    class Stale(TablePredictor):
        def predict(self, query: MatchQuery) -> ProbabilityTable:
            raise LookAheadError("model trained past the decision time")

    with pytest.raises(LookAheadError):
        run_backtest(data, trainer=lambda training: Stale(_probs()), odds=history, settings=settings)


# ------------------------------------------------------------------------------ pieces


def test_max_drawdown() -> None:
    vals = [Decimal(x) for x in ("100", "120", "90", "130", "65", "70")]
    dd, pct = max_drawdown(vals)
    assert dd == Decimal("65") and pct == pytest.approx(0.5)
    assert max_drawdown([]) == (Decimal("0"), 0.0)
    assert max_drawdown([Decimal("1"), Decimal("2")]) == (Decimal("0"), 0.0)


def test_simulated_bankroll_state(settings: Settings) -> None:
    from cs2bot.backtest.report import BetRecord
    from cs2bot.domain.results import SettlementOutcome

    bank = SimulatedBankroll(initial=Decimal("1000"), tax=engine_ev.TaxModel.from_settings(settings))
    bet = BetRecord(
        match_id=5,
        decided_at=T0,
        market_type="match_winner",
        spec_key=MW.key(),
        selection="team1",
        odds=Decimal("2.0"),
        p_win=0.6,
        p_settled=0.6,
        sigma=0.01,
        ev=0.05,
        stake=Decimal("20"),
    )
    bank.place(bet, T0 + timedelta(hours=3), SettlementOutcome.LOST)
    state = bank.state(T0 + timedelta(minutes=1))
    assert state.balance == Decimal("980") and state.bankroll_reference == Decimal("1000")
    assert state.exposure_by_match == {5: Decimal("20")} and state.bets_placed_today == 1
    bank.settle_until(T0 + timedelta(hours=3))
    later = bank.state(T0 + timedelta(hours=4))
    assert later.balance == Decimal("980") and later.realized_pnl_today == Decimal("-20")
    assert later.day_start_bankroll == Decimal("1000") and later.exposure_by_match == {}
    assert bet.outcome == "lost" and bet.pnl == Decimal("-20")
    # next Warsaw day: the loss no longer counts for today but still for the week
    next_day = bank.state(T0 + timedelta(days=1))
    assert next_day.realized_pnl_today == 0 and next_day.realized_pnl_week == Decimal("-20")


def test_engine_pricer_and_decider() -> None:
    pricer = EnginePricer(sigma_floor=0.02)
    table = ProbabilityTable(match_id=1, as_of=T0, outcomes=dict([_op(MW, "team1", 0.6)]))
    q = _quote(MW, "team1", "2.0", T0 - timedelta(hours=1))
    assert pricer(table, q).sigma == 0.02
    with pytest.raises(UnpriceableMarket):
        pricer(table, _quote(MW, "team2", "2.0", T0))
    with pytest.raises(UnpriceableMarket):
        pricer(object(), q)
    assert pricer({outcome_key(MW, "team1"): table.outcomes[outcome_key(MW, "team1")]}, q).p_win == 0.6
    est = MapWinEstimate(map_name="nuke", p_team1_win=0.64, sigma=0.03)
    assert pricer(est, _quote(MW, "team2", "2.0", T0)).p_win == pytest.approx(0.36)
    assert pricer(est, _quote(MAP3.model_copy(update={"map_number": 1}), "team1", "2.0", T0)).p_win == 0.64
    with pytest.raises(UnpriceableMarket):
        pricer(est, _quote(MAP3, "team1", "2.0", T0))
    # the engine decider accepts a 2 h old quote when the backtest allows it
    settings = Settings()
    priced = engine_ev.price(pricer(table, q).model_copy(update={"p_win": 0.9}), q, settings)
    query = MatchQuery(1, T0 + timedelta(hours=1), MatchFormat.BO3, 1, 2, as_of=T0)
    ctx = DecisionContext(match=query, now=T0, veto_known=True, data_completeness=1.0)
    assert EngineDecider(settings.decision, settings.betclic, max_quote_age_s=3 * 3600)(priced, ctx).accepted
    stale = EngineDecider(settings.decision, settings.betclic)(priced, ctx)
    assert not stale.accepted and any("odds_stale" in r for r in stale.reasons)


def test_series_match_prediction_is_exact_and_prices_series_markets() -> None:
    seq = MapSequenceProbability(
        maps=["mirage", "inferno", "nuke"], pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None], probability=1.0
    )
    est = {
        "mirage": MapWinEstimate(map_name="mirage", p_team1_win=0.6, sigma=0.05),
        "inferno": MapWinEstimate(map_name="inferno", p_team1_win=0.7, sigma=0.05),
        "nuke": MapWinEstimate(map_name="nuke", p_team1_win=0.5, sigma=0.05),
    }
    pred = series_match_prediction(
        match_id=3,
        fmt=MatchFormat.BO3,
        team1_id=1,
        team2_id=2,
        veto_distribution=[seq],
        map_estimates=est,
        model_version="t",
        created_at=T0,
        veto_known=True,
    )
    exact = series_outcome(MatchFormat.BO3, [0.6, 0.7, 0.5])
    assert pred.central.p_team1_win == pytest.approx(exact.p_team1_win)
    assert pred.central.slots[2].p_played == pytest.approx(exact.p_played[2])
    assert pred.low is not None and pred.high is not None
    assert pred.low.p_team1_win < pred.central.p_team1_win < pred.high.p_team1_win
    assert sum(pred.central.map_score_pmf.values()) == pytest.approx(1.0)
    pricer = EnginePricer()
    map3 = pricer(pred, _quote(MAP3, "team1", "2.0", T0))
    assert map3.p_void == pytest.approx(1 - exact.p_played[2]) and map3.p_win_given_settled == pytest.approx(
        0.5
    )
    assert pricer(pred, _quote(MW, "team1", "2.0", T0)).sigma > 0.01
    with pytest.raises(UnpriceableMarket):  # round markets need a simulation
        pricer(
            pred,
            _quote(
                MarketSpec(market_type=MarketType.TOTAL_ROUNDS, map_number=1, line=21.5), "over", "1.9", T0
            ),
        )
