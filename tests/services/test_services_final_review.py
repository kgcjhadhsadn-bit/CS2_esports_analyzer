"""Final review (money path): the betting cycle never prices a prediction that no longer fits the match.

The quotes are mapped in the orientation of the match row NOW, the stored prediction in the orientation of
the match row when it was made. Before the fix a prediction stayed bettable for up to 8 hours after a
source replaced a participant, changed the format, the match left the Tier 1/2 scope, or a re-prediction
failed / was skipped because the lineups disappeared.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

import cs2bot.db.models as M
from cs2bot.db import state as db_state
from cs2bot.domain.betting import PlacementStatus
from cs2bot.domain.enums import BetStatus, MatchFormat
from cs2bot.executor import FakeExecutor
from cs2bot.services import prediction as prediction_mod
from cs2bot.services.betting import run_betting_cycle
from cs2bot.services.prediction import STALE_KEY, predict_upcoming, stale_reason
from cs2bot.services.settlement import settle_bets
from tests.services.conftest import KICKOFF_IN, World
from tests.services.test_services_e2e import bets, finish_match, live, prepare_market


def _cycle(world: World, ex: FakeExecutor, minutes: int = 0):  # type: ignore[no-untyped-def]
    return run_betting_cycle(
        world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(minutes=minutes)
    )


def test_prediction_for_replaced_teams_is_never_bet(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    with world.factory() as s:
        match = s.get(M.Match, mid)
        assert match is not None
        match.team2_id = 7  # a source replaced a participant after the prediction was made
        s.commit()
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = _cycle(world, ex)
    assert not ex.orders and not bets(world), rep.as_dict()
    assert any("prediction is for teams" in n for n in rep.notes), rep.notes


def test_prediction_for_another_format_is_never_bet(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    with world.factory() as s:
        match = s.get(M.Match, mid)
        assert match is not None
        match.format = MatchFormat.BO1
        s.commit()
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = _cycle(world, ex)
    assert not ex.orders and any("bo1" in n.lower() for n in rep.notes), rep.notes


def test_match_that_left_the_scope_is_not_bet(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    with world.factory() as s:
        match = s.get(M.Match, mid)
        assert match is not None
        match.in_scope = False
        s.commit()
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = _cycle(world, ex)
    assert rep.matches_considered == 0 and not ex.orders


def test_skipped_re_prediction_blocks_the_old_prediction_until_a_new_one_exists(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    real_prepare = prediction_mod.prepare_match

    def without_lineups(*args: Any, **kwargs: Any) -> prediction_mod.PreparedMatch:
        prepared = real_prepare(*args, **kwargs)
        ctx = prepared.ctx.model_copy(update={"team1_lineup": []})
        return dataclasses.replace(prepared, ctx=ctx)

    monkeypatch.setattr(prediction_mod, "prepare_match", without_lineups)
    later = world.now + timedelta(minutes=1)
    report = predict_upcoming(world.factory, world.settings, world.bundle, now=later, match_ids=[mid])
    assert report.skipped == 1, report.as_dict()
    with world.factory() as s:
        match = s.get(M.Match, mid)
        assert match is not None and stale_reason(match) == "lineups not known"
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = _cycle(world, ex, minutes=2)
    assert not ex.orders and any("out of date" in n for n in rep.notes), rep.notes

    # the lineups are known again: the unchanged fingerprint makes the stored prediction valid again
    monkeypatch.setattr(prediction_mod, "prepare_match", real_prepare)
    report = predict_upcoming(
        world.factory, world.settings, world.bundle, now=world.now + timedelta(minutes=3), match_ids=[mid]
    )
    assert report.unchanged + report.predicted == 1, report.as_dict()
    with world.factory() as s:
        match = s.get(M.Match, mid)
        assert match is not None and stale_reason(match) is None
        assert STALE_KEY not in (match.extra or {}).get("prediction", {})
    rep = _cycle(world, ex, minutes=4)
    assert ex.orders and rep.placed >= 1, rep.as_dict()


def test_failed_re_prediction_blocks_the_old_prediction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulator exploded")

    monkeypatch.setattr(prediction_mod, "_predict_one", boom)
    report = predict_upcoming(
        world.factory, world.settings, world.bundle, now=world.now + timedelta(minutes=1), match_ids=[mid]
    )
    assert report.failed == 1
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = _cycle(world, ex, minutes=2)
    assert not ex.orders and any("prediction failed" in n for n in rep.notes), rep.notes


def test_second_unresolved_uncertain_placement_pauses_trading(world: World) -> None:
    live(world)
    first, second = world.match_ids[0], world.match_ids[1]
    prepare_market(world, first)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    ex.queue(PlacementStatus.UNCERTAIN)
    rep = _cycle(world, ex)
    assert rep.uncertain == 1 and rep.paused_reason is None, rep.as_dict()
    with world.factory() as s:
        assert not db_state.is_trading_halted(s)[0]  # one unconfirmed click: the next cycle may try again

    prepare_market(world, second)
    ex.queue(PlacementStatus.UNCERTAIN)
    rep = _cycle(world, ex, minutes=1)
    assert rep.uncertain == 1 and rep.paused_reason and "unconfirmed" in rep.paused_reason, rep.as_dict()
    n_orders = len(ex.orders)
    with world.factory() as s:
        assert db_state.is_trading_halted(s)[0]
    rep = _cycle(world, ex, minutes=2)
    assert len(ex.orders) == n_orders and rep.bets_created == 0  # nothing more until the owner resumes
    assert [b.status for b in bets(world)] == [BetStatus.PLACING, BetStatus.PLACING]


def test_uncertain_after_the_first_one_was_resolved_does_not_pause(world: World) -> None:
    live(world)
    first, second = world.match_ids[0], world.match_ids[1]
    prepare_market(world, first)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    ex.queue(PlacementStatus.UNCERTAIN)
    _cycle(world, ex)
    with world.factory() as s:  # settlement found it in the history meanwhile
        bet = s.scalar(select(M.Bet))
        assert bet is not None and bet.status is BetStatus.PLACING
        bet.status = BetStatus.PLACED
        s.commit()
    prepare_market(world, second)
    ex.queue(PlacementStatus.UNCERTAIN)
    rep = _cycle(world, ex, minutes=1)
    assert rep.uncertain == 1 and rep.paused_reason is None, rep.as_dict()


def test_local_settlement_refuses_a_bet_whose_match_teams_changed(world: World) -> None:
    live(world)
    mid = world.match_ids[0]
    prepare_market(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    rep = _cycle(world, ex)
    assert rep.placed >= 1, rep.as_dict()
    placed = bets(world)
    assert all(b.extra["teams_at_placement"] == [1, 2] for b in placed)
    finish_match(world, mid, [("mirage", 13, 7), ("inferno", 13, 10)])
    with world.factory() as s:
        match = s.get(M.Match, mid)
        assert match is not None
        match.team2_id = 7  # a source re-pointed the match after the bets were placed
        s.commit()
    srep = settle_bets(world.factory, world.settings, None, now=world.now + KICKOFF_IN + timedelta(hours=3))
    assert srep.settled_real == 0 and srep.unknown == len(placed), srep.as_dict()
    assert all(b.status is BetStatus.PLACED for b in bets(world))
    assert any("teams changed" in a for a in srep.alerts), srep.alerts
