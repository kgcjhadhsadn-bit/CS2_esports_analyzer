"""Final review, Lens C: the owner's rule end to end in the live betting cycle.

Confidence only (odds ignored), a WIN must pay back more than the stake, the high-confidence guard
applied before the confidence check and the stake tier, stakes by confidence tier, most confident
outcome first. Uses the synthetic world of ``tests/services/conftest.py`` (its model is very sure about
match 2: p(team1) = 1.00, total maps under 0.98, 2-0 0.98).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

import cs2bot.db.models as M
from cs2bot.engine.decision import reason_code
from cs2bot.executor import FakeExecutor
from cs2bot.services.betting import run_betting_cycle
from tests.services.conftest import World
from tests.services.test_services_e2e import CMS, MAP1, MW, TM, _p, prepare_market


def owner_rule(world: World) -> None:
    """The production defaults of the decision / staking rule (the services conftest relaxes them)."""
    s = world.settings
    s.executor.dry_run = False
    s.decision.min_probability = 0.75
    s.decision.min_ev = -1.0
    s.decision.max_sigma = 0.25  # the tiny synthetic world has a wide sigma
    s.decision.require_profitable_win = True
    s.decision.use_confidence_guard = True
    s.bankroll.staking_mode = "confidence_tiers"
    s.models.confidence_guard_file = world.root / "no-guard-yet.json"


def set_prices(world: World, mid: int) -> None:
    world.set_odds(
        mid,
        {
            (MW, "team1"): "1.28",  # p 1.00 -> 0.875 after the no-history guard; EV < 0 - odds ignored
            (MW, "team2"): "3.40",
            (TM, "under"): "1.30",  # p 0.98 -> 0.866
            (TM, "over"): "3.10",
            (CMS, "2-0"): "1.10",  # a win would pay back less than the stake: never bet
            (CMS, "2-1"): "9.00",
            (CMS, "1-2"): "12.00",
            (CMS, "0-2"): "14.00",
            (MAP1, "team1"): "1.25",  # waits for the veto
            (MAP1, "team2"): "3.50",
        },
        at=world.now,
    )


def decisions(world: World) -> dict[tuple[str, str], M.BetDecisionRow]:
    with world.factory() as s:
        rows = list(s.scalars(select(M.BetDecisionRow).order_by(M.BetDecisionRow.id)))
    return {(r.spec_key, r.selection_key): r for r in rows}


def test_owner_rule_end_to_end(world: World) -> None:
    owner_rule(world)
    mid = world.match_ids[1]
    prepare_market(world, mid)
    assert _p(world, mid, MW, "team1") > 0.99 and _p(world, mid, TM, "under") > 0.95
    set_prices(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    report = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert not report.errors, report.as_dict()

    # most confident first; the no-history guard moves both into the 85-92 % tier -> 1.5 % = 15 PLN
    assert [(o.market_raw_name, o.stake) for o in ex.orders] == [
        ("Zwycięzca meczu", Decimal("15.00")),
        ("Liczba map", Decimal("15.00")),
    ]
    rows = decisions(world)
    mw = rows[(MW, "team1")]
    assert mw.accepted and mw.ev < 0  # odds ignored: a negative-EV favourite at 1.28 is bet
    assert mw.p_win == pytest.approx(0.875, abs=1e-3)
    notes = mw.settings_snapshot["notes"]
    assert any(n.startswith("confidence guard: p(win | settled) 1.000 -> 0.875") for n in notes), notes
    assert any(n.startswith("tier: p(win | settled) 0.875 >= 0.850 -> 1.50%") for n in notes), notes
    cms = rows[(CMS, "2-0")]
    assert not cms.accepted and {reason_code(r) for r in cms.reasons} == {"unprofitable_win"}
    assert "waiting_for_veto" in {reason_code(r) for r in rows[(MAP1, "team1")].reasons}
    with world.factory() as s:
        bet = s.scalar(select(M.Bet).where(M.Bet.spec_key == MW))
    assert bet is not None and bet.extra["p_settled"] == pytest.approx(0.875, abs=1e-3)
    assert bet.extra["stake_fraction"] == pytest.approx(0.015)
    assert bet.min_acceptable_odds >= Decimal("1.137")
    from cs2bot.services.telegram import format_bet_placed

    with world.factory() as s:
        text = format_bet_placed(s, s.get(M.Bet, bet.id))
    assert "confidence 87.5% -> stake 1.5% of the bankroll" in text


def test_without_the_guard_the_raw_probability_sets_the_tier(world: World) -> None:
    owner_rule(world)
    world.settings.decision.use_confidence_guard = False
    mid = world.match_ids[1]
    prepare_market(world, mid)
    set_prices(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    # 92 %+ tier = 2 % each; together exactly the 4 % per-match cap
    assert [o.stake for o in ex.orders] == [Decimal("20.00"), Decimal("20.00")]


def test_a_guard_that_saw_over_confidence_blocks_the_market(world: World, tmp_path: Path) -> None:
    """A backtest showed match-winner picks at 95-100 % winning only 70 %: the guard pulls them below
    75 % and the market is not bet; total maps (no own history, pooled history fine) still is."""
    from cs2bot.models.confidence import ConfidenceGuard

    owner_rule(world)
    records = [("match_winner", 0.97, 1 if i % 10 < 7 else 0) for i in range(400)]
    records += [("total_maps", 0.97, 1 if i % 100 < 97 else 0) for i in range(400)]
    guard = ConfidenceGuard.fit(records)
    assert guard.adjust("match_winner", 1.0) < 0.75
    world.settings.models.confidence_guard_file = tmp_path / "guard.json"
    guard.save(world.settings.models.confidence_guard_file)
    mid = world.match_ids[1]
    prepare_market(world, mid)
    set_prices(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert [o.market_raw_name for o in ex.orders] == ["Liczba map"]
    mw = decisions(world)[(MW, "team1")]
    assert not mw.accepted and "low_probability" in {reason_code(r) for r in mw.reasons}


def test_a_broken_guard_file_falls_back_to_the_cautious_prior(world: World, tmp_path: Path) -> None:
    owner_rule(world)
    world.settings.models.confidence_guard_file = tmp_path / "guard.json"
    world.settings.models.confidence_guard_file.write_text("{broken", encoding="utf-8")
    mid = world.match_ids[1]
    prepare_market(world, mid)
    set_prices(world, mid)
    ex = FakeExecutor(balance=Decimal("1000.00"))
    run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now + timedelta(seconds=1))
    assert [o.stake for o in ex.orders] == [Decimal("15.00"), Decimal("15.00")]


# ------------------------------------------------------------------ unattended guard refresh


def test_guard_refresh_waits_for_priced_history_and_remembers_the_attempt(world: World) -> None:
    from cs2bot.db import state as db_state
    from cs2bot.services.backtesting import GUARD_STATE_KEY, GuardRefreshReport, refresh_confidence_guard

    calls: list[dict[str, object]] = []

    def runner(factory: object, settings: object, **kw: object) -> GuardRefreshReport:
        calls.append(kw)
        return GuardRefreshReport(ran=True, written=True, path="guard.json", n_predictions=500)

    s = world.settings
    s.models.confidence_guard_file = world.root / "guard.json"
    # the synthetic history has no stored Betclic prices for finished matches: nothing to fit on
    first = refresh_confidence_guard(world.factory, s, now=world.now, runner=runner)
    assert not first.ran and first.priced_matches == 0 and "stored Betclic prices" in (first.skipped or "")
    with world.factory() as session:
        assert db_state.get_state(session, GUARD_STATE_KEY)["last_attempt"] == world.now.isoformat()
    # the next day is not re-tried (weekly cadence), a week later it is
    again = refresh_confidence_guard(world.factory, s, now=world.now + timedelta(days=1), runner=runner)
    assert "younger than" in (again.skipped or "") and not calls
    s.models.guard_min_priced_matches = 0
    later = world.now + timedelta(days=8)
    done = refresh_confidence_guard(world.factory, s, now=later, runner=runner)
    assert done.written and len(calls) == 1
    assert calls[0]["since"] == later - timedelta(days=s.models.guard_backtest_days)
    assert calls[0]["sims"] == s.models.guard_backtest_sims
    # disabled
    s.models.guard_refresh_days = 0
    off = refresh_confidence_guard(world.factory, s, now=later + timedelta(days=30), runner=runner)
    assert off.skipped and "disabled" in off.skipped and len(calls) == 1


def test_a_fresh_guard_file_is_not_rewritten(world: World) -> None:
    from cs2bot.services.backtesting import refresh_confidence_guard

    s = world.settings
    s.models.confidence_guard_file = world.root / "guard.json"
    s.models.confidence_guard_file.write_text("{}", encoding="utf-8")
    report = refresh_confidence_guard(world.factory, s, runner=lambda *a, **k: pytest.fail("must not run"))
    assert report.skipped and "days old" in report.skipped


def test_the_guard_refresh_job_is_scheduled_in_the_train_lane(world: World) -> None:
    from cs2bot.flows.jobs import TRAIN, Runtime, build_jobs
    from cs2bot.services.registry import ModelRegistry

    rt = Runtime(
        settings=world.settings, session_factory=world.factory, registry=ModelRegistry(world.settings)
    )
    jobs = {j.name: j for j in build_jobs(rt)}
    job = jobs["confidence_guard_refresh"]
    assert job.lane == TRAIN and job.enabled
    world.settings.models.guard_refresh_days = 0
    assert not {j.name: j for j in build_jobs(rt)}["confidence_guard_refresh"].enabled


def test_doctor_reports_the_confidence_guard(world: World) -> None:
    from cs2bot.models.confidence import ConfidenceGuard
    from cs2bot.services.doctor import check_confidence_guard

    s = world.settings
    s.models.confidence_guard_file = world.root / "guard.json"
    s.decision.use_confidence_guard = True
    missing = check_confidence_guard(s)
    assert missing.level == "warn" and "no-history prior" in missing.message
    ConfidenceGuard.fit([("match_winner", 0.8, 1)] * 10).save(s.models.confidence_guard_file)
    assert check_confidence_guard(s).level == "ok"
    s.decision.require_trusted_calibration = True
    assert check_confidence_guard(s).level == "warn"  # nothing trusted yet
    s.models.confidence_guard_file.write_text("{bad", encoding="utf-8")
    assert check_confidence_guard(s).level == "fail"


# ------------------------------------------------------------------ start-up: training in the background


def test_run_up_serves_and_schedules_while_the_first_training_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: the first training ran before the scheduler and the API (a long one marked the VPS
    container unhealthy, collected no odds and could not be stopped cleanly)."""
    import threading

    import httpx
    from alembic import command

    from cs2bot.db.base import Base
    from cs2bot.db.session import make_engine, make_session_factory
    from cs2bot.flows.supervisor import EXIT_OK, UpHandles, run_up
    from cs2bot.odds import StaticOddsSource
    from cs2bot.services.dbops import alembic_config
    from cs2bot.services.registry import ModelRegistry
    from tests.services.test_services_up import _settings

    settings = _settings(tmp_path)
    engine = make_engine(settings.database.url)
    Base.metadata.create_all(engine)
    command.stamp(alembic_config(settings.database.url), "head")
    factory = make_session_factory(engine)
    training_started, release = threading.Event(), threading.Event()

    def slow_retrain(self: ModelRegistry, session_factory: object, **_: object) -> None:
        training_started.set()
        release.wait(20)
        raise RuntimeError("no data in this test")

    monkeypatch.setattr(ModelRegistry, "retrain", slow_retrain)
    seen: dict[str, object] = {}

    def started(h: UpHandles) -> None:
        assert training_started.wait(10)
        seen["ping"] = httpx.get(f"{h.url}api/ping", timeout=10).json()  # served while training
        seen["training_running"] = not release.is_set()
        release.set()
        h.stop.set()

    code = run_up(
        settings,
        session_factory=factory,
        executor_factory=lambda s, f: FakeExecutor(),
        runtime_overrides={"odds_source_factory": lambda: StaticOddsSource([]), "hltv_enabled": False},
        on_started=started,
        echo=lambda _line: None,
    )
    assert code == EXIT_OK
    assert seen == {"ping": {"status": "ok"}, "training_running": True}
    engine.dispose()


def test_the_scheduled_retrain_skips_a_bundle_trained_moments_ago(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import cs2bot.services.registry as reg
    from cs2bot.db.jobs import JobTracker
    from cs2bot.flows.jobs import Runtime, _retrain

    registry = reg.ModelRegistry(world.settings, world.bundle)
    world.bundle.trained_at = world.now  # as if the start-up training just finished
    calls: list[int] = []

    def fake_retrain(*_a: object, **_k: object) -> object:
        calls.append(1)
        return SimpleNamespace(n_map_results=7, as_dict=dict, warnings=[])

    monkeypatch.setattr(registry, "retrain", fake_retrain)
    rt = Runtime(settings=world.settings, session_factory=world.factory, registry=registry)
    monkeypatch.setattr(reg, "utcnow", lambda: world.now + timedelta(minutes=30))
    skipped = JobTracker()
    _retrain(rt, skipped)
    assert not calls and "fresh" in str(skipped.details)
    monkeypatch.setattr(reg, "utcnow", lambda: world.now + timedelta(hours=3))
    trained = JobTracker()
    _retrain(rt, trained)
    assert calls == [1] and trained.items == 7


# ------------------------------------------------------------------ executor unavailable


def _unusable(order: object) -> object:
    from cs2bot.domain.betting import PlacementResult, PlacementStatus
    from cs2bot.executor.base import EXECUTOR_UNAVAILABLE_PREFIX
    from cs2bot.utils.time import utcnow

    return PlacementResult(
        status=PlacementStatus.FAILED,
        message=f"{EXECUTOR_UNAVAILABLE_PREFIX}Chromium could not be started (Executable doesn't exist)",
        at=utcnow(),
    )


def test_an_unusable_executor_stops_the_cycle_and_pauses_until_a_health_check_passes(world: World) -> None:
    """Before: each accepted outcome of every cycle (3 min) became one more FAILED bet, each trying to
    start Chromium again; the ``executor_unavailable`` pause code existed but nothing produced it."""
    from cs2bot.db import state as db_state
    from cs2bot.domain.betting import ExecutorHealth
    from cs2bot.domain.control import PauseCode
    from cs2bot.services.autonomy import AutonomyService
    from cs2bot.services.health import HealthCache

    owner_rule(world)
    world.settings.autonomy.enabled = True
    mid = world.match_ids[1]
    prepare_market(world, mid)
    set_prices(world, mid)
    ex = FakeExecutor([_unusable, _unusable], balance=Decimal("1000.00"))
    report = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert len(ex.orders) == 1 and report.failed == 1  # the second accepted outcome was not tried
    with world.factory() as s:
        reasons = db_state.get_pause_reasons(s)
    assert [r.code for r in reasons] == [PauseCode.EXECUTOR_UNAVAILABLE]
    # Chromium works again: the next health check after the pause lifts it without the owner
    later = world.now + timedelta(minutes=10)
    ex.health = ExecutorHealth(ok=True, logged_in=True, balance=Decimal("1000.00"), checked_at=later)
    service = AutonomyService(world.factory, world.settings, ex, health_cache=HealthCache())
    result = service.run_once(now=later)
    assert PauseCode.EXECUTOR_UNAVAILABLE.value in [
        c.value if hasattr(c, "value") else c for c in result.resumed
    ]
    with world.factory() as s:
        assert not db_state.is_trading_halted(s)[0]


def test_without_autonomy_an_unusable_executor_only_stops_the_cycle(world: World) -> None:
    from cs2bot.db import state as db_state

    owner_rule(world)
    world.settings.autonomy.enabled = False
    mid = world.match_ids[1]
    prepare_market(world, mid)
    set_prices(world, mid)
    ex = FakeExecutor([_unusable, _unusable], balance=Decimal("1000.00"))
    report = run_betting_cycle(world.factory, world.settings, ex, world.bundle, now=world.now)
    assert len(ex.orders) == 1 and any("not available" in n for n in report.notes)
    with world.factory() as s:
        assert not db_state.is_trading_halted(s)[0]
