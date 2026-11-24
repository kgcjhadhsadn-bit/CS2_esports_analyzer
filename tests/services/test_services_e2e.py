"""End to end: history -> train -> predict -> bet (dry run and live with the fake executor) -> settle.

Every safety property of the betting / settlement services is exercised here against the real engine,
the real database schema and the fake executor: ledger arithmetic, statuses, CLV, idempotency (two
cycles, crashes before and after the placement was handed to the executor), kill switch, pause,
loss-limit pause, LOGIN_REQUIRED / SELECTOR_ERROR pause, UNCERTAIN handling and the exposure caps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

import cs2bot.db.models as M
from cs2bot import datasets
from cs2bot.db import state as db_state
from cs2bot.domain.betting import BetOrder, BookmakerBetRecord, BookmakerBetStatus, PlacementStatus
from cs2bot.domain.enums import BetStatus, LedgerKind, MatchStatus
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.results import SettlementOutcome
from cs2bot.engine import ledger, settlement
from cs2bot.engine.ev import net_payout
from cs2bot.executor import FakeExecutor
from cs2bot.services import betting
from cs2bot.services.betting import run_betting_cycle
from cs2bot.services.prediction import predict_upcoming
from cs2bot.services.settlement import settle_bets
from cs2bot.utils.time import utcnow
from tests.services.conftest import KICKOFF_IN, World, fair_odds

MW = MarketSpec(market_type=MarketType.MATCH_WINNER).key()
CMS = MarketSpec(market_type=MarketType.CORRECT_MAP_SCORE).key()
TM = MarketSpec(market_type=MarketType.TOTAL_MAPS, line=2.5).key()
MAP1 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=1).key()


class Crash(BaseException):
    """Simulates the process dying (not an Exception: nothing may swallow it)."""


def _p(world: World, match_id: int, spec_key: str, sel: str) -> float:
    with world.factory() as s:
        row = s.scalar(
            select(M.Prediction).where(
                M.Prediction.match_id == match_id,
                M.Prediction.spec_key == spec_key,
                M.Prediction.selection_key == sel,
                M.Prediction.is_latest.is_(True),
            )
        )
        assert row is not None, (spec_key, sel)
        return row.p_win


def prepare_market(world: World, match_id: int, *, ev: float = 0.15) -> dict[str, str]:
    """Predict, then price the market so the model's favourites carry ``ev``; the rest is unattractive.

    Returns the favoured selection per market.
    """
    at = world.now - timedelta(minutes=1)
    base = {(MW, "team1"): "1.90", (MW, "team2"): "1.90", (TM, "over"): "1.90", (TM, "under"): "1.90"}
    base |= {(CMS, s): "3.50" for s in ("2-0", "2-1", "1-2", "0-2")}
    base |= {(MAP1, "team1"): "1.90", (MAP1, "team2"): "1.90"}
    world.set_odds(match_id, base, at=at)
    report = predict_upcoming(world.factory, world.settings, world.bundle, now=world.now, match_ids=[match_id])
    assert report.predicted == 1, report.as_dict()
    fav: dict[str, str] = {}
    odds: dict[tuple[str, str], Decimal] = {}
    for spec_key, sels in ((MW, ["team1", "team2"]), (TM, ["over", "under"]), (MAP1, ["team1", "team2"])):
        ps = {s: _p(world, match_id, spec_key, s) for s in sels}
        best = max(ps, key=lambda k: ps[k])
        fav[spec_key] = best
        for s in sels:
            odds[(spec_key, s)] = fair_odds(ps[s], ev if s == best else -0.3)
    cms = {s: _p(world, match_id, CMS, s) for s in ("2-0", "2-1", "1-2", "0-2")}
    ranked = sorted(cms, key=lambda k: cms[k], reverse=True)
    fav[CMS] = ranked[0]
    for i, s in enumerate(ranked):
        # two selections of the same market with positive EV: at most one may ever be backed
        odds[(CMS, s)] = fair_odds(cms[s], ev if i == 0 else (ev / 2 if i == 1 else -0.3))
    world.set_odds(match_id, odds, at=world.now)
    return fav


def finish_match(world: World, match_id: int, maps: list[tuple[str, int, int]]) -> None:
    """Store the final result: (map name, team1 rounds, team2 rounds) per played map."""
    with world.factory() as s:
        match = s.get(M.Match, match_id)
        assert match is not None
        t1 = sum(1 for _, a, b in maps if a > b)
        t2 = len(maps) - t1
        match.status = MatchStatus.FINISHED
        match.team1_score, match.team2_score = t1, t2
        match.winner_team_id = match.team1_id if t1 > t2 else match.team2_id
        for k, (name, a, b) in enumerate(maps, start=1):
            s.add(
                M.MapPlayed(
                    match_id=match_id,
                    map_number=k,
                    map_name=name,
                    status=MatchStatus.FINISHED,
                    team1_rounds=a,
                    team2_rounds=b,
                    winner_team_id=match.team1_id if a > b else match.team2_id,
                    played_at=world.now + KICKOFF_IN + timedelta(hours=k - 1),
                )
            )
        s.commit()


def live(world: World) -> None:
    world.settings.executor.dry_run = False


def bets(world: World) -> list[M.Bet]:
    with world.factory() as s:
        return list(s.scalars(select(M.Bet).order_by(M.Bet.id)))


def ledger_balance(world: World) -> Decimal:
    with world.factory() as s:
        return ledger.current_balance(s)


def bookmaker_settles_like_reality(world: World, ex: FakeExecutor, match_id: int) -> dict[int, SettlementOutcome]:
    """Settle the fake bookmaker's open bets from the stored match result (what Betclic would do)."""
    out: dict[int, SettlementOutcome] = {}
    with world.factory() as s:
        actuals = datasets.load_match_actuals(s, match_id)
        assert actuals is not None and actuals.complete
        for bet in s.scalars(select(M.Bet).where(M.Bet.match_id == match_id, M.Bet.status == BetStatus.PLACED)):
            res = settlement.resolve_bet(bet, actuals, betclic=world.settings.betclic)
            out[bet.id] = res.outcome
            assert bet.external_bet_id
            if res.outcome is SettlementOutcome.WON:
                ex.settle(bet.external_bet_id, BookmakerBetStatus.WON, net_payout(bet.stake, bet.placed_odds, world.settings))
            elif res.outcome is SettlementOutcome.LOST:
                ex.settle(bet.external_bet_id, BookmakerBetStatus.LOST, Decimal("0"))
            else:
                ex.settle(bet.external_bet_id, BookmakerBetStatus.VOID, bet.stake)
    return out


# --------------------------------------------------------------------------------------- dry run


def test_dry_run_cycle_records_paper_bets_and_settles_on_paper(world: World) -> None:
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert rep.dry_run and not rep.errors, rep.as_dict()
    assert rep.bets_created >= 2 and rep.dry_run_ok == rep.bets_created
    rows = bets(world)
    assert rows and all(b.status is BetStatus.DRY_RUN and b.dry_run for b in rows)
    assert all(o.dry_run for o in ex.orders)
    with world.factory() as s:
        assert [e.kind for e in s.scalars(select(M.BankrollLedger))] == [LedgerKind.DEPOSIT]
        decisions = list(s.scalars(select(M.BetDecisionRow)))
        assert any(not d.accepted and any("waiting_for_veto" in r for r in d.reasons) for d in decisions)
        assert all(d.prediction_id is not None for d in decisions)
        # paper stakes are capped per match too
        assert sum(b.stake for b in rows) <= Decimal("40.00")

    # a second dry-run cycle does not repeat the paper bets
    rep2 = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=1))
    assert rep2.bets_created == 0 and len(bets(world)) == len(rows)

    finish_match(world, mid, [("mirage", 13, 7), ("inferno", 13, 10)])
    srep = settle_bets(world.factory, world.settings, None, now=world.now + KICKOFF_IN + timedelta(hours=3))
    assert srep.settled_paper == len(rows) and not srep.errors, srep.as_dict()
    for b in bets(world):
        assert b.status is BetStatus.DRY_RUN and b.settled_at is not None and b.pnl is not None
        assert b.extra["settled_outcome"] in ("won", "lost", "void", "push")
        assert b.clv is not None  # the opening placeholder snapshot is before kick-off too
    assert ledger_balance(world) == Decimal("1000.00")


# ------------------------------------------------------------------------------------- live money


def test_live_cycle_full_lifecycle_ledger_clv_and_reconcile(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    fav = prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert not rep.dry_run and not rep.errors and rep.placed >= 2, rep.as_dict()
    placed = bets(world)
    assert all(b.status is BetStatus.PLACED and not b.dry_run and b.external_bet_id for b in placed)
    # per-bet and per-match caps (bankroll 1000: 2% per bet, 4% per match)
    assert all(b.stake <= Decimal("20.00") for b in placed)
    assert sum(b.stake for b in placed) <= Decimal("40.00")
    # never two selections of one market
    per_market: dict[str, set[str]] = defaultdict(set)
    for b in placed:
        per_market[b.spec_key].add(b.selection_key)
    assert all(len(v) == 1 for v in per_market.values())
    # orders carry the kick-off and a min acceptable odds not above the quote
    for o in ex.orders:
        assert o.event_starts_at is not None and o.min_acceptable_odds <= o.requested_odds
        assert not o.dry_run
    stakes = sum(b.stake for b in placed)
    assert ledger_balance(world) == Decimal("1000.00") - stakes
    assert ex.balance == Decimal("1000.00") - stakes

    # idempotency: the next cycles place nothing new
    for k in (1, 2):
        again = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=k))
        assert again.bets_created == 0, again.as_dict()
    assert len(bets(world)) == len(placed) and len(ex.orders) == len(placed)

    # the price of the backed match-winner selection drops before kick-off -> positive CLV
    our = next(b for b in placed if b.spec_key == MW)
    assert our.selection_key == fav[MW]
    closing = (our.requested_odds * Decimal("0.9")).quantize(Decimal("0.01"))
    world.set_odds(mid, {(MW, our.selection_key): closing}, at=world.now + KICKOFF_IN - timedelta(minutes=20))

    finish_match(world, mid, [("mirage", 13, 7), ("inferno", 13, 10)])
    outcomes = bookmaker_settles_like_reality(world, ex, mid)
    after = world.now + KICKOFF_IN + timedelta(hours=3)
    srep = settle_bets(world.factory, world.settings, ex, now=after)
    assert not srep.errors and srep.settled_real == len(placed), srep.as_dict()
    assert srep.disagreements == 0 and srep.reconcile is not None and not srep.reconcile["flagged"]
    payouts = Decimal("0")
    for b in bets(world):
        expected = outcomes[b.id]
        assert b.status.value == ("void" if expected.is_refund else expected.value)
        assert b.settled_at is not None and b.pnl is not None
        if b.status is BetStatus.WON:
            payouts += net_payout(b.stake, b.placed_odds, world.settings)
            assert b.pnl == net_payout(b.stake, b.placed_odds, world.settings) - b.stake
        elif b.status is BetStatus.LOST:
            assert b.pnl == -b.stake
        assert b.closing_odds is not None and b.clv is not None
    mw = next(b for b in bets(world) if b.id == our.id)
    assert mw.closing_odds == closing
    assert mw.clv == pytest.approx(float(mw.placed_odds / closing - 1))
    assert ledger_balance(world) == Decimal("1000.00") - stakes + payouts == ex.balance
    with world.factory() as s:
        assert not db_state.is_trading_halted(s)[0]
        assert s.get(M.SystemState, db_state.EXECUTOR_HEALTH) is not None

    # settling again changes nothing
    again = settle_bets(world.factory, world.settings, ex, now=after + timedelta(minutes=30))
    assert again.settled_real == 0 and ledger_balance(world) == ex.balance


def test_bookmaker_settlement_wins_over_local_result_and_is_flagged(world: World) -> None:
    live(world)
    mid = world.match_ids[1]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    placed = bets(world)
    assert placed
    finish_match(world, mid, [("mirage", 13, 7), ("inferno", 13, 10)])
    outcomes = bookmaker_settles_like_reality(world, ex, mid)
    # flip the first bet at the "bookmaker": it says the opposite of our result
    first = placed[0]
    real = outcomes[first.id]
    flipped = BookmakerBetStatus.LOST if real is SettlementOutcome.WON else BookmakerBetStatus.WON
    pay = Decimal("0") if flipped is BookmakerBetStatus.LOST else net_payout(first.stake, first.placed_odds, world.settings)
    for i, rec in enumerate(ex.history):
        if rec.external_bet_id == first.external_bet_id:
            ex.balance = (ex.balance or Decimal(0)) - (rec.payout or Decimal(0)) + pay
            ex.history[i] = rec.model_copy(update={"status": flipped, "payout": pay})
    srep = settle_bets(world.factory, world.settings, ex, now=world.now + KICKOFF_IN + timedelta(hours=3))
    assert srep.disagreements == 1, srep.as_dict()
    row = next(b for b in bets(world) if b.id == first.id)
    assert row.status.value == flipped.value
    assert ledger_balance(world) == ex.balance
    with world.factory() as s:
        assert s.scalar(select(M.AuditLog).where(M.AuditLog.action == "settlement_disagreement")) is not None


# ------------------------------------------------------------------------------------ crash safety


def test_crash_after_handing_the_order_to_the_executor_never_double_bets(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))

    def place_then_die(order: BetOrder) -> Any:
        # the bookmaker accepted the bet, then the bot process died before recording the answer
        ex.history.append(
            BookmakerBetRecord(
                external_bet_id="BC-777",
                placed_at=utcnow(),
                event_name=order.external_event_id,
                market_name=order.market_raw_name,
                selection_name=order.selection_raw_name,
                odds=order.requested_odds,
                stake=order.stake,
                status=BookmakerBetStatus.OPEN,
            )
        )
        ex.balance = (ex.balance or Decimal(0)) - order.stake
        raise Crash()

    ex.queue(place_then_die)
    with pytest.raises(Crash):
        run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    rows = bets(world)
    assert len(rows) == 1 and rows[0].status is BetStatus.PLACING
    crashed = rows[0]

    # restart: the PLACING bet blocks its outcome; other outcomes may be bet, the crashed one never again
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=3))
    assert not rep.errors, rep.as_dict()
    keys = [(o.market_raw_name, o.selection_raw_name) for o in ex.orders]
    assert keys.count((crashed.market_raw, crashed.selection_raw)) == 1
    # exposure counts the unconfirmed bet: the match cap still holds
    assert sum(b.stake for b in bets(world)) <= Decimal("40.00")

    # settlement finds it in the bet history and confirms it with the bookmaker's id
    srep = settle_bets(world.factory, world.settings, ex, now=world.now + timedelta(minutes=10))
    assert srep.placing_confirmed == 1, srep.as_dict()
    row = next(b for b in bets(world) if b.id == crashed.id)
    assert row.status is BetStatus.PLACED and row.external_bet_id == "BC-777"
    assert ledger_balance(world) == ex.balance


def test_crash_before_the_order_was_handed_over_is_cancelled_and_redecided(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    calls = {"n": 0}
    real = betting._fresh_halt

    def die_once(factory: Any) -> tuple[bool, str | None]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Crash()
        return real(factory)

    monkeypatch.setattr(betting, "_fresh_halt", die_once)
    with pytest.raises(Crash):
        run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    rows = bets(world)
    assert len(rows) == 1 and rows[0].status is BetStatus.PENDING and not ex.orders

    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=5))
    assert rep.stale_pending_cancelled == 1 and rep.placed >= 1, rep.as_dict()
    first = next(b for b in bets(world) if b.id == rows[0].id)
    assert first.status is BetStatus.CANCELLED
    # the outcome was decided again under a new key and sent exactly once
    redo = [b for b in bets(world) if b.spec_key == first.spec_key and b.selection_key == first.selection_key]
    assert len(redo) == 2 and redo[1].idempotency_key != first.idempotency_key
    keys = [(o.market_raw_name, o.selection_raw_name) for o in ex.orders]
    assert keys.count((first.market_raw, first.selection_raw)) == 1
    assert len({o.idempotency_key for o in ex.orders}) == len(ex.orders)


# ------------------------------------------------------------------------------------ safety stops


def test_kill_switch_and_pause_block_every_placement(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    with world.factory() as s:
        db_state.set_kill_switch(s, True, actor="test", reason="owner")
        s.commit()
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert rep.bets_created == 0 and not ex.orders and rep.halted_reason
    with world.factory() as s:
        accepted_before_sizing = list(s.scalars(select(M.BetDecisionRow).where(M.BetDecisionRow.reasons.is_not(None))))
        assert any(any("halted" in r for r in d.reasons) for d in accepted_before_sizing)
        db_state.set_kill_switch(s, False, actor="test")
        db_state.set_paused(s, True, actor="test", reason="manual")
        s.commit()
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=1))
    assert rep.bets_created == 0 and not ex.orders and rep.paused_reason == "manual"
    with world.factory() as s:
        db_state.set_paused(s, False, actor="test")
        s.commit()
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=2))
    assert rep.placed >= 1


def test_kill_switch_switched_on_mid_cycle_stops_before_the_next_placement(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    real = ex.place

    def place_and_hit_kill_switch(order: BetOrder) -> Any:
        result = real(order)
        with world.factory() as s:  # the owner presses the kill switch right after the first bet
            db_state.set_kill_switch(s, True, actor="dashboard", reason="panic")
            s.commit()
        return result

    monkeypatch.setattr(ex, "place", place_and_hit_kill_switch)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert rep.placed == 1 and rep.cancelled == 1, rep.as_dict()
    statuses = sorted(b.status.value for b in bets(world))
    assert statuses == ["cancelled", "placed"]
    assert len(ex.orders) == 1


def test_loss_limit_pauses_trading(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    with world.factory() as s:
        ledger.ensure_initialized(s, world.settings, at=world.now - timedelta(days=2))
        loser = M.Bet(
            match_id=world.match_ids[2],
            external_event_id="old",
            market_raw="x",
            selection_raw="y",
            spec_key=MW,
            selection_key="team1",
            requested_odds=Decimal("1.80"),
            min_acceptable_odds=Decimal("1.70"),
            stake=Decimal("60.00"),
            status=BetStatus.PLACING,
            dry_run=False,
            idempotency_key="old-loser",
        )
        s.add(loser)
        s.flush()
        from cs2bot.domain.betting import PlacementResult

        ledger.apply_placement(
            s,
            loser,
            PlacementResult(status=PlacementStatus.PLACED, external_bet_id="X1", at=world.now - timedelta(hours=2)),
            world.settings,
        )
        ledger.settle_bet(s, loser, "lost", world.settings, at=world.now - timedelta(hours=1))
        s.commit()
    ex = FakeExecutor(balance=Decimal("940.00"))
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert rep.loss_limit_hit and rep.paused_reason and not ex.orders, rep.as_dict()
    with world.factory() as s:
        halted, reason = db_state.is_trading_halted(s)
        assert halted and reason and "loss" in reason.lower()


@pytest.mark.parametrize(
    ("status", "word"),
    [(PlacementStatus.LOGIN_REQUIRED, "login"), (PlacementStatus.SELECTOR_ERROR, "selector")],
)
def test_login_required_and_selector_error_pause_trading(world: World, status: PlacementStatus, word: str) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    ex.queue(status)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert len(ex.orders) == 1, "placing must stop after the first failure of this kind"
    assert rep.failed == 1 and rep.paused_reason and word in rep.paused_reason.lower(), rep.as_dict()
    assert [b.status for b in bets(world)] == [BetStatus.FAILED]
    with world.factory() as s:
        assert db_state.is_trading_halted(s)[0]
        assert ledger.current_balance(s) == Decimal("1000.00")


def test_uncertain_placement_stays_open_counts_as_exposure_and_is_failed_after_grace(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    ex.queue(PlacementStatus.UNCERTAIN)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert rep.uncertain == 1, rep.as_dict()
    unsure = bets(world)[0]
    assert unsure.status is BetStatus.PLACING
    with world.factory() as s:
        from cs2bot.engine import bankroll

        state = bankroll.compute_state(s, world.settings, now=world.now)
        assert state.exposure_by_match.get(mid, Decimal(0)) >= unsure.stake
        assert s.scalar(select(M.AuditLog).where(M.AuditLog.action == "bet_uncertain")) is not None
    n_orders = len(ex.orders)
    rep2 = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=2))
    assert all(o.idempotency_key != unsure.idempotency_key for o in ex.orders[n_orders:])
    assert rep2.bets_created == 0 or all(b.selection_raw != unsure.selection_raw for b in bets(world)[1:])

    # not in the history yet, inside the grace period: stays open
    s1 = settle_bets(world.factory, world.settings, ex, now=world.now + timedelta(minutes=5))
    assert s1.placing_open == 1 and next(b for b in bets(world) if b.id == unsure.id).status is BetStatus.PLACING
    # still absent after the grace period: FAILED, exposure released, owner alerted
    s2 = settle_bets(world.factory, world.settings, ex, now=world.now + timedelta(hours=1))
    assert s2.placing_failed == 1, s2.as_dict()
    assert next(b for b in bets(world) if b.id == unsure.id).status is BetStatus.FAILED
    with world.factory() as s:
        assert s.scalar(select(M.AuditLog).where(M.AuditLog.action == "placing_not_found")) is not None


def test_no_executor_records_decisions_only(world: World) -> None:
    live(world)
    prepare_market(world, world.match_ids[0])
    rep = run_betting_cycle(world.factory, world.settings, None, world.bundle, now=world.now)
    assert rep.bets_created == 0 and rep.decisions > 0 and not bets(world)
    with world.factory() as s:
        assert any(
            any("no_executor" in r for r in d.reasons) for d in s.scalars(select(M.BetDecisionRow))
        )


def test_stale_prediction_or_other_model_version_is_not_bet(world: World) -> None:
    live(world)
    prepare_market(world, world.match_ids[0])
    ex = FakeExecutor(balance=Decimal("1000.00"))
    later = world.now + timedelta(hours=9)
    world.set_odds(world.match_ids[0], {(MW, "team1"): "1.50"}, at=later)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=later)
    assert rep.matches_considered == 0 and not ex.orders
    other = type(world.bundle)(**{**world.bundle.__dict__, "version": "other"})
    rep = run_betting_cycle(world.factory, world.settings, ex, other, now=world.now)
    assert rep.matches_considered == 0 and not ex.orders
