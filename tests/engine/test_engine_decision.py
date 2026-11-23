"""Decision rules: each rule on its own, several at once, overrides and the DB helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from cs2bot.config import BetclicSettings, DecisionSettings, MarketThresholds
from cs2bot.db.models import ContextFlagRow, Veto
from cs2bot.domain.betting import BetDecision
from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import (
    BetStatus,
    ContextFlagType,
    LinkStatus,
    MatchStatus,
    TeamSlot,
    VetoAction,
)
from cs2bot.domain.markets import SELECTION_OVER, MarketSpec, MarketType
from cs2bot.engine.decision import (
    MatchDecisionInfo,
    ReasonCode,
    bet_key,
    evaluate,
    evaluate_all,
    is_slot_specific,
    load_already_bet_keys,
    load_match_decision_info,
    reason_code,
    settled_sigma,
    thresholds_for,
)
from tests.engine.engine_factories import (
    T0,
    add_bet,
    add_event,
    add_match,
    make_outcome,
    make_prediction,
    make_priced,
)

NOW = T0 + timedelta(seconds=60)


@dataclass
class Case:
    """Inputs of one evaluate() call; tweak one field per test case."""

    priced_kw: dict[str, Any] = field(default_factory=dict)
    match_kw: dict[str, Any] = field(default_factory=dict)
    settings_kw: dict[str, Any] = field(default_factory=dict)
    betclic_kw: dict[str, Any] = field(default_factory=dict)
    now: datetime = NOW

    def run(self) -> BetDecision:
        priced = make_priced(**self.priced_kw)
        match = MatchDecisionInfo(
            **{
                "match_id": 1,
                "scheduled_at": T0 + timedelta(hours=3),
                "data_completeness": 0.99,
                "link_confidence": 0.97,
                "link_status": LinkStatus.AUTO,
                "veto_known": False,
                **self.match_kw,
            }
        )
        return evaluate(
            priced,
            match=match,
            settings=DecisionSettings(**self.settings_kw),
            betclic=BetclicSettings(**self.betclic_kw),
            now=self.now,
        )


def codes(decision: BetDecision) -> list[str]:
    return [reason_code(r) for r in decision.reasons]


def test_baseline_is_accepted() -> None:
    d = Case().run()
    assert d.accepted and d.reasons == []
    assert d.match_id == 1 and d.decided_at == NOW
    assert d.stake == Decimal("0")  # sizing happens later in engine.bankroll


MAP1 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=1)

SINGLE_RULES: list[tuple[str, Case, ReasonCode]] = [
    ("disabled", Case(settings_kw={"disabled_markets": ["match_winner"]}), ReasonCode.MARKET_DISABLED),
    (
        "disabled-case-insensitive",
        Case(settings_kw={"disabled_markets": [" Match_Winner "]}),
        ReasonCode.MARKET_DISABLED,
    ),
    (
        "other-market",
        Case(priced_kw={"spec": MarketSpec(market_type=MarketType.OTHER)}),
        ReasonCode.MARKET_DISABLED,
    ),
    ("unavailable", Case(priced_kw={"is_available": False}), ReasonCode.SELECTION_UNAVAILABLE),
    ("odds-below-min", Case(settings_kw={"min_odds": Decimal("1.70")}), ReasonCode.ODDS_OUT_OF_RANGE),
    ("odds-above-max", Case(settings_kw={"max_odds": Decimal("1.50")}), ReasonCode.ODDS_OUT_OF_RANGE),
    ("stale-odds", Case(now=T0 + timedelta(seconds=601)), ReasonCode.ODDS_STALE),
    ("stale-custom-age", Case(betclic_kw={"odds_max_age_s": 30}), ReasonCode.ODDS_STALE),
    ("future-odds", Case(priced_kw={"captured_at": NOW + timedelta(minutes=10)}), ReasonCode.ODDS_STALE),
    (
        "too-close",
        Case(match_kw={"scheduled_at": NOW + timedelta(minutes=5)}),
        ReasonCode.OUTSIDE_TIME_WINDOW,
    ),
    ("started", Case(match_kw={"scheduled_at": NOW - timedelta(minutes=5)}), ReasonCode.OUTSIDE_TIME_WINDOW),
    ("too-far", Case(match_kw={"scheduled_at": NOW + timedelta(hours=49)}), ReasonCode.OUTSIDE_TIME_WINDOW),
    ("unknown-start", Case(match_kw={"scheduled_at": None}), ReasonCode.OUTSIDE_TIME_WINDOW),
    # p = 0.70 at 1.80: EV = 0.7 * 0.584 - 0.3 = 0.109 is fine, the confidence is not
    ("low-probability", Case(priced_kw={"p_win": 0.70, "odds": "1.80"}), ReasonCode.LOW_PROBABILITY),
    # p = 0.80 at 1.45: EV = 0.8 * 0.276 - 0.2 = 0.0208 < 0.03
    ("low-ev", Case(priced_kw={"odds": "1.45"}), ReasonCode.LOW_EV),
    ("high-sigma", Case(priced_kw={"sigma": 0.06}), ReasonCode.HIGH_SIGMA),
    ("low-completeness", Case(match_kw={"data_completeness": 0.9}), ReasonCode.LOW_DATA_COMPLETENESS),
    ("unknown-completeness", Case(match_kw={"data_completeness": None}), ReasonCode.LOW_DATA_COMPLETENESS),
    ("weak-link-confidence", Case(match_kw={"link_confidence": 0.85}), ReasonCode.WEAK_EVENT_LINK),
    ("unknown-link-confidence", Case(match_kw={"link_confidence": None}), ReasonCode.WEAK_EVENT_LINK),
    (
        "link-pending-review",
        Case(match_kw={"link_status": LinkStatus.PENDING_REVIEW}),
        ReasonCode.WEAK_EVENT_LINK,
    ),
    ("context-veto", Case(match_kw={"context_veto": True}), ReasonCode.CONTEXT_VETO),
    ("waiting-for-veto", Case(priced_kw={"spec": MAP1}), ReasonCode.WAITING_FOR_VETO),
    (
        "duplicate",
        Case(match_kw={"already_bet_keys": {bet_key(1, "match_winner", "team1")}}),
        ReasonCode.DUPLICATE,
    ),
]


@pytest.mark.parametrize(
    ("case", "code"), [(c, code) for _, c, code in SINGLE_RULES], ids=[n for n, *_ in SINGLE_RULES]
)
def test_single_rule(case: Case, code: ReasonCode) -> None:
    d = case.run()
    assert not d.accepted
    assert codes(d) == [code.value], d.reasons


def test_confirmed_link_and_known_veto_are_fine() -> None:
    d = Case(
        priced_kw={"spec": MAP1}, match_kw={"link_status": LinkStatus.CONFIRMED, "veto_known": True}
    ).run()
    assert d.accepted, d.reasons


def test_several_failures_are_all_listed() -> None:
    d = Case(
        priced_kw={"odds": "1.45", "sigma": 0.08, "spec": MAP1, "is_available": False},
        match_kw={
            "link_status": LinkStatus.UNLINKED,
            "link_confidence": None,
            "context_veto": True,
            "context_veto_reason": "stand-in AWPer, lineup unclear",
            "already_bet_keys": {bet_key(1, MAP1.key(), "team1")},
        },
        now=T0 + timedelta(hours=1),
    ).run()
    assert not d.accepted
    assert codes(d) == [
        ReasonCode.SELECTION_UNAVAILABLE.value,
        ReasonCode.ODDS_STALE.value,
        ReasonCode.LOW_EV.value,
        ReasonCode.HIGH_SIGMA.value,
        ReasonCode.WEAK_EVENT_LINK.value,  # status
        ReasonCode.WEAK_EVENT_LINK.value,  # confidence
        ReasonCode.CONTEXT_VETO.value,
        ReasonCode.WAITING_FOR_VETO.value,
        ReasonCode.DUPLICATE.value,
    ]
    assert any("stand-in AWPer" in r for r in d.reasons)
    assert any("waiting for veto" in r for r in d.reasons)


def test_reason_messages_are_readable() -> None:
    d = Case(priced_kw={"odds": "1.45"}, settings_kw={"min_odds": Decimal("1.50")}).run()
    assert "odds 1.45 outside [1.5, 15]" in d.reasons[0]
    started = Case(match_kw={"scheduled_at": NOW - timedelta(minutes=5)}).run()
    assert "started 5 min ago" in started.reasons[0]


def test_per_market_overrides() -> None:
    strict = {"match_winner": MarketThresholds(min_probability=0.85, min_ev=0.2, max_sigma=0.01)}
    d = Case(settings_kw={"market_overrides": strict}).run()
    assert codes(d) == ["low_probability", "low_ev", "high_sigma"]
    # the override does not leak into other market types
    other = Case(
        priced_kw={"spec": MAP1}, match_kw={"veto_known": True}, settings_kw={"market_overrides": strict}
    )
    assert other.run().accepted
    th = thresholds_for(MarketType.MATCH_WINNER, DecisionSettings(market_overrides=strict))
    assert (th.min_probability, th.min_ev, th.max_sigma) == (0.85, 0.2, 0.01)
    partial = {"match_winner": MarketThresholds(min_ev=0.10)}
    th2 = thresholds_for(MarketType.MATCH_WINNER, DecisionSettings(market_overrides=partial))
    assert (th2.min_probability, th2.min_ev, th2.max_sigma) == (0.75, 0.10, 0.05)


def test_probability_check_ignores_refunds() -> None:
    # map 3 winner: p_win 0.6 with 0.25 void -> p(win | settled) = 0.8 >= 0.75
    spec3 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=3)
    d = Case(
        priced_kw={"spec": spec3, "p_win": 0.6, "p_void": 0.25, "odds": "1.80"}, match_kw={"veto_known": True}
    )
    assert d.run().accepted


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MarketSpec(market_type=MarketType.MATCH_WINNER), False),
        (MarketSpec(market_type=MarketType.TOTAL_MAPS, line=2.5), False),
        (MarketSpec(market_type=MarketType.MAP_WINNER, map_number=2), True),
        (MarketSpec(market_type=MarketType.TOTAL_ROUNDS, map_number=1, line=22.5), True),
        (MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=5, map_number=1, line=15.5), True),
        (MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=5, maps_scope=(1, 2), line=30.5), True),
        (MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=5, line=40.5), False),
        (MarketSpec(market_type=MarketType.TOP_FRAGGER), False),
    ],
)
def test_slot_specific_markets(market: MarketSpec, expected: bool) -> None:
    assert is_slot_specific(market) is expected
    sel = (
        SELECTION_OVER
        if market.line is not None and market.market_type is not MarketType.MAP_WINNER
        else "team1"
    )
    d = Case(priced_kw={"spec": market, "selection": sel}).run()
    assert (ReasonCode.WAITING_FOR_VETO.value in codes(d)) is expected
    relaxed = Case(priced_kw={"spec": market, "selection": sel}, settings_kw={"prefer_known_veto": False})
    assert ReasonCode.WAITING_FOR_VETO.value not in codes(relaxed.run())


def test_pricing_mismatch_is_caught() -> None:
    priced = make_priced()
    broken = priced.model_copy(update={"outcome": priced.outcome.model_copy(update={"selection": "team2"})})
    match = MatchDecisionInfo(
        match_id=1,
        scheduled_at=T0 + timedelta(hours=3),
        data_completeness=1.0,
        link_confidence=1.0,
        link_status=LinkStatus.CONFIRMED,
    )
    d = evaluate(broken, match=match, settings=DecisionSettings(), betclic=BetclicSettings(), now=NOW)
    assert codes(d) == [ReasonCode.PRICING_MISMATCH.value]


def test_evaluate_requires_aware_now_and_evaluate_all() -> None:
    match = MatchDecisionInfo(
        match_id=1, scheduled_at=T0 + timedelta(hours=3), data_completeness=1.0, link_confidence=1.0,
        link_status=LinkStatus.AUTO,
    )  # fmt: skip
    with pytest.raises(ValueError, match="timezone"):
        evaluate(
            make_priced(),
            match=match,
            settings=DecisionSettings(),
            betclic=BetclicSettings(),
            now=datetime(2026, 11, 20, 18, 1),
        )
    decisions = evaluate_all(
        [make_priced(), make_priced(odds="1.45")],
        match=match,
        settings=DecisionSettings(),
        betclic=BetclicSettings(),
        now=NOW,
    )
    assert [d.accepted for d in decisions] == [True, False]


def test_reason_code_parsing() -> None:
    assert reason_code("low_ev: EV +0.01 < 0.03") == "low_ev"
    assert reason_code("plain") == "plain"


# ------------------------------------------------------------------ DB helpers


def test_load_already_bet_keys(session: Session) -> None:
    m = add_match(session)
    other = add_match(session)
    add_bet(session, match_id=m.id, status=BetStatus.PLACED, spec_key="match_winner", selection_key="team1")
    add_bet(session, match_id=m.id, status=BetStatus.DRY_RUN, dry_run=True, spec_key="map_winner|map=1")
    add_bet(
        session, match_id=m.id, status=BetStatus.FAILED, spec_key="total_maps|line=2.5", selection_key="over"
    )
    add_bet(session, match_id=m.id, status=BetStatus.CANCELLED, spec_key="map_winner|map=2")
    add_bet(session, match_id=other.id, status=BetStatus.WON, spec_key="match_winner", selection_key="team2")
    keys = load_already_bet_keys(session, m.id)
    assert keys == {bet_key(m.id, "match_winner", "team1"), bet_key(m.id, "map_winner|map=1", "team1")}


def test_load_match_decision_info(session: Session) -> None:
    m = add_match(session, scheduled_at=T0 + timedelta(hours=5))
    m.data_completeness = 0.8
    ev = add_event(session, m.id, confidence=0.93, status=LinkStatus.CONFIRMED)
    unlinked = add_event(session, None)
    add_bet(session, match_id=m.id, status=BetStatus.PLACED)
    session.add(
        ContextFlagRow(
            match_id=m.id,
            flag_type=ContextFlagType.STAND_IN,
            severity=0.8,
            confidence=0.9,
            veto=True,
            rationale="AWPer replaced by a stand-in",
            created_at=T0,
        )
    )
    session.add(
        ContextFlagRow(
            match_id=m.id,
            flag_type=ContextFlagType.VISA_ISSUE,
            severity=0.5,
            confidence=0.5,
            veto=True,
            rationale="expired flag",
            created_at=T0 - timedelta(days=2),
            expires_at=T0 - timedelta(days=1),
        )
    )
    session.flush()

    info = load_match_decision_info(session, match_id=m.id, bookmaker_event_id=ev.id, now=NOW)
    assert info.scheduled_at == T0 + timedelta(hours=5)
    assert info.data_completeness == 0.8
    assert (info.link_confidence, info.link_status) == (0.93, LinkStatus.CONFIRMED)
    assert info.veto_known is False
    assert info.context_veto and "stand-in" in (info.context_veto_reason or "")
    assert "expired flag" not in (info.context_veto_reason or "")
    assert info.already_bet_keys == {bet_key(m.id, "match_winner", "team1")}

    # an event that is not linked to this match gives no link at all
    bad = load_match_decision_info(session, match_id=m.id, bookmaker_event_id=unlinked.id, now=NOW)
    assert (bad.link_confidence, bad.link_status) == (None, LinkStatus.UNLINKED)

    # with a prediction: its completeness / veto knowledge / flags are used
    session.add(Veto(match_id=m.id, step=1, action=VetoAction.BAN, map_name="nuke"))
    session.flush()
    session.refresh(m)
    assert load_match_decision_info(session, match_id=m.id, now=NOW).veto_known is True
    pred = make_prediction(match_id=m.id)
    pred = pred.model_copy(
        update={
            "data_completeness": 0.97,
            "veto_known": True,
            "context_flags": [
                ContextFlag(
                    flag_type=ContextFlagType.ILLNESS,
                    team=TeamSlot.TEAM2,
                    severity=0.6,
                    confidence=0.7,
                    veto=True,
                    rationale="IGL hospitalised",
                )
            ],
        }
    )
    info2 = load_match_decision_info(
        session, match_id=m.id, bookmaker_event_id=ev.id, prediction=pred, now=NOW
    )
    assert info2.data_completeness == 0.97 and info2.veto_known is True
    assert "IGL hospitalised" in (info2.context_veto_reason or "")
    with pytest.raises(ValueError, match="does not exist"):
        load_match_decision_info(session, match_id=99999)


def test_decision_info_feeds_evaluate(session: Session) -> None:
    m = add_match(session, scheduled_at=T0 + timedelta(hours=2))
    m.data_completeness = 1.0
    ev = add_event(session, m.id)
    session.flush()
    info = load_match_decision_info(session, match_id=m.id, bookmaker_event_id=ev.id, now=NOW)
    run: Callable[[], Any] = lambda: evaluate(  # noqa: E731
        make_priced(), match=info, settings=DecisionSettings(), betclic=BetclicSettings(), now=NOW
    )
    assert run().accepted
    add_bet(session, match_id=m.id, status=BetStatus.PENDING)
    info = load_match_decision_info(session, match_id=m.id, bookmaker_event_id=ev.id, now=NOW)
    assert codes(run()) == [ReasonCode.DUPLICATE.value]


def test_non_finite_numbers_never_pass() -> None:
    nan = float("nan")
    priced = make_priced()
    broken = priced.model_copy(
        update={"ev": nan, "outcome": priced.outcome.model_copy(update={"sigma": nan})}
    )
    match = MatchDecisionInfo(
        match_id=1,
        scheduled_at=T0 + timedelta(hours=3),
        data_completeness=nan,
        link_confidence=nan,
        link_status=LinkStatus.AUTO,
    )
    d = evaluate(broken, match=match, settings=DecisionSettings(), betclic=BetclicSettings(), now=NOW)
    assert not d.accepted
    assert codes(d) == [
        ReasonCode.INVALID_NUMBERS.value,
        ReasonCode.LOW_EV.value,
        ReasonCode.HIGH_SIGMA.value,
        ReasonCode.LOW_DATA_COMPLETENESS.value,
        ReasonCode.WEAK_EVENT_LINK.value,
    ]
    assert "non-finite sigma, ev" in d.reasons[0]
    nan_p = priced.model_copy(update={"outcome": priced.outcome.model_copy(update={"p_win": nan})})
    d2 = Case().run()
    assert d2.accepted
    match_ok = match.model_copy(update={"data_completeness": 1.0, "link_confidence": 1.0})
    d3 = evaluate(nan_p, match=match_ok, settings=DecisionSettings(), betclic=BetclicSettings(), now=NOW)
    assert codes(d3) == [ReasonCode.INVALID_NUMBERS.value, ReasonCode.LOW_PROBABILITY.value]


# ------------------------------------------------------------------ review round 1 additions


def test_sigma_is_judged_on_the_scale_of_the_settled_probability() -> None:
    """Map 3 of a BO3 is void half of the time: the sigma of the unconditional p_win is only half the
    uncertainty of p(win | settled), the probability the confidence check uses."""
    spec3 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=3)
    kw: dict[str, Any] = {"spec": spec3, "p_win": 0.42, "p_void": 0.5, "odds": "1.80"}
    fine = Case(priced_kw={**kw, "sigma": 0.02}, match_kw={"veto_known": True}).run()
    assert fine.accepted  # 0.02 / 0.5 = 0.04 <= 0.05
    risky = Case(priced_kw={**kw, "sigma": 0.03}, match_kw={"veto_known": True}).run()
    assert codes(risky) == [ReasonCode.HIGH_SIGMA]  # 0.03 / 0.5 = 0.06 > 0.05
    assert "sigma 0.0600 > 0.0500" in risky.reasons[0] and "0.0300" in risky.reasons[0]
    assert settled_sigma(make_outcome(p_win=0.42, p_void=0.5, sigma=0.03)) == pytest.approx(0.06)
    assert settled_sigma(make_outcome(p_win=0.8, sigma=0.03)) == pytest.approx(0.03)
    assert settled_sigma(make_outcome(p_win=0.0, p_void=1.0, sigma=0.01)) == float("inf")


@pytest.mark.parametrize(
    "status", [MatchStatus.LIVE, MatchStatus.FINISHED, MatchStatus.CANCELED, MatchStatus.POSTPONED]
)
def test_only_scheduled_matches_are_bet(status: MatchStatus) -> None:
    d = Case(match_kw={"match_status": status}).run()
    assert codes(d) == [ReasonCode.MATCH_NOT_SCHEDULED] and status.value in d.reasons[0]


@pytest.mark.parametrize("status", [None, MatchStatus.SCHEDULED, MatchStatus.UNKNOWN])
def test_scheduled_or_unknown_match_status_is_fine(status: MatchStatus | None) -> None:
    assert Case(match_kw={"match_status": status}).run().accepted


def test_naive_timestamps_fail_the_rule_instead_of_crashing() -> None:
    naive_quote = Case(priced_kw={"captured_at": T0.replace(tzinfo=None)}).run()
    assert codes(naive_quote) == [ReasonCode.ODDS_STALE] and "timezone" in naive_quote.reasons[0]
    naive_start = Case(match_kw={"scheduled_at": (T0 + timedelta(hours=3)).replace(tzinfo=None)}).run()
    assert codes(naive_start) == [ReasonCode.OUTSIDE_TIME_WINDOW] and "timezone" in naive_start.reasons[0]


@pytest.mark.parametrize(
    "update",
    [
        {"p_win": 1.2},  # would sail through every threshold
        {"p_win": 0.8, "p_void": 0.3},  # sums to 1.1
        {"p_win": 0.8, "p_push": -0.1},
        {"p_win": 0.8, "sigma": -0.01},
    ],
)
def test_invalid_distributions_never_pass(update: dict[str, float]) -> None:
    priced = make_priced()
    broken = priced.model_copy(update={"outcome": priced.outcome.model_copy(update=update)})
    d = evaluate(
        broken,
        match=MatchDecisionInfo(
            match_id=1,
            scheduled_at=T0 + timedelta(hours=3),
            data_completeness=0.99,
            link_confidence=0.97,
            link_status=LinkStatus.AUTO,
        ),
        settings=DecisionSettings(),
        betclic=BetclicSettings(),
        now=NOW,
    )
    assert not d.accepted and ReasonCode.INVALID_NUMBERS in codes(d)


def test_dry_run_bets_do_not_block_real_bets(session: Session) -> None:
    m = add_match(session)
    add_bet(session, match_id=m.id, status=BetStatus.DRY_RUN, dry_run=True, spec_key="map_winner|map=1")
    add_bet(session, match_id=m.id, status=BetStatus.DRY_RUN, dry_run=False, spec_key="map_winner|map=2")
    add_bet(session, match_id=m.id, status=BetStatus.PLACING, spec_key="match_winner")  # unconfirmed
    real = {bet_key(m.id, "match_winner", "team1")}
    dry = {bet_key(m.id, "map_winner|map=1", "team1"), bet_key(m.id, "map_winner|map=2", "team1")}
    assert load_already_bet_keys(session, m.id) == real | dry  # default: a dry run is not repeated
    assert load_already_bet_keys(session, m.id, include_dry_run=False) == real
    info = load_match_decision_info(session, match_id=m.id, include_dry_run_bets=False)
    assert info.already_bet_keys == real


def test_decision_info_uses_the_earlier_start_time_and_the_match_status(session: Session) -> None:
    m = add_match(session, scheduled_at=T0 + timedelta(hours=5))
    ev = add_event(session, m.id)
    ev.starts_at = T0 + timedelta(hours=4)  # Betclic closes the market an hour before our start time
    other = add_event(session, None)
    other.starts_at = T0 + timedelta(hours=1)  # not linked to this match: ignored
    session.flush()
    info = load_match_decision_info(session, match_id=m.id, bookmaker_event_id=ev.id, now=NOW)
    assert info.scheduled_at == T0 + timedelta(hours=4) and info.match_status is MatchStatus.SCHEDULED
    assert load_match_decision_info(
        session, match_id=m.id, bookmaker_event_id=other.id, now=NOW
    ).scheduled_at == T0 + timedelta(hours=5)
    ev.starts_at = T0 + timedelta(hours=6)
    m.status = MatchStatus.LIVE
    session.flush()
    info = load_match_decision_info(session, match_id=m.id, bookmaker_event_id=ev.id, now=NOW)
    assert info.scheduled_at == T0 + timedelta(hours=5) and info.match_status is MatchStatus.LIVE
    d = evaluate(make_priced(), match=info, settings=DecisionSettings(), betclic=BetclicSettings(), now=NOW)
    assert ReasonCode.MATCH_NOT_SCHEDULED in codes(d)
    with pytest.raises(ValueError, match="prediction is for match"):
        load_match_decision_info(session, match_id=m.id, prediction=make_prediction(match_id=m.id + 1))


def test_naive_expiry_on_a_prediction_flag_is_read_as_utc(session: Session) -> None:
    m = add_match(session)
    expired = ContextFlag(
        flag_type=ContextFlagType.ILLNESS,
        severity=0.9,
        confidence=0.9,
        veto=True,
        rationale="was ill last week",
        expires_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
    )
    active = expired.model_copy(
        update={"rationale": "ill today", "expires_at": (NOW + timedelta(hours=1)).replace(tzinfo=None)}
    )
    pred = make_prediction(match_id=m.id).model_copy(update={"context_flags": [expired, active]})
    info = load_match_decision_info(session, match_id=m.id, prediction=pred, now=NOW)
    assert info.context_veto and info.context_veto_reason == "illness: ill today"
