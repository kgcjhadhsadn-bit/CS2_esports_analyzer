"""Bankroll, performance (calibration / markets) and health endpoints."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.api import create_app
from cs2bot.db import state
from cs2bot.db.models import MapPlayed, Match
from cs2bot.domain.betting import ExecutorHealth
from tests.api.api_seed import BASE_URL, CSRF, NOW, Seed, make_settings

# ------------------------------------------------------------------ bankroll


def test_bankroll_real_money(client: TestClient) -> None:
    b = client.get("/api/bankroll").json()
    assert b["mode"] == "real" and b["currency"] == "PLN" and b["as_of"] == "2026-11-20T12:00:00Z"
    assert b["balance"] == "968.44" and b["balance_source"] == "ledger"
    assert b["open_exposure"] == "15.00" and b["open_bets"] == 1 and b["bankroll_reference"] == "983.44"
    assert b["exposure_by_match"] == [{"match_id": 101, "stake": "15.00", "bets": 1}]
    day = b["daily"]
    assert day["period_start"] == "2026-11-19T23:00:00Z"  # Warsaw midnight
    assert day["start_bankroll"] == "1013.44" and day["limit_amount"] == "50.67"
    assert (
        day["realized_pnl"] == "-30.00"
        and day["open_stakes"] == "15.00"
        and day["worst_case_pnl"] == "-45.00"
    )
    assert day["remaining"] == "20.67" and day["reached"] is False
    assert day["used_fraction"] == pytest.approx(30 / 50.67, abs=1e-6)
    week = b["weekly"]
    assert week["period_start"] == "2026-11-15T23:00:00Z"
    assert week["start_bankroll"] == "1000.00" and week["limit_amount"] == "100.00"
    assert week["realized_pnl"] == "-16.56" and week["reached"] is False
    s = b["stats"]
    assert (s["bets"], s["open_bets"], s["settled_bets"], s["won"], s["lost"], s["void"]) == (
        4,
        1,
        3,
        1,
        1,
        1,
    )
    assert s["turnover"] == "50.00" and s["realized_pnl"] == "-16.56" and s["capital_base"] == "1000.00"
    assert s["roi"] == pytest.approx(-0.01656) and s["yield_rate"] == pytest.approx(-0.3312)
    assert s["hit_rate"] == 0.5 and s["avg_odds"] == "2.00"
    assert s["avg_clv"] == pytest.approx(0.01) and s["clv_bets"] == 2
    assert s["max_drawdown"] == "30.00" and s["max_drawdown_pct"] == pytest.approx(30 / 1013.44, abs=1e-6)
    assert [p["balance"] for p in b["equity_curve"]] == [
        "1000.00",
        "980.00",
        "970.00",
        "1003.44",
        "1013.44",
        "983.44",
        "968.44",
    ]
    assert [p["cumulative_pnl"] for p in b["pnl_curve"]] == ["13.44", "13.44", "-16.56"]
    assert [(p["period"], p["pnl"], p["bets"]) for p in b["pnl_by_day"]] == [
        ("2026-11-17", "13.44", 1),
        ("2026-11-18", "0.00", 1),
        ("2026-11-20", "-30.00", 1),
    ]
    assert [(p["period"], p["start"], p["pnl"], p["staked"]) for p in b["pnl_by_week"]] == [
        ("2026-W47", "2026-11-16", "-16.56", "50.00")
    ]
    assert [(p["period"], p["pnl"]) for p in b["pnl_by_month"]] == [("2026-11", "-16.56")]
    assert b["settings"]["daily_loss_limit_fraction"] == 0.05 and b["trading_halted"] is False


def test_bankroll_paper(client: TestClient) -> None:
    b = client.get("/api/bankroll", params={"dry_run": "true"}).json()
    assert b["mode"] == "dry_run" and b["balance_source"] == "paper"
    assert b["balance"] == "997.60" and b["open_exposure"] == "10.00" and b["bankroll_reference"] == "1007.60"
    assert (
        b["stats"]["won"] == 1
        and b["stats"]["realized_pnl"] == "7.60"
        and b["stats"]["capital_base"] == "1000.00"
    )
    assert [p["kind"] for p in b["equity_curve"]] == ["start", "settlement"]
    assert b["equity_curve"][-1]["balance"] == "1007.60"
    assert b["daily"]["start_bankroll"] == "1007.60" and b["daily"]["realized_pnl"] == "0.00"


def test_bankroll_empty_db(app_factory) -> None:
    with TestClient(app_factory(), base_url=BASE_URL) as c:
        b = c.get("/api/bankroll").json()
    assert b["balance"] == "1000.00" and b["balance_source"] == "starting_bankroll"
    assert b["stats"]["bets"] == 0 and b["stats"]["roi"] == 0.0 and b["stats"]["hit_rate"] is None
    assert b["equity_curve"] == [] and b["daily"]["limit_amount"] == "50.00"


def test_loss_limit_override_reaches_limit(client: TestClient) -> None:
    resp = client.put(
        "/api/control/config", json={"bankroll": {"daily_loss_limit_fraction": 0.02}}, headers=CSRF
    )
    assert resp.status_code == 200, resp.text
    day = client.get("/api/bankroll").json()["daily"]
    assert day["limit_fraction"] == 0.02 and day["limit_amount"] == "20.27" and day["reached"] is True
    assert day["remaining"] == "0.00" and day["used_fraction"] > 1
    alerts = {a["code"]: a for a in client.get("/api/health").json()["alerts"]}
    assert alerts["loss_limit_reached"]["level"] == "critical"
    assert "Daily loss limit reached" in alerts["loss_limit_reached"]["message"]


# ------------------------------------------------------------------ performance


def test_calibration(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    body = client.get("/api/performance/calibration").json()
    assert body["n_bins"] == 10
    bets = body["bets"]
    assert bets["n"] == 2 and bets["base_rate"] == 0.5 and bets["mean_predicted"] == pytest.approx(0.77)
    assert bets["brier"] == pytest.approx(((0.78 - 1) ** 2 + 0.76**2) / 2)
    assert sum(b["count"] for b in bets["bins"]) == 2 and len(bets["bins"]) == 10
    hot = next(b for b in bets["bins"] if b["count"])
    assert hot["lower"] == 0.7 and hot["observed_rate"] == 0.5 and hot["ci_low"] < 0.5 < hot["ci_high"]
    assert [p["period"] for p in bets["brier_by_month"]] == ["2026-11"]

    with session_factory() as s:
        matches = {m.id: m for m in s.scalars(select(Match).where(Match.id <= 10))}
        maps = {mp.match_id: mp for mp in s.scalars(select(MapPlayed).where(MapPlayed.map_number == 1))}
    mw_outcomes = [1 if matches[i].winner_team_id == matches[i].team1_id else 0 for i in range(1, 11)]
    map_outcomes = [1 if maps[i].winner_team_id == matches[i].team1_id else 0 for i in range(1, 11)]
    by_market = body["predictions_by_market"]
    assert set(by_market) == {"match_winner", "map_winner"}
    mw = by_market["match_winner"]
    assert mw["n"] == 10  # team1 row only - the complementary team2 row is not double counted
    assert mw["base_rate"] == pytest.approx(sum(mw_outcomes) / 10)
    assert mw["brier"] == pytest.approx(sum((0.6 - y) ** 2 for y in mw_outcomes) / 10)
    assert by_market["map_winner"]["brier"] == pytest.approx(sum((0.55 - y) ** 2 for y in map_outcomes) / 10)
    assert body["predictions"]["n"] == 20
    assert body["predictions"]["ece"] is not None

    real_only = client.get("/api/performance/calibration", params={"mode": "dry_run", "bins": 5}).json()
    assert (
        real_only["bets"]["n"] == 0
        and len(real_only["bets"]["bins"]) == 5
        and real_only["bets"]["brier"] is None
    )
    future = client.get("/api/performance/calibration", params={"from": "2030-01-01"}).json()
    assert future["predictions"]["n"] == 0
    assert client.get("/api/performance/calibration", params={"bins": 1}).status_code == 422


def test_market_performance(client: TestClient) -> None:
    body = client.get("/api/performance/markets").json()
    assert body["mode"] == "real"
    rows = {r["market_type"]: r for r in body["items"]}
    assert list(rows) == ["match_winner", "map_winner", "total_maps"]  # taxonomy order
    mw = rows["match_winner"]
    assert (mw["bets"], mw["open"], mw["settled"], mw["won"]) == (2, 1, 1, 1)
    assert mw["staked"] == "20.00" and mw["pnl"] == "13.44" and mw["roi"] == pytest.approx(0.672)
    assert mw["hit_rate"] == 1.0 and mw["avg_clv"] == 0.03 and mw["avg_odds"] == "1.90"
    assert mw["avg_ev"] == pytest.approx((0.30 + 0.3728) / 2)
    assert rows["map_winner"]["pnl"] == "-30.00" and rows["map_winner"]["hit_rate"] == 0.0
    assert rows["total_maps"]["void"] == 1 and rows["total_maps"]["roi"] is None
    total = body["total"]
    assert total["market_type"] == "all" and total["bets"] == 4 and total["pnl"] == "-16.56"
    paper = client.get("/api/performance/markets", params={"mode": "dry_run"}).json()
    assert {r["market_type"] for r in paper["items"]} == {"match_winner", "map_handicap"}
    assert client.get("/api/performance/markets", params={"mode": "all"}).json()["total"]["bets"] == 6
    windowed = client.get("/api/performance/markets", params={"from": "2026-11-20T00:00:00Z"}).json()
    assert windowed["total"]["bets"] == 2  # the lost bet and the open bet were placed today


# ------------------------------------------------------------------ health


def test_health_alerts(client: TestClient, seeded: Seed) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    h = resp.json()
    assert h["status"] == "critical" and h["db"]["ok"] is True and h["dry_run"] is True
    codes = [a["code"] for a in h["alerts"]]
    for expected in (
        "betclic_login_required",
        "dry_run_on",
        "executor_unhealthy",
        "executor_selectors_failing",
        "job_stale",
        "job_failing",
        "job_stuck",
        "link_review_pending",
    ):
        assert expected in codes, expected
    assert "odds_collector_stale" not in codes and "loss_limit_reached" not in codes
    levels = [a["level"] for a in h["alerts"]]
    assert levels == sorted(levels, key=["critical", "warning", "info"].index)
    messages = " | ".join(a["message"] for a in h["alerts"])
    assert "Betclic login required" in messages and "Dry-run mode is ON" in messages
    assert "confirm_button" in messages and "settle_bets" in messages
    jobs = {j["name"]: j for j in h["jobs"]}
    assert jobs["odds_collect"]["kind"] == "odds" and jobs["odds_collect"]["stale"] is False
    assert (
        jobs["odds_collect"]["last_success_age_s"] == 60.0
        and jobs["odds_collect"]["expected_interval_s"] == 180
    )
    assert jobs["ingest_upcoming"]["stale"] is True
    assert (
        jobs["settle_bets"]["consecutive_failures"] == 3
        and jobs["settle_bets"]["error"] == "RuntimeError: boom"
    )
    assert (
        jobs["predict_upcoming"]["status"] == "running"
        and jobs["predict_upcoming"]["consecutive_failures"] == 0
    )
    ex = h["executor"]
    assert ex["source"] == "system_state" and ex["logged_in"] is False and ex["balance"] == "968.44"
    assert ex["age_s"] == 300.0
    f = h["freshness"]
    assert f["last_odds_snapshot_at"] == "2026-11-20T11:00:00Z" and f["last_odds_snapshot_age_s"] == 3600.0
    assert f["last_event_seen_age_s"] == 120.0 and f["last_match_sync_at"] == "2026-11-20T11:50:00Z"
    assert f["last_decision_at"] == "2026-11-20T11:30:00Z"
    assert {j["kind"] for j in f["sync_jobs"]} == {"odds", "upcoming"}
    assert h["pending_link_reviews"] == 2 and h["trading"]["halted"] is False


def test_health_provider_and_trading_alerts(app_factory, seeded: Seed, session_factory) -> None:
    healthy = ExecutorHealth(ok=True, logged_in=True, selectors_ok={"stake_input": True}, checked_at=NOW)
    with session_factory() as s:
        state.set_kill_switch(s, True, actor="test", reason="manual stop")
        state.set_paused(s, True, actor="system", reason="daily loss limit")
        s.commit()
    with TestClient(app_factory(executor_health_provider=lambda: healthy), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    codes = {a["code"]: a for a in h["alerts"]}
    assert h["executor"]["source"] == "provider" and h["executor"]["logged_in"] is True
    assert "betclic_login_required" not in codes and "executor_unhealthy" not in codes
    assert codes["kill_switch_on"]["message"] == "Kill switch is ON: manual stop"
    assert codes["trading_paused"]["message"] == "Trading paused: daily loss limit"
    assert h["trading"]["halted"] is True and h["trading"]["reason"] == "kill switch is on"


def test_health_provider_failure_falls_back(app_factory, seeded: Seed) -> None:
    def broken() -> ExecutorHealth:
        raise RuntimeError("browser gone")

    with TestClient(app_factory(executor_health_provider=broken), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    codes = [a["code"] for a in h["alerts"]]
    assert "executor_health_error" in codes and h["executor"]["source"] == "system_state"
    assert "browser gone" not in str(h)  # only the exception type is reported


def test_health_odds_collector_stale(app_factory, seeded: Seed) -> None:
    with TestClient(
        app_factory(now=NOW + timedelta(hours=2), executor={"dry_run": False}), base_url=BASE_URL
    ) as c:
        h = c.get("/api/health").json()
    alerts = {a["code"]: a for a in h["alerts"]}
    assert "odds collector stale" in alerts["odds_collector_stale"]["message"].lower()
    assert "dry_run_on" not in alerts and h["dry_run"] is False
    assert alerts["executor_health_stale"]["level"] == "warning"  # checked 2h05 ago


def test_health_empty_database(app_factory) -> None:
    with TestClient(app_factory(), base_url=BASE_URL) as c:
        h = c.get("/api/health").json()
    codes = [a["code"] for a in h["alerts"]]
    assert "odds_collector_never_ran" in codes and "executor_health_unknown" in codes
    assert h["jobs"] == [] and h["status"] == "warning"


def test_health_database_down(tmp_path) -> None:
    broken = sessionmaker(bind=create_engine(f"sqlite:///{tmp_path}/missing-dir/db.sqlite"))
    app = create_app(make_settings(tmp_path), broken)
    with TestClient(app, base_url=BASE_URL) as c:
        resp = c.get("/api/health")
        assert resp.status_code == 200
        h = resp.json()
        assert (
            h["status"] == "critical"
            and h["db"]["ok"] is False
            and h["db"]["error"].startswith("OperationalError")
        )
        assert next(a["code"] for a in h["alerts"]) == "db_unreachable"
        assert h["trading"] is None and h["freshness"] is None
        # other endpoints answer 503 instead of crashing
        failing = c.get("/api/matches")
        assert failing.status_code == 503 and failing.json() == {"detail": "database unavailable"}
        assert c.get("/api/ping").status_code == 200
