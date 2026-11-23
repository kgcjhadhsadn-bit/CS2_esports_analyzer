"""Regression tests added in review round 1.

Several of them drive the *real* engine / odds / scheduler code (never imported by ``cs2bot.api``
itself) to check that the dashboard shows what those packages actually produce:

* paper bets settled by ``engine.ledger.settle_bet`` keep ``status = dry_run``;
* the bankroll numbers and loss limits equal ``engine.bankroll.compute_state`` / ``loss_limit_status``;
* ``Scheduler.status()`` is shown under ``/api/health``;
* ``odds.remap_event`` works as the injected ``event_remapper``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.api import ApiOptions
from cs2bot.api.analytics import calibration_set, effective_status
from cs2bot.api.auth import LoginRateLimiter
from cs2bot.api.bankroll import LedgerView
from cs2bot.api.deps import AuthState
from cs2bot.api.ev import TaxTerms, breakeven_odds, ev_after_tax, fair_odds, p_win_given_settled
from cs2bot.api.main import _uvicorn_log_level
from cs2bot.db import state
from cs2bot.db.models import (
    AuditLog,
    BankrollLedger,
    Bet,
    BetDecisionRow,
    BookmakerEvent,
    BookmakerSelection,
    MapPlayed,
    Match,
    SystemState,
)
from cs2bot.domain.betting import PlacementResult, PlacementStatus
from cs2bot.domain.enums import BetStatus, LedgerKind, MatchStatus
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.engine import bankroll as engine_bankroll
from cs2bot.engine import ledger as engine_ledger
from cs2bot.flows.scheduler import Daily, Interval, Job, Scheduler
from tests.api.api_seed import (
    BASE_URL,
    CSRF,
    MW,
    NOW,
    Seed,
    _bet,
    _decision,
    _prediction,
    make_settings,
)

# ------------------------------------------------------------------ paper bets settled by the engine


def _place(session: Session, bet: Bet, status: PlacementStatus, at: datetime, settings: Any) -> None:
    result = PlacementResult(status=status, placed_odds=bet.requested_odds, at=at)
    engine_ledger.apply_placement(session, bet, result, settings)


def test_effective_status() -> None:
    won = effective_status(BetStatus.DRY_RUN, extra={"settled_outcome": "won"})
    assert won is BetStatus.WON
    assert effective_status(BetStatus.DRY_RUN, extra={"settled_outcome": "PUSH "}) is BetStatus.VOID
    assert effective_status(BetStatus.DRY_RUN, extra={"settled_outcome": "lost"}) is BetStatus.LOST
    assert effective_status(BetStatus.DRY_RUN, extra={}) is BetStatus.DRY_RUN  # still open
    assert effective_status(BetStatus.DRY_RUN, extra=None, pnl=Decimal("1")) is BetStatus.DRY_RUN
    # rows without the key: inferred from settled_at + the sign of pnl
    assert effective_status(BetStatus.DRY_RUN, pnl=Decimal("-10"), settled_at=NOW) is BetStatus.LOST
    assert effective_status(BetStatus.DRY_RUN, pnl=Decimal("0"), settled_at=NOW) is BetStatus.VOID
    assert effective_status(BetStatus.DRY_RUN, pnl=Decimal("3"), settled_at=NOW) is BetStatus.WON
    assert effective_status(BetStatus.DRY_RUN, extra={"settled_outcome": "??"}) is BetStatus.DRY_RUN
    # real bets are never reinterpreted
    assert effective_status(BetStatus.PLACED, extra={"settled_outcome": "won"}) is BetStatus.PLACED
    assert effective_status(BetStatus.LOST, pnl=Decimal("5"), settled_at=NOW) is BetStatus.LOST


def test_paper_bets_settled_by_the_engine(app_factory, session_factory, tmp_path: Path) -> None:
    """``settle_bet`` keeps a paper bet's status DRY_RUN: it must still count as won / lost / void."""
    settings = make_settings(tmp_path)
    ids: dict[str, int] = {}
    with session_factory() as s:
        for key, outcome in (("won", "won"), ("lost", "lost"), ("void", "void"), ("open", None)):
            bet = _bet(
                s,
                f"paper-{key}",
                stake=Decimal("10.00"),
                requested_odds=Decimal("2.00"),
                status=BetStatus.PENDING,
                dry_run=True,
            )
            _place(s, bet, PlacementStatus.DRY_RUN_OK, NOW - timedelta(hours=5), settings)
            if outcome is not None:
                engine_ledger.settle_bet(s, bet, outcome, settings, at=NOW - timedelta(hours=1))
            assert bet.status is BetStatus.DRY_RUN  # the engine never turns it into won / lost
            ids[key] = bet.id
        s.commit()

    with TestClient(app_factory(settings=settings), base_url=BASE_URL) as c:
        paper = c.get("/api/bankroll", params={"dry_run": "true"}).json()
        stats = paper["stats"]
        assert (stats["bets"], stats["open_bets"], stats["settled_bets"]) == (4, 1, 3)
        assert (stats["won"], stats["lost"], stats["void"]) == (1, 1, 1)
        # won: 10 * 0.88 * 2.00 - 10 = +7.60; lost: -10.00; void: 0.00
        assert stats["realized_pnl"] == "-2.40" and stats["turnover"] == "20.00" and stats["hit_rate"] == 0.5
        assert paper["open_exposure"] == "10.00" and paper["balance"] == "987.60"
        assert paper["bankroll_reference"] == "997.60" and paper["daily"]["realized_pnl"] == "-2.40"
        assert [p["pnl"] for p in paper["pnl_by_day"]] == ["-2.40"]
        assert paper["equity_curve"][-1]["balance"] == "997.60"
        # paper money never shows up in the real-money view
        real = c.get("/api/bankroll").json()
        assert real["stats"]["bets"] == 0 and real["balance"] == "1000.00"

        listed = c.get("/api/bets", params={"dry_run": "true"}).json()
        assert listed["total"] == 4
        assert listed["summary"] == {
            "count": 4,
            "stake": "40.00",
            "pnl": "-2.40",
            "open": 1,
            "won": 1,
            "lost": 1,
            "void": 1,
        }
        by_id = {b["id"]: b for b in listed["items"]}
        assert {b["status"] for b in by_id.values()} == {"dry_run"}
        assert by_id[ids["won"]]["result"] == "won" and by_id[ids["won"]]["is_open"] is False
        assert by_id[ids["lost"]]["result"] == "lost" and by_id[ids["void"]]["result"] == "void"
        assert by_id[ids["open"]]["result"] is None and by_id[ids["open"]]["is_open"] is True
        # the status filter matches the result of paper bets and the stored status
        won = c.get("/api/bets", params={"status": "won"}).json()
        assert [b["id"] for b in won["items"]] == [ids["won"]] and won["summary"]["won"] == 1
        assert c.get("/api/bets", params={"status": "dry_run"}).json()["total"] == 4
        assert c.get("/api/bets", params={"status": "lost,void", "limit": 1}).json()["total"] == 2
        assert c.get("/api/bets", params={"dry_run": "false"}).json()["total"] == 0
        assert c.get(f"/api/bets/{ids['won']}").json()["bet"]["result"] == "won"

        perf = c.get("/api/performance/markets", params={"mode": "dry_run"}).json()["total"]
        assert (perf["settled"], perf["won"], perf["lost"], perf["void"], perf["open"]) == (3, 1, 1, 1, 1)
        assert perf["pnl"] == "-2.40" and perf["roi"] == pytest.approx(-0.12)


def test_paper_bet_calibration_and_review_open_count(
    client: TestClient, seeded: Seed, session_factory, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    with session_factory() as s:
        for key, outcome, p_win in (("a", "won", 0.9), ("b", "lost", 0.8)):
            decision = _decision(
                s,
                5,
                MW,
                "team1",
                accepted=True,
                decided=NOW - timedelta(hours=6),
                odds="2.00",
                p_win=p_win,
                ev=0.4,
            )
            bet = _bet(
                s,
                f"cal-{key}",
                decision_id=decision.id,
                match_id=5,
                selection_id=seeded.selections["team2"],
                stake=Decimal("10.00"),
                requested_odds=Decimal("2.00"),
                status=BetStatus.PENDING,
                dry_run=True,
            )
            _place(s, bet, PlacementStatus.DRY_RUN_OK, NOW - timedelta(hours=5), settings)
            engine_ledger.settle_bet(s, bet, outcome, settings, at=NOW - timedelta(hours=1))
        s.commit()
    paper = client.get("/api/performance/calibration", params={"mode": "dry_run"}).json()["bets"]
    assert paper["n"] == 2 and paper["base_rate"] == 0.5
    assert paper["brier"] == pytest.approx(((0.9 - 1) ** 2 + 0.8**2) / 2)
    assert client.get("/api/performance/calibration", params={"mode": "real"}).json()["bets"]["n"] == 2
    assert client.get("/api/performance/calibration").json()["bets"]["n"] == 4
    # the two settled paper bets sit on a selection of the main event; only the PLACED real bet is open
    body = client.post(
        f"/api/review/links/{seeded.events['main']}", json={"action": "confirm"}, headers=CSRF
    ).json()
    assert body["open_bets"] == 1


# ------------------------------------------------------------------ bankroll == engine


def test_bankroll_equals_engine_state_on_the_first_day(app_factory, session_factory, tmp_path: Path) -> None:
    """The ledger starts today: the day/week-start bankroll is the opening deposit, not 0."""
    settings = make_settings(tmp_path)
    with session_factory() as s:
        engine_ledger.ensure_initialized(s, settings, at=NOW - timedelta(hours=3))
        lost = _bet(s, "e-lost", stake=Decimal("30.00"), status=BetStatus.PENDING, dry_run=False)
        _place(s, lost, PlacementStatus.PLACED, NOW - timedelta(hours=2), settings)
        engine_ledger.settle_bet(s, lost, "lost", settings, at=NOW - timedelta(hours=1))
        placed = _bet(s, "e-open", stake=Decimal("15.00"), status=BetStatus.PENDING, dry_run=False)
        _place(s, placed, PlacementStatus.PLACED, NOW - timedelta(minutes=30), settings)
        _bet(s, "e-pending", stake=Decimal("10.00"), status=BetStatus.PENDING, dry_run=False)
        uncertain = _bet(s, "e-uncertain", stake=Decimal("5.00"), status=BetStatus.PENDING, dry_run=False)
        _place(s, uncertain, PlacementStatus.UNCERTAIN, NOW - timedelta(minutes=10), settings)
        assert uncertain.status is BetStatus.PLACING
        st = engine_bankroll.compute_state(s, settings, now=NOW)
        limits = engine_bankroll.loss_limit_status(st, settings.bankroll)
        ledger_balance = engine_ledger.current_balance(s)
        s.commit()

    with TestClient(app_factory(settings=settings), base_url=BASE_URL) as c:
        b = c.get("/api/bankroll").json()
        alerts = [a["code"] for a in c.get("/api/health").json()["alerts"]]
    assert b["balance"] == f"{ledger_balance:.2f}" == "955.00" and b["balance_source"] == "ledger"
    # pending + unconfirmed stakes are not debited yet: they reduce the free balance only
    assert b["available_balance"] == f"{st.balance:.2f}" == "940.00"
    assert b["bankroll_reference"] == f"{st.bankroll_reference:.2f}" == "970.00"
    assert b["open_exposure"] == f"{limits.open_stakes:.2f}" == "30.00" and b["open_bets"] == 3
    day, week = b["daily"], b["weekly"]
    assert day["start_bankroll"] == f"{st.day_start_bankroll:.2f}" == "1000.00"
    assert week["start_bankroll"] == f"{st.week_start_bankroll:.2f}" == "1000.00"
    assert day["limit_amount"] == f"{limits.daily_limit:.2f}" == "50.00"
    assert week["limit_amount"] == f"{limits.weekly_limit:.2f}" == "100.00"
    assert day["realized_pnl"] == f"{st.realized_pnl_today:.2f}" == "-30.00"
    assert day["reached"] is limits.daily_hit is False and week["reached"] is limits.weekly_hit is False
    assert day["remaining"] == "20.00" and day["open_stakes"] == "30.00" and day["worst_case_pnl"] == "-60.00"
    # the engine's guard: limit - (realized loss + ALL open stakes), never below zero on the dashboard
    assert limits.daily_allowance == Decimal("-10.00") and day["stake_allowance"] == "0.00"
    assert week["stake_allowance"] == f"{limits.weekly_allowance:.2f}" == "40.00"
    assert "loss_limit_reached" not in alerts


def test_bankroll_mid_period_deposit_matches_engine(
    client: TestClient, session_factory, tmp_path: Path
) -> None:
    """A deposit made today counts as capital the daily limit protects (engine convention)."""
    settings = make_settings(tmp_path)
    with session_factory() as s:
        engine_ledger.record_deposit(s, Decimal("500.00"), at=NOW - timedelta(minutes=5))
        st = engine_bankroll.compute_state(s, settings, now=NOW)
        limits = engine_bankroll.loss_limit_status(st, settings.bankroll)
        s.commit()
    b = client.get("/api/bankroll").json()
    assert b["balance"] == "1468.44" and b["bankroll_reference"] == f"{st.bankroll_reference:.2f}"
    assert b["daily"]["start_bankroll"] == f"{st.day_start_bankroll:.2f}" == "1513.44"
    assert b["daily"]["limit_amount"] == f"{limits.daily_limit:.2f}" == "75.67"
    assert b["weekly"]["start_bankroll"] == f"{st.week_start_bankroll:.2f}" == "1500.00"
    assert b["stats"]["capital_base"] == "1500.00"


def test_ledger_view_with_back_dated_rows() -> None:
    """The balance is the last WRITTEN row; the curve is re-accumulated in time order."""
    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    rows = [
        BankrollLedger(
            id=1, ts=t0, kind=LedgerKind.DEPOSIT, amount=Decimal("1000"), balance_after=Decimal("1000")
        ),
        BankrollLedger(
            id=2,
            ts=t0 + timedelta(hours=3),
            kind=LedgerKind.STAKE,
            amount=Decimal("-20"),
            balance_after=Decimal("980"),
            bet_id=7,
        ),
        # booked later but dated at the time the bet was really placed (reconciled UNCERTAIN bet)
        BankrollLedger(
            id=3,
            ts=t0 + timedelta(hours=1),
            kind=LedgerKind.STAKE,
            amount=Decimal("-10"),
            balance_after=Decimal("970"),
            bet_id=8,
        ),
    ]
    view = LedgerView(list(reversed(rows)))
    assert view.current() == Decimal("970")  # not 980 (the row with the latest timestamp)
    assert view.debited_bet_ids() == {7, 8} and view.deposits() == Decimal("1000")
    curve = view.curve()
    assert [p.balance for p in curve] == ["1000.00", "990.00", "970.00"]
    assert [p.ts for p in curve] == sorted(p.ts for p in curve)
    assert LedgerView([]).curve() == [] and LedgerView([]).current() is None


def test_back_dated_ledger_row_does_not_change_the_balance(client: TestClient, session_factory) -> None:
    with session_factory() as s:
        s.add(
            BankrollLedger(
                ts=datetime(2026, 11, 12, 9, 0, tzinfo=UTC),  # between the deposit and the first stake
                kind=LedgerKind.ADJUSTMENT,
                amount=Decimal("-8.44"),
                balance_after=Decimal("960.00"),
                note="correction booked today for last week",
            )
        )
        s.commit()
    b = client.get("/api/bankroll").json()
    assert b["balance"] == "960.00" and b["equity_curve"][-1]["balance"] == "960.00"
    assert [p["balance"] for p in b["equity_curve"]][:3] == ["1000.00", "991.56", "971.56"]


# ------------------------------------------------------------------ scheduler status in /api/health


def _noop(_tracker: Any) -> None:
    return None


def test_health_shows_real_scheduler_status(app_factory, seeded: Seed, session_factory) -> None:
    jobs = [
        Job("odds_collect", _noop, Interval(timedelta(minutes=3))),
        Job("predict_upcoming", _noop, Interval(timedelta(minutes=15))),
        Job("settle_bets", _noop, Interval(timedelta(minutes=30))),
        Job("hltv_rankings", _noop, Daily(time(4, 0)), lane="slow"),
        Job("retrain_models", _noop, Daily(time(5, 0)), lane="slow", enabled=False),
    ]
    scheduler = Scheduler(jobs, session_factory, clock=lambda: NOW)
    scheduler.restore_state()  # reads job_runs: predict_upcoming's RUNNING row is a leftover of a crash
    with TestClient(app_factory(scheduler_status_provider=scheduler.status), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    sched = {j["name"]: j for j in h["scheduler"]["jobs"]}
    assert h["scheduler"]["ok"] is True and list(sched) == [j.name for j in jobs]
    assert sched["odds_collect"]["next_due_at"] == "2026-11-20T12:01:00Z"
    assert sched["odds_collect"]["due_in_s"] == 60.0 and sched["odds_collect"]["lane"] == "fast"
    assert sched["odds_collect"]["last_run_at"] == "2026-11-20T11:58:00Z"
    assert sched["settle_bets"]["failures"] == 3 and sched["settle_bets"]["overdue"] is False
    assert sched["retrain_models"]["enabled"] is False and sched["retrain_models"]["overdue"] is False
    assert sched["hltv_rankings"]["last_run_at"] is None and sched["hltv_rankings"]["overdue"] is False
    # the leftover RUNNING row is not a live run: the job has been due for 2 h 45 min
    assert sched["predict_upcoming"]["running"] is False and sched["predict_upcoming"]["overdue"] is True
    assert sched["predict_upcoming"]["due_in_s"] == -(2 * 3600 + 45 * 60)

    by_name = {j["name"]: j for j in h["jobs"]}
    assert by_name["odds_collect"]["scheduled"] is True
    assert by_name["odds_collect"]["next_due_at"] == "2026-11-20T12:01:00Z"
    assert by_name["settle_bets"]["scheduler_failures"] == 3
    # not a scheduler job any more: nobody is supposed to run it, so it is not "stale"
    assert by_name["ingest_upcoming"]["scheduled"] is False and by_name["ingest_upcoming"]["stale"] is False
    alerts = h["alerts"]
    overdue = [a for a in alerts if a["code"] == "scheduler_job_overdue"]
    assert (
        len(overdue) == 1 and "predict_upcoming" in overdue[0]["message"] and overdue[0]["level"] == "warning"
    )
    disabled = next(a for a in alerts if a["code"] == "scheduler_job_disabled")
    assert disabled["level"] == "info" and "retrain_models" in disabled["message"]
    stale = [a["message"] for a in alerts if a["code"] == "job_stale"]
    assert not any("ingest_upcoming" in m for m in stale)


def test_health_scheduler_running_job_and_busy_lane(app_factory, seeded: Seed) -> None:
    hour_ago = (NOW - timedelta(hours=1)).isoformat()
    status = [
        # the RUNNING job_runs row (started 3 h ago) is newer than the scheduler's last finished run
        {
            "name": "predict_upcoming",
            "lane": "fast",
            "enabled": True,
            "last_run": (NOW - timedelta(hours=4)).isoformat(),
            "next_due": (NOW - timedelta(hours=3)).isoformat(),
            "failures": 0,
        },
        {"name": "odds_collect", "lane": "fast", "enabled": True, "next_due": hour_ago, "failures": 0},
        {"name": "sync_results", "lane": "slow", "enabled": True, "next_due": hour_ago, "failures": 2},
        {"name": "barely_late", "lane": "slow", "enabled": True, "next_due": (NOW - timedelta(minutes=14))},
        # malformed entries are skipped / tolerated
        "garbage",
        {"lane": "fast"},
        {"name": "odd_types", "enabled": 1, "last_run": "not a date", "next_due": 12345, "failures": "x"},
    ]
    with TestClient(app_factory(scheduler_status_provider=lambda: status), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    sched = {j["name"]: j for j in h["scheduler"]["jobs"]}
    assert set(sched) == {"predict_upcoming", "odds_collect", "sync_results", "barely_late", "odd_types"}
    assert sched["predict_upcoming"]["running"] is True and sched["predict_upcoming"]["overdue"] is False
    assert sched["odds_collect"]["overdue"] is False  # queued behind the running job of its lane
    assert sched["sync_results"]["overdue"] is True and sched["sync_results"]["failures"] == 2
    assert sched["barely_late"]["overdue"] is False  # inside the 15 min grace
    assert sched["odd_types"] == {
        "name": "odd_types",
        "lane": None,
        "enabled": True,
        "last_run_at": None,
        "next_due_at": None,
        "due_in_s": None,
        "failures": 0,
        "running": False,
        "overdue": False,
    }
    codes = [a["code"] for a in h["alerts"]]
    assert codes.count("scheduler_job_overdue") == 1 and "job_stuck" in codes

    strict = ApiOptions(scheduler_overdue_grace_s=60.0)
    with TestClient(
        app_factory(scheduler_status_provider=lambda: status, options=strict), base_url=BASE_URL
    ) as c:
        late = {j["name"]: j["overdue"] for j in c.get("/api/health").json()["scheduler"]["jobs"]}
    assert late["barely_late"] is True and late["odds_collect"] is False


def test_health_scheduler_provider_failure_and_absence(app_factory, seeded: Seed) -> None:
    def broken() -> list[dict[str, object]]:
        raise RuntimeError("scheduler object gone")

    with TestClient(app_factory(scheduler_status_provider=broken), base_url=BASE_URL) as c:
        resp = c.get("/api/health")
    assert resp.status_code == 200
    h = resp.json()
    assert h["scheduler"] == {"ok": False, "jobs": []}
    alert = next(a for a in h["alerts"] if a["code"] == "scheduler_status_error")
    assert alert["level"] == "warning" and "scheduler object gone" not in resp.text
    assert {j["name"]: j["stale"] for j in h["jobs"]}["ingest_upcoming"] is True  # nothing was suppressed

    with TestClient(app_factory(), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    assert h["scheduler"] is None and all(j["scheduled"] is None for j in h["jobs"])


def test_odds_staleness_is_reported_even_when_the_scheduler_has_no_odds_job(
    app_factory, seeded: Seed
) -> None:
    status = [{"name": "settle_bets", "lane": "fast", "enabled": True, "next_due": NOW.isoformat()}]
    late = NOW + timedelta(hours=2)
    with TestClient(app_factory(scheduler_status_provider=lambda: status, now=late), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    jobs = {j["name"]: j for j in h["jobs"]}
    assert jobs["odds_collect"]["scheduled"] is False and jobs["odds_collect"]["stale"] is False
    stale = next(a for a in h["alerts"] if a["code"] == "odds_collector_stale")
    assert "last seen" in stale["message"]


def test_scheduler_status_survives_a_database_outage(tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    from cs2bot.api import create_app

    broken = sessionmaker(bind=create_engine(f"sqlite:///{tmp_path}/missing-dir/db.sqlite"))
    status = [{"name": "odds_collect", "lane": "fast", "enabled": True, "next_due": NOW.isoformat()}]
    app = create_app(make_settings(tmp_path), broken, scheduler_status_provider=lambda: status)
    with TestClient(app, base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    assert h["db"]["ok"] is False and [j["name"] for j in h["scheduler"]["jobs"]] == ["odds_collect"]


def test_dry_run_alert_is_informational(app_factory, session_factory) -> None:
    """Paper trading is the default mode: on its own it must not turn the health status yellow."""
    with session_factory() as s:
        state.set_state(
            s, state.EXECUTOR_HEALTH, {"ok": True, "logged_in": True, "checked_at": NOW.isoformat()}
        )
        s.commit()
    status = [{"name": "odds_collect", "lane": "fast", "enabled": True, "next_due": NOW.isoformat()}]
    with TestClient(app_factory(scheduler_status_provider=lambda: status), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    by_code = {a["code"]: a["level"] for a in h["alerts"]}
    assert by_code["dry_run_on"] == "info"
    # the only non-info alert of an empty database: no odds collected yet
    assert {code for code, level in by_code.items() if level != "info"} == {"odds_collector_never_ran"}


# ------------------------------------------------------------------ calibration


def test_prediction_calibration_is_conditional_and_has_no_look_ahead(
    client: TestClient, session_factory
) -> None:
    map3 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=3)
    with session_factory() as s:
        played = s.scalars(
            select(MapPlayed)
            .join(Match, Match.id == MapPlayed.match_id)
            .where(MapPlayed.map_number == 3, Match.id > 10, Match.status == MatchStatus.FINISHED)
        ).first()
        assert played is not None, "the synthetic universe has no finished BO3 that went to map 3"
        match = s.get(Match, played.match_id)
        assert match is not None and match.scheduled_at is not None
        won = played.winner_team_id == match.team1_id
        # unconditional: P(map 3 is played) = 0.4 and P(team1 wins it | played) = 0.5
        _prediction(
            s, match.id, map3, "team1", 0.2, p_void=0.6, created=match.scheduled_at - timedelta(minutes=30)
        )
        # look-ahead: written after the match started (must be ignored)
        other = s.scalars(
            select(Match).where(Match.id > 10, Match.id != match.id, Match.status == MatchStatus.FINISHED)
        ).first()
        assert other is not None and other.scheduled_at is not None
        _prediction(s, other.id, MW, "team1", 0.99, created=other.scheduled_at + timedelta(minutes=5))
        _prediction(s, other.id, MW, "team2", 0.01, created=other.scheduled_at)
        s.commit()
    body = client.get("/api/performance/calibration").json()
    maps = body["predictions_by_market"]["map_winner"]
    assert maps["n"] == 11
    assert maps["mean_predicted"] == pytest.approx((10 * 0.55 + 0.5) / 11, abs=1e-6)
    mid_bin = next(b for b in maps["bins"] if b["lower"] == 0.5)
    assert mid_bin["count"] == 11  # 0.5 belongs to [0.5, 0.6), together with the ten 0.55 samples
    low_bin = next(b for b in maps["bins"] if b["lower"] == 0.2)
    assert low_bin["count"] == 0 and won in (True, False)
    assert body["predictions_by_market"]["match_winner"]["n"] == 10  # the look-ahead rows are not used


def test_bet_calibration_uses_probability_given_settled(
    client: TestClient, seeded: Seed, session_factory
) -> None:
    with session_factory() as s:
        map1 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=1)
        pred = _prediction(s, 2, map1, "team2", 0.38, p_void=0.5)
        decision = s.get(BetDecisionRow, seeded.decisions["lost"])
        assert decision is not None
        decision.prediction_id, decision.p_win = pred.id, 0.38
        s.commit()
    bets = client.get("/api/performance/calibration", params={"mode": "real"}).json()["bets"]
    # lost bet: 0.38 / (1 - 0.5) = 0.76; won bet: 0.78 (no linked prediction -> the decision's p_win)
    assert bets["n"] == 2 and bets["mean_predicted"] == pytest.approx(0.77)


def test_calibration_bin_edges() -> None:
    ten = calibration_set([(0.3, 1), (0.7, 0), (0.6, 1), (1.0, 1), (0.0, 0)], 10)
    counts = {b.lower: b.count for b in ten.bins}
    assert counts[0.3] == 1 and counts[0.2] == 0 and counts[0.7] == 1 and counts[0.6] == 1
    assert counts[0.9] == 1 and counts[0.0] == 1  # 1.0 falls into the last bin
    hundred = calibration_set([(0.57, 1), (0.29, 0)], 100)
    hot = sorted(b.lower for b in hundred.bins if b.count)
    assert hot == [pytest.approx(0.29), pytest.approx(0.57)]


# ------------------------------------------------------------------ EV helper


def test_ev_matches_engine_formula_with_and_without_refunded_tax() -> None:
    full = TaxTerms(Decimal("0.12"))
    partial = TaxTerms(Decimal("0.12"), Decimal("0.12"))  # void_refunds_stake_tax = False
    odds = Decimal("2.50")
    assert ev_after_tax(0.5, odds, full, p_void=0.2) == pytest.approx(0.5 * 2.2 + 0.2 - 1)
    # a refund costs the stake tax: 0.2 * (1 - 0.12)
    assert ev_after_tax(0.5, odds, partial, p_void=0.2) == pytest.approx(0.5 * 2.2 + 0.2 * 0.88 - 1)
    assert ev_after_tax(0.5, odds, Decimal("0.12")) == pytest.approx(0.1)  # a bare rate still works
    be_full = breakeven_odds(0.5, full, p_void=0.2)
    be_partial = breakeven_odds(0.5, partial, p_void=0.2)
    assert be_full is not None and be_partial is not None and be_partial > be_full
    assert ev_after_tax(0.5, be_partial, partial, p_void=0.2) == pytest.approx(0.0, abs=1e-9)
    assert fair_odds(0.5, p_void=0.2) == Decimal("1.6")
    assert p_win_given_settled(0.2, p_void=0.6) == pytest.approx(0.5)
    assert p_win_given_settled(0.3, p_push=0.5, p_void=0.5) is None
    assert p_win_given_settled(0.9, p_void=0.5) == 1.0  # inconsistent input is clamped
    # the engine's own formula (expected_value without a stake) gives the same numbers
    from cs2bot.config import BetclicSettings
    from cs2bot.domain.predictions import OutcomeProbability
    from cs2bot.engine.ev import expected_value

    outcome = OutcomeProbability(
        spec_key="x", selection="team1", p_win=0.5, p_void=0.15, p_push=0.05, sigma=0.02
    )
    for refunds, terms in ((True, full), (False, partial)):
        engine = expected_value(outcome, odds, BetclicSettings(void_refunds_stake_tax=refunds))
        assert ev_after_tax(0.5, odds, terms, p_void=0.15, p_push=0.05) == pytest.approx(engine)


def test_tax_terms_from_settings_and_match_detail(
    app_factory, seeded: Seed, session_factory, tmp_path: Path
) -> None:
    assert TaxTerms.from_settings(make_settings(tmp_path)) == TaxTerms(Decimal("0.12"), Decimal("0"))
    no_refund = make_settings(tmp_path, betclic={"void_refunds_stake_tax": False})
    assert TaxTerms.from_settings(no_refund) == TaxTerms(Decimal("0.12"), Decimal("0.12"))
    promo = make_settings(tmp_path, betclic={"void_refunds_stake_tax": False, "tax_free_promo": True})
    assert TaxTerms.from_settings(promo) == TaxTerms(Decimal("0"), Decimal("0"))

    with session_factory() as s:
        over = MarketSpec(market_type=MarketType.TOTAL_MAPS, line=2.5)
        _prediction(s, 101, over, "over", 0.3, p_void=0.25)
        s.commit()

    def row(settings: Any) -> dict[str, Any]:
        with TestClient(app_factory(settings=settings), base_url=BASE_URL) as c:
            markets = c.get("/api/matches/101").json()["markets"]
        return next(
            m for m in markets if m["spec_key"] == "total_maps|line=2.5" and m["selection_key"] == "over"
        )

    normal = row(make_settings(tmp_path))
    assert normal["p_win_settled"] == pytest.approx(0.4) and normal["p_lose"] == pytest.approx(0.45)
    assert normal["ev"] == pytest.approx(0.3 * 0.88 * 2.20 + 0.25 - 1)
    assert row(no_refund)["ev"] == pytest.approx(0.3 * 0.88 * 2.20 + 0.25 * 0.88 - 1)


# ------------------------------------------------------------------ config overrides


@pytest.mark.parametrize(
    "body",
    [
        '{"decision": {"min_ev": NaN}}',
        '{"decision": {"min_probability": NaN}}',
        '{"decision": {"max_sigma": NaN}}',
        '{"bankroll": {"kelly_fraction": NaN}}',
        '{"bankroll": {"daily_loss_limit_fraction": Infinity}}',
        '{"decision": {"market_overrides": {"match_winner": {"min_probability": NaN}}}}',
    ],
)
def test_config_rejects_non_finite_numbers(client: TestClient, session_factory, body: str) -> None:
    """NaN compares false with everything: stored as a threshold it would switch a safety check off."""
    resp = client.put(
        "/api/control/config", content=body, headers={**CSRF, "content-type": "application/json"}
    )
    assert resp.status_code == 422, resp.text
    with session_factory() as s:
        assert s.get(SystemState, state.CONFIG_OVERRIDES) is None
    effective = client.get("/api/control/config").json()["effective"]
    assert all(
        math.isfinite(v) for section in effective.values() for v in section.values() if isinstance(v, float)
    )


# ------------------------------------------------------------------ robustness / security


def test_huge_ids_are_rejected_cleanly(client: TestClient, seeded: Seed) -> None:
    huge = "9999999999999999999999"
    for path in (
        f"/api/matches/{huge}",
        f"/api/matches/{huge}/odds-history",
        f"/api/bets/{huge}",
        f"/api/bets/{huge}/screenshot",
        f"/api/teams/{huge}",
        f"/api/matches?team_id={huge}",
        f"/api/matches/101/odds-history?selection_id={huge}",
        f"/api/bets?match_id={huge}",
        f"/api/decisions?match_id={huge}",
        f"/api/matches?offset={huge}",
    ):
        assert client.get(path).status_code == 422, path
    post = client.post(f"/api/review/links/{huge}", json={"action": "reject"}, headers=CSRF)
    assert post.status_code == 422
    relink = client.post(
        f"/api/review/links/{seeded.events['pending_unlinked']}",
        json={"action": "relink", "match_id": int(huge)},
        headers=CSRF,
    )
    assert relink.status_code == 422
    assert client.get("/api/matches/2147483647").status_code == 404
    assert client.get("/api/matches", params={"limit": 10**12}).status_code == 200  # capped, not rejected


PUBLIC_API_PATHS = {
    "/api/auth/me",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/ping",
    "/api/docs",
    "/api/docs/oauth2-redirect",
    "/api/openapi.json",
}


def test_every_data_route_requires_a_session_when_auth_is_on(
    app_factory, seeded: Seed, auth_settings
) -> None:
    app = app_factory(settings=auth_settings)
    checked = 0
    with TestClient(app, base_url=BASE_URL) as c:
        for path, operations in app.openapi()["paths"].items():
            assert path.startswith("/api"), path
            if path in PUBLIC_API_PATHS:
                continue
            url = path.replace("{match_id}", "1").replace("{bet_id}", "1")
            url = url.replace("{event_id}", "1").replace("{team_id}", "1")
            assert "{" not in url, f"unknown path parameter in {path}"
            for method in operations:
                resp = c.request(method.upper(), url, headers=CSRF, json={})
                assert resp.status_code == 401, (
                    f"{method} {path} answered {resp.status_code} without a session"
                )
                checked += 1
        assert c.get("/api/ping").status_code == 200
    assert checked >= 24  # every data / control operation of the API


def test_allowed_hosts_and_login_limits_come_from_settings(app_factory, seeded: Seed, tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        api={
            "password": SecretStr("s3cret-pass"),
            "session_secret": SecretStr("y" * 32),
            "allowed_hosts": ["cs2bot.lan", "other.lan:9000"],
            "login_max_failures": 2,
            "login_window_s": 600.0,
        },
    )
    with TestClient(app_factory(settings=settings), base_url=BASE_URL) as c:
        assert c.get("/api/ping", headers={"host": "cs2bot.lan:8000"}).status_code == 200
        assert c.get("/api/ping", headers={"host": "other.lan:9000"}).status_code == 200
        assert c.get("/api/ping", headers={"host": "evil.example:8000"}).status_code == 400
        bad = {"username": "admin", "password": "wrong"}
        assert c.post("/api/auth/login", json=bad, headers=CSRF).status_code == 401
        assert c.post("/api/auth/login", json=bad, headers=CSRF).status_code == 401
        locked = c.post("/api/auth/login", json=bad, headers=CSRF)
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 300
    # ApiOptions still overrides the settings (tests / embedding)
    with TestClient(
        app_factory(settings=settings, options=ApiOptions(login_max_failures=1)), base_url=BASE_URL
    ) as c:
        assert c.post("/api/auth/login", json=bad, headers=CSRF).status_code == 401
        assert c.post("/api/auth/login", json=bad, headers=CSRF).status_code == 429


def test_audit_actor_fits_the_column(app_factory, seeded: Seed, session_factory, tmp_path: Path) -> None:
    assert AuthState(enabled=True, username="u" * 200).actor == ("dashboard:" + "u" * 200)[:64]
    assert AuthState(enabled=False).actor == "dashboard"
    long_name = "owner-" + "x" * 80
    settings = make_settings(
        tmp_path,
        api={
            "username": long_name,
            "password": SecretStr("s3cret-pass"),
            "session_secret": SecretStr("z" * 32),
        },
    )
    with TestClient(app_factory(settings=settings), base_url=BASE_URL) as c:
        login = c.post(
            "/api/auth/login", json={"username": long_name, "password": "s3cret-pass"}, headers=CSRF
        )
        assert login.status_code == 200
        assert c.post("/api/control/pause", json={}, headers=CSRF).status_code == 200
    with session_factory() as s:
        actors = set(s.scalars(select(AuditLog.actor)))
        updated_by = s.get(SystemState, state.PAUSED)
    assert (
        actors and all(len(a) <= 64 for a in actors) and any(a.startswith("dashboard:owner-") for a in actors)
    )
    assert updated_by is not None and updated_by.updated_by is not None and len(updated_by.updated_by) <= 64


def test_login_rate_limiter_forgets_expired_clients() -> None:
    clock = [0.0]
    limiter = LoginRateLimiter(max_failures=3, global_max_failures=10**6, window_s=60, clock=lambda: clock[0])
    for i in range(300):
        limiter.record_failure(f"10.0.{i // 250}.{i % 250}")
    assert len(limiter._by_client) == 300  # all inside the window: nothing may be forgotten
    clock[0] = 120.0
    limiter.record_failure("10.9.9.9")
    assert set(limiter._by_client) == {"10.9.9.9"} and limiter.retry_after("10.9.9.9") is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("INFO", "info"),
        ("WARN", "warning"),
        (" Warning ", "warning"),
        ("FATAL", "critical"),
        ("bogus", "info"),
    ],
)
def test_uvicorn_log_level(name: str, expected: str) -> None:
    assert _uvicorn_log_level(name) == expected


# ------------------------------------------------------------------ review + the real odds re-mapper


def test_review_with_the_real_odds_remapper(app_factory, seeded: Seed, session_factory) -> None:
    """``cs2bot.odds.remap_event`` as ``event_remapper``: a swapped confirm re-keys the selections."""
    odds = pytest.importorskip("cs2bot.odds")
    mapper = odds.MarketMapper.from_yaml("config/betclic_markets.yaml")
    settings_holder: dict[str, Any] = {}

    def remapper(session: Session, event: BookmakerEvent) -> int:
        return int(odds.remap_event(session, event, mapper, settings=settings_holder["settings"]))

    app = app_factory(event_remapper=remapper)
    settings_holder["settings"] = app.state.cs2bot.settings
    event_id = seeded.events["pending_swapped"]  # Betclic lists "Synth Team 6 - Synth Team 5"; ours is 5 vs 6

    def keys() -> dict[str, tuple[str | None, bool]]:
        with session_factory() as s:
            rows = s.scalars(
                select(BookmakerSelection).where(
                    BookmakerSelection.raw_name.in_(["Synth Team 6", "Synth Team 5"])
                )
            )
            return {r.raw_name: (r.selection_key, bool(r.is_mapped)) for r in rows}

    assert keys() == {"Synth Team 6": ("team1", True), "Synth Team 5": ("team2", True)}
    with TestClient(app, base_url=BASE_URL) as c:
        body = c.post(
            f"/api/review/links/{event_id}", json={"action": "confirm", "teams_swapped": True}, headers=CSRF
        ).json()
        assert body["remapped"] is True and body["markets_reset"] == 1
        assert keys() == {"Synth Team 6": ("team2", True), "Synth Team 5": ("team1", True)}
        with session_factory() as s:
            event = s.get(BookmakerEvent, event_id)
            assert event is not None and "remap_required" not in (event.extra or {})
        # the re-keyed selections are joined with the model on the match page again
        detail = c.get("/api/matches/103").json()
        labels = {m["selection_raw"]: m["label"] for m in detail["markets"] if m["selection_raw"]}
        assert labels == {
            "Synth Team 6": "Match winner: Synth Team 6",
            "Synth Team 5": "Match winner: Synth Team 5",
        }
