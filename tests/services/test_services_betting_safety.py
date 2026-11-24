"""Review round 1: money-safety regressions of the betting cycle, settlement and the job wiring.

Each test pins one fix: cross-process compare-and-swap of the bet status, stopping a cycle after an
unconfirmed click / a closed executor / a shutdown / a failure to record a result, outcomes whose
unconfirmed placement could not be found are never re-bet, date-only bet-history rows, the
bookmaker's payout as the truth for money, no balance reconciliation without the bet history, alerts
raised once per bet, and per-job sources closed in their lane.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

import cs2bot.db.models as M
from cs2bot.domain.betting import BetOrder, BookmakerBetRecord, BookmakerBetStatus, PlacementStatus
from cs2bot.domain.enums import BetStatus, LedgerKind, MatchStatus
from cs2bot.domain.results import SettlementOutcome
from cs2bot.engine import ledger
from cs2bot.engine.ev import net_payout
from cs2bot.executor import FakeExecutor
from cs2bot.executor.base import ExecutorClosedError
from cs2bot.flows import jobs as jobs_mod
from cs2bot.flows.jobs import Runtime, build_jobs
from cs2bot.services import betting
from cs2bot.services.betting import run_betting_cycle, transition_status
from cs2bot.services.registry import ModelRegistry
from cs2bot.services.settlement import (
    UNCONFIRMED_NOT_FOUND_KEY,
    history_row_latest_time,
    match_history_record,
    plausible_bookmaker_payout,
    settle_bets,
)
from cs2bot.utils.time import WARSAW
from tests.services.conftest import KICKOFF_IN, World
from tests.services.test_services_e2e import (
    MW,
    bets,
    bookmaker_settles_like_reality,
    finish_match,
    ledger_balance,
    live,
    prepare_market,
)


def _audits(world: World, action: str) -> int:
    with world.factory() as s:
        return s.scalar(select(func.count(M.AuditLog.id)).where(M.AuditLog.action == action)) or 0


def _pending_bet(world: World, *, key: str, cycle_at: datetime) -> int:
    with world.factory() as s:
        bet = M.Bet(
            match_id=world.match_ids[0],
            external_event_id="ev",
            market_raw="Zwycięzca meczu",
            selection_raw="team1",
            spec_key=MW,
            selection_key="team1",
            requested_odds=Decimal("1.90"),
            min_acceptable_odds=Decimal("1.80"),
            stake=Decimal("10.00"),
            status=BetStatus.PENDING,
            dry_run=False,
            idempotency_key=key,
            extra={"cycle_at": cycle_at.isoformat()},
        )
        s.add(bet)
        s.commit()
        return bet.id


# --------------------------------------------------------------------------- compare-and-swap


def test_transition_status_is_a_compare_and_swap_across_sessions(world: World) -> None:
    bet_id = _pending_bet(world, key="cas-1", cycle_at=world.now - timedelta(hours=1))
    with world.factory() as mine, world.factory() as other:
        bet = mine.get(M.Bet, bet_id)
        assert bet is not None and bet.status is BetStatus.PENDING
        # another process hands the row to its executor first
        theirs = other.get(M.Bet, bet_id)
        assert theirs is not None
        assert transition_status(other, theirs, expected=BetStatus.PENDING, new=BetStatus.PLACING)
        other.commit()
        # this process's stale-row clean-up must not overwrite it
        assert not transition_status(mine, bet, expected=BetStatus.PENDING, new=BetStatus.CANCELLED)
        mine.commit()
        assert bet.status is BetStatus.PLACING  # refreshed from the database
    assert next(b for b in bets(world) if b.id == bet_id).status is BetStatus.PLACING


def test_cancel_stale_pending_skips_fresh_rows_and_cancels_old_ones(world: World) -> None:
    old = _pending_bet(world, key="stale-old", cycle_at=world.now - timedelta(minutes=10))
    fresh = _pending_bet(world, key="stale-fresh", cycle_at=world.now - timedelta(seconds=30))
    with world.factory() as s:
        assert betting.cancel_stale_pending(s, now=world.now) == 1
        s.commit()
    status = {b.id: b.status for b in bets(world)}
    assert status[old] is BetStatus.CANCELLED and status[fresh] is BetStatus.PENDING


def test_row_cancelled_by_another_process_before_placement_is_never_sent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(world)
    prepare_market(world, world.match_ids[0])
    ex = FakeExecutor(balance=Decimal("1000.00"))
    real = betting._fresh_halt

    def other_process_cancels(factory: Any) -> tuple[bool, str | None]:
        with world.factory() as s:  # e.g. a CLI `bet run` cleaning up "stale" PENDING rows
            for bet in s.scalars(select(M.Bet).where(M.Bet.status == BetStatus.PENDING)):
                bet.status = BetStatus.CANCELLED
            s.commit()
        return real(factory)

    monkeypatch.setattr(betting, "_fresh_halt", other_process_cancels)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert not ex.orders and rep.cancelled == 1 and rep.placed == 0, rep.as_dict()
    assert [b.status for b in bets(world)] == [BetStatus.CANCELLED]
    assert ledger_balance(world) == Decimal("1000.00")


# ------------------------------------------------------------------------------ stopping a cycle


def test_uncertain_placement_stops_the_rest_of_the_cycle_without_pausing(world: World) -> None:
    live(world)
    for mid in world.match_ids[:2]:
        prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    ex.queue(PlacementStatus.UNCERTAIN)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert len(ex.orders) == 1 and rep.uncertain == 1, rep.as_dict()
    assert any("uncertain" in n for n in rep.notes)
    assert rep.paused_reason is None
    with world.factory() as s:
        # the other accepted outcomes are recorded as stopped, not silently dropped
        stopped = [
            d for d in s.scalars(select(M.BetDecisionRow)) if any("placing stopped" in r for r in d.reasons)
        ]
        assert stopped
    # the next cycle carries on (the unconfirmed outcome itself stays blocked)
    rep2 = run_betting_cycle(
        world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=1)
    )
    assert rep2.placed >= 1, rep2.as_dict()
    assert len({o.idempotency_key for o in ex.orders}) == len(ex.orders)


def test_closed_executor_fails_the_bet_without_uncertainty_and_stops(world: World) -> None:
    """At shutdown the executor is closed under a running cycle: nothing was sent -> FAILED, not PLACING."""
    live(world)
    prepare_market(world, world.match_ids[0])
    ex = FakeExecutor(balance=Decimal("1000.00"))

    def closed(order: BetOrder) -> Any:
        raise ExecutorClosedError("this ThreadBoundExecutor was closed")

    ex.queue(closed)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert len(ex.orders) == 1 and rep.failed == 1 and rep.uncertain == 0, rep.as_dict()
    assert [b.status for b in bets(world)] == [BetStatus.FAILED]
    assert rep.paused_reason is None and _audits(world, "bet_uncertain") == 0


def test_shutdown_signal_stops_placing_before_the_next_order(world: World) -> None:
    live(world)
    prepare_market(world, world.match_ids[0])
    ex = FakeExecutor(balance=Decimal("1000.00"))
    stop = {"now": False}
    real = ex.place

    def place_then_ctrl_c(order: BetOrder) -> Any:
        result = real(order)
        stop["now"] = True  # Ctrl+C while the first bet was being placed
        return result

    ex.place = place_then_ctrl_c  # type: ignore[method-assign]
    rep = run_betting_cycle(
        world.factory, world.settings, ex, world.bundle, now=world.now, should_stop=lambda: stop["now"]
    )
    assert rep.placed == 1 and len(ex.orders) == 1, rep.as_dict()
    assert any("shutting down" in n for n in rep.notes)


def test_unrecordable_result_keeps_the_bet_open_and_stops(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(world)
    prepare_market(world, world.match_ids[0])
    ex = FakeExecutor(balance=Decimal("1000.00"))

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(ledger, "apply_placement", broken)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert len(ex.orders) == 1 and rep.errors, rep.as_dict()
    # the bet may exist: it stays PLACING (exposure, never re-sent) until settlement reconciles it
    assert [b.status for b in bets(world)] == [BetStatus.PLACING]
    assert _audits(world, "placement_conflict") == 1
    monkeypatch.undo()
    srep = settle_bets(world.factory, world.settings, ex, now=world.now + timedelta(minutes=5))
    assert srep.placing_confirmed == 1 and ledger_balance(world) == ex.balance


def test_unexpected_error_after_a_bet_row_stops_the_whole_cycle(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(world)
    for mid in world.match_ids[:2]:
        prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bug after the click")

    monkeypatch.setattr(betting, "_summary", boom)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert len(ex.orders) == 1 and rep.errors, rep.as_dict()
    assert any("unexpected error" in n for n in rep.notes)
    # the uncommitted result was rolled back: the bet stays PLACING, settlement confirms it
    assert [b.status for b in bets(world)] == [BetStatus.PLACING]
    monkeypatch.undo()
    srep = settle_bets(world.factory, world.settings, ex, now=world.now + timedelta(minutes=5))
    assert srep.placing_confirmed == 1 and ledger_balance(world) == ex.balance


def test_failed_paper_bet_does_not_use_up_the_match_cap(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paper bet the executor could not even simulate (FAILED) reserves nothing for the next bets."""
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    ex.queue(PlacementStatus.MARKET_UNAVAILABLE)
    seen: list[Decimal] = []
    real = betting.bankroll.size_decision

    def spy(dec: Any, state: Any, *args: Any) -> Any:
        seen.append(state.exposure_by_match.get(mid, Decimal("0")))
        return real(dec, state, *args)

    monkeypatch.setattr(betting.bankroll, "size_decision", spy)
    rep = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert rep.failed == 1 and rep.dry_run_ok >= 1, rep.as_dict()
    assert len(seen) >= 2 and seen[0] == seen[1] == Decimal("0"), seen
    rows = bets(world)
    assert rows[0].status is BetStatus.FAILED
    assert all(b.status is BetStatus.DRY_RUN for b in rows[1:])


# ------------------------------------------------------------------- unconfirmed, then not found


def test_unconfirmed_bet_not_found_is_flagged_and_its_outcome_never_bet_again(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    ex.queue(PlacementStatus.UNCERTAIN)
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    unsure = bets(world)[0]
    assert unsure.status is BetStatus.PLACING
    srep = settle_bets(world.factory, world.settings, ex, now=world.now + timedelta(hours=1))
    assert srep.placing_failed == 1, srep.as_dict()
    row = next(b for b in bets(world) if b.id == unsure.id)
    assert row.status is BetStatus.FAILED and row.extra.get(UNCONFIRMED_NOT_FOUND_KEY) is True

    rep = run_betting_cycle(
        world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=3)
    )
    assert not rep.errors, rep.as_dict()
    sent = [(o.market_raw_name, o.selection_raw_name) for o in ex.orders]
    assert sent.count((unsure.market_raw, unsure.selection_raw)) == 1
    same = [
        b for b in bets(world) if (b.spec_key, b.selection_key) == (unsure.spec_key, unsure.selection_key)
    ]
    assert len(same) == 1
    # another selection of that market is not backed either (it may be a second leg of one market)
    assert {b.selection_key for b in bets(world) if b.spec_key == unsure.spec_key} == {unsure.selection_key}
    with world.factory() as s:
        reasons = [
            r
            for d in s.scalars(
                select(M.BetDecisionRow).where(
                    M.BetDecisionRow.spec_key == unsure.spec_key,
                    M.BetDecisionRow.selection_key == unsure.selection_key,
                )
            )
            for r in d.reasons
        ]
        assert any("could not be confirmed" in r for r in reasons)


def test_history_row_with_date_only_matches_a_later_attempt_the_same_day() -> None:
    attempt = datetime(2026, 11, 22, 17, 31, 40, tzinfo=UTC)  # 18:31 Warsaw
    day_only = datetime(2026, 11, 22, 0, 0, tzinfo=WARSAW).astimezone(UTC)
    minute = datetime(2026, 11, 22, 18, 31, tzinfo=WARSAW).astimezone(UTC)
    assert history_row_latest_time(day_only) == datetime(2026, 11, 23, 0, 0, tzinfo=WARSAW).astimezone(UTC)
    assert history_row_latest_time(minute) == minute + timedelta(minutes=1)
    # the autumn DST day has 25 hours: the whole local day counts
    dst = datetime(2026, 10, 25, 0, 0, tzinfo=WARSAW).astimezone(UTC)
    assert history_row_latest_time(dst) - dst == timedelta(hours=25)

    bet = M.Bet(
        id=1,
        stake=Decimal("10.00"),
        selection_raw="Synth Team 1",
        market_raw="Zwycięzca meczu",
        extra={ledger.PLACEMENT_ATTEMPTED_AT_KEY: attempt.isoformat()},
    )

    def record(placed_at: datetime, ext: str) -> BookmakerBetRecord:
        return BookmakerBetRecord(
            external_bet_id=ext,
            placed_at=placed_at,
            market_name="Zwycięzca meczu",
            selection_name="Synth Team 1",
            stake=Decimal("10.00"),
            status=BookmakerBetStatus.OPEN,
        )

    found, n = match_history_record(bet, [record(day_only, "D1")], claimed=set())
    assert found is not None and found.external_bet_id == "D1" and n == 1
    # a row from the previous day can never be this attempt
    yesterday = day_only - timedelta(days=1)
    assert match_history_record(bet, [record(yesterday, "D0")], claimed=set()) == (None, 0)


# ------------------------------------------------------------------------- settlement and money


def test_bookmaker_payout_is_the_truth_for_money(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    finish_match(world, mid, [("mirage", 13, 7), ("inferno", 13, 10)])
    outcomes = bookmaker_settles_like_reality(world, ex, mid)
    won = [bid for bid, o in outcomes.items() if o is SettlementOutcome.WON]
    if not won:
        pytest.skip("the synthetic favourites lost every bet in this world")
    target = next(b for b in bets(world) if b.id == won[0])
    local = net_payout(target.stake, target.placed_odds, world.settings)
    paid = local - Decimal("1.00")  # e.g. Betclic rounds the winnings tax differently
    for i, rec in enumerate(ex.history):
        if rec.external_bet_id == target.external_bet_id:
            ex.balance = (ex.balance or Decimal(0)) - (rec.payout or Decimal(0)) + paid
            ex.history[i] = rec.model_copy(update={"payout": paid})
    srep = settle_bets(world.factory, world.settings, ex, now=world.now + KICKOFF_IN + timedelta(hours=3))
    assert not srep.errors and srep.reconcile is not None, srep.as_dict()
    assert not srep.reconcile["flagged"], srep.reconcile
    row = next(b for b in bets(world) if b.id == target.id)
    assert row.payout == paid and row.pnl == paid - row.stake
    assert ledger_balance(world) == ex.balance
    with world.factory() as s:
        adj = list(s.scalars(select(M.BankrollLedger).where(M.BankrollLedger.kind == LedgerKind.ADJUSTMENT)))
        assert [a.amount for a in adj] == [Decimal("-1.00")]
        assert not db_state_halted(s)
    assert _audits(world, "payout_mismatch") == 1


def db_state_halted(session: Any) -> bool:
    from cs2bot.db import state as db_state

    return db_state.is_trading_halted(session)[0]


def test_plausible_bookmaker_payout_bounds() -> None:
    bet = M.Bet(stake=Decimal("10.00"), requested_odds=Decimal("2.00"), placed_odds=Decimal("2.10"))
    won, lost, void = SettlementOutcome.WON, SettlementOutcome.LOST, SettlementOutcome.VOID
    assert plausible_bookmaker_payout(bet, won, Decimal("18.48"))
    assert plausible_bookmaker_payout(bet, won, Decimal("21.00"))
    assert not plausible_bookmaker_payout(bet, won, Decimal("21.02"))  # more than stake x odds
    assert not plausible_bookmaker_payout(bet, won, Decimal("0"))
    assert plausible_bookmaker_payout(bet, lost, Decimal("0"))
    assert not plausible_bookmaker_payout(bet, lost, Decimal("5"))
    assert plausible_bookmaker_payout(bet, void, Decimal("8.80"))
    assert not plausible_bookmaker_payout(bet, void, Decimal("10.01"))
    assert not plausible_bookmaker_payout(bet, void, Decimal("NaN"))


def test_implausible_bookmaker_payout_is_not_booked(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    target = bets(world)[0]
    finish_match(world, mid, [("mirage", 13, 7), ("inferno", 13, 10)])
    # the history row says LOST but shows a payout (a misread row): the ledger must not follow it
    ex.settle(target.external_bet_id or "", BookmakerBetStatus.LOST, Decimal("0"))
    ex.history = [
        r.model_copy(update={"payout": Decimal("999.00")})
        if r.external_bet_id == target.external_bet_id
        else r
        for r in ex.history
    ]
    srep = settle_bets(
        world.factory, world.settings, ex, now=world.now + KICKOFF_IN + timedelta(hours=3), check_health=False
    )
    row = next(b for b in bets(world) if b.id == target.id)
    assert row.status is BetStatus.LOST and row.pnl == -row.stake and row.payout == Decimal("0")
    assert _audits(world, "payout_mismatch") == 1, srep.as_dict()


def test_no_balance_reconciliation_without_the_bet_history(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    # Betclic has already credited a win the bot cannot see: the history page is broken
    for rec in list(ex.history):
        ex.settle(rec.external_bet_id or "", BookmakerBetStatus.WON, Decimal("25.00"))

    def broken(since: datetime) -> list[BookmakerBetRecord]:
        raise RuntimeError("history selectors changed")

    ex.fetch_bet_history = broken  # type: ignore[method-assign]
    before = ledger_balance(world)
    srep = settle_bets(world.factory, world.settings, ex, now=world.now + timedelta(hours=1))
    assert srep.history_failed and srep.reconcile is None, srep.as_dict()
    assert any("reconciliation skipped" in n for n in srep.notes)
    assert ledger_balance(world) == before
    with world.factory() as s:
        assert not db_state_halted(s)


def test_unknown_settlement_is_alerted_once_not_every_pass(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    with world.factory() as s:
        ledger.ensure_initialized(s, world.settings, at=world.now)
        bet = M.Bet(
            match_id=mid,
            external_event_id="ev",
            market_raw="?",
            selection_raw="?",
            spec_key="garbage-spec",
            selection_key="team1",
            requested_odds=Decimal("1.90"),
            min_acceptable_odds=Decimal("1.80"),
            stake=Decimal("5.00"),
            status=BetStatus.PLACED,
            dry_run=False,
            placed_odds=Decimal("1.90"),
            placed_at=world.now,
            idempotency_key="unknown-1",
        )
        s.add(bet)
        match = s.get(M.Match, mid)
        assert match is not None
        match.status = MatchStatus.FINISHED
        s.commit()
    for k in range(3):
        rep = settle_bets(world.factory, world.settings, None, now=world.now + timedelta(hours=8 + k))
        assert rep.unknown == 1, rep.as_dict()
    assert _audits(world, "settlement_unknown") == 1
    assert next(b for b in bets(world) if b.idempotency_key == "unknown-1").status is BetStatus.PLACED


# ------------------------------------------------------------------------------------- job wiring


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_per_job_sources_are_closed_in_their_lane(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[_Closable] = []

    def factory() -> _Closable:
        made.append(_Closable())
        return made[-1]

    seen: list[Any] = []

    def fake_hltv_upcoming(session_factory: Any, settings: Any, *, fetcher: Any) -> Any:
        seen.append(fetcher)
        from cs2bot.domain.sync import SyncReport

        return SyncReport(job="hltv_upcoming", created=1)

    monkeypatch.setattr(jobs_mod.ingestion, "hltv_upcoming", fake_hltv_upcoming)
    rt = Runtime(
        settings=world.settings,
        session_factory=world.factory,
        registry=ModelRegistry(world.settings, world.bundle),
        hltv_fetcher_factory=factory,
    )
    job = next(j for j in build_jobs(rt) if j.name == "ingest_upcoming_hltv")
    from cs2bot.db.jobs import track_job

    for _ in range(2):
        with track_job(job.name, world.factory) as tracker:
            job.func(tracker)
    assert len(made) == 2 and all(m.closed for m in made) and seen == made


def test_bet_job_passes_the_shutdown_event(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_cycle(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return betting.BettingReport(started_at=world.now, dry_run=True)

    monkeypatch.setattr(betting, "run_betting_cycle", fake_cycle)
    rt = Runtime(
        settings=world.settings,
        session_factory=world.factory,
        registry=ModelRegistry(world.settings, world.bundle),
    )
    job = next(j for j in build_jobs(rt) if j.name == "bet_run")
    from cs2bot.db.jobs import track_job

    with track_job(job.name, world.factory) as tracker:
        job.func(tracker)
    should_stop = captured["should_stop"]
    assert should_stop() is False
    rt.stop_event.set()
    assert should_stop() is True
